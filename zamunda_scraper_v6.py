"""
Torrent Site Scraper  v6  —  Playwright edition  (site-agnostic / config-driven)
==================================================================================
This script no longer hardcodes the target site. All site-specific details
(base URL, API path, query parameter names, category flags, field names,
response shape, ID range) live in config.json next to this script and are
loaded once at startup.

Shipped default: config.json is pre-filled for zamunda.rip and works
out of the box with zero edits — see README.md "Using it as-is".

To point this at a different torrent site with a similar JSON API,
edit config.json — see README.md "Tweaking the config". No Python
changes should be needed for sites that expose a comparable
q / offset / category-flags API shape.

Four modes — choose at startup:
    [1] Latin search    — aa-zz + keywords + vowel-led 3-char combos
    [2] Category browse — the category endpoints defined in config.json
    [3] Cyrillic search — аа-яя combos + vowel-led Cyrillic triples
                          (Bulgarian-specific; not configurable — see README)
    [4] ID scrape       — queries every ID in the configured id_range
                          with all category flags on

All modes share
----------------
* Deduplication by the configured external_id field — global, never reset
* API via page.evaluate() so Cloudflare cookies are automatic
* Auto-save after every query / category / ID batch
* Resumable — skips already-completed work on restart

File map
--------
  Mode 1  checkpoint : zamunda_checkpoint.json
          output     : zamunda_torrents.json

  Mode 2  reads      : zamunda_checkpoint.json  (never modified)
          checkpoint : zamunda_category_checkpoint.json
          output     : zamunda_final.json

  Mode 3  reads      : zamunda_checkpoint.json + zamunda_category_checkpoint.json
          checkpoint : zamunda_cyrillic_checkpoint.json
          output     : zamunda_cyrillic_final.json

  Mode 4  reads      : all three checkpoints above  (never modified)
          checkpoint : zamunda_id_checkpoint.json
          output     : zamunda_id_final.json

Requirements
------------
    pip install playwright
    playwright install chromium

Usage
-----
    python zamunda_scraper_v6.py
    (then type 1, 2, 3 or 4 when prompted)
"""

import asyncio
import json
import string
import random
import sys
from itertools import product
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Page


# ══════════════════════════════════════════════════════════════
#  Config loading
# ══════════════════════════════════════════════════════════════

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"⚠  config.json not found next to this script ({CONFIG_PATH}).")
        print("   This file is required — see README.md for its format.")
        sys.exit(1)
    try:
        with CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as exc:
        print(f"⚠  config.json is not valid JSON: {exc}")
        sys.exit(1)

    required = ["base_url", "api_path", "query_param", "offset_param",
                "page_size", "category_flags", "id_scrape_flags",
                "categories", "field_map", "response_wrapper_keys", "id_range"]
    missing = [k for k in required if k not in cfg]
    if missing:
        print(f"⚠  config.json is missing required keys: {missing}")
        sys.exit(1)

    return cfg


CONFIG        = load_config()
SITE_NAME     = CONFIG.get("site_name", "Site")
BASE_URL      = CONFIG["base_url"]
API_ENDPOINT  = BASE_URL + CONFIG["api_path"]
QUERY_PARAM   = CONFIG["query_param"]
OFFSET_PARAM  = CONFIG["offset_param"]
PAGE_SIZE     = CONFIG["page_size"]
CATEGORY_FLAGS_DEFAULT = CONFIG["category_flags"]     # all-off template
ID_SCRAPE_FLAGS        = CONFIG["id_scrape_flags"]
CATEGORIES             = CONFIG["categories"]
FIELD_MAP               = CONFIG["field_map"]
RESPONSE_WRAPPER_KEYS   = CONFIG["response_wrapper_keys"]
ID_START                = CONFIG["id_range"]["start"]
ID_END                  = CONFIG["id_range"]["end"]
EXTRA_SEEDS              = CONFIG.get("extra_seeds", [])


# ══════════════════════════════════════════════════════════════
#  Runtime configuration  (not site-specific — safe to tune here)
# ══════════════════════════════════════════════════════════════

SEARCH_CHECKPOINT   = "zamunda_checkpoint.json"
SEARCH_OUTPUT       = "zamunda_torrents.json"

CAT_CHECKPOINT      = "zamunda_category_checkpoint.json"
CAT_OUTPUT          = "zamunda_final.json"

CYR_CHECKPOINT      = "zamunda_cyrillic_checkpoint.json"
CYR_OUTPUT          = "zamunda_cyrillic_final.json"

ID_CHECKPOINT       = "zamunda_id_checkpoint.json"
ID_OUTPUT           = "zamunda_id_final.json"

MAX_LATIN_QUERIES   = 676       # full aa–zz
MAX_CYR_TRIPLES     = 300       # cap on vowel-led Cyrillic triples
ID_BATCH_SAVE       = 500       # save checkpoint every N ID requests

MIN_DELAY           = 0.2
MAX_DELAY           = 0.4

HEADLESS            = False
LOGIN_TIMEOUT       = 120_000
REQUEST_TIMEOUT     = 30_000

# Bulgarian Cyrillic alphabet (30 letters) — mode 3 is Bulgaria-specific
# and intentionally not configurable via config.json (see README.md).
CYRILLIC      = "абвгдежзийклмнопрстуфхцчшщъьюя"
CYR_VOWELS    = "аеиоуя"


# ══════════════════════════════════════════════════════════════
#  Query generation
# ══════════════════════════════════════════════════════════════

def generate_latin_queries() -> list[str]:
    """aa-zz combos + EXTRA_SEEDS (from config) + vowel-led 3-char combos."""
    chars  = string.ascii_lowercase
    combos = ["".join(p) for p in product(chars, repeat=2)][:MAX_LATIN_QUERIES]
    seen   = set(combos)
    extras = [s for s in EXTRA_SEEDS if s not in seen]
    seen.update(extras)
    triples = ["".join(p) for p in product("aeiou", chars, chars)][:200]
    triples = [t for t in triples if t not in seen]
    seen.update(triples)
    return combos + extras + triples


def generate_cyrillic_queries() -> list[str]:
    """
    аа-яя (all 900 two-char Cyrillic combos)
    + vowel-led Cyrillic triples (capped at MAX_CYR_TRIPLES).
    """
    combos  = ["".join(p) for p in product(CYRILLIC, repeat=2)]
    seen    = set(combos)
    triples = ["".join(p) for p in product(CYR_VOWELS, CYRILLIC, CYRILLIC)]
    triples = [t for t in triples if t not in seen][:MAX_CYR_TRIPLES]
    seen.update(triples)
    return combos + triples


# ══════════════════════════════════════════════════════════════
#  Shared data helpers
# ══════════════════════════════════════════════════════════════

def extract_torrent(item: dict) -> dict:
    """
    Map one raw API item to our output schema using FIELD_MAP from config.
    All field access uses .get() so missing keys produce None gracefully.
    Output keys are always the canonical names (external_id, title, ...)
    regardless of what the source API calls them.
    """
    return {
        "external_id": item.get(FIELD_MAP["external_id"]),
        "title":       item.get(FIELD_MAP["title"]),
        "category":    item.get(FIELD_MAP["category"]),
        "size":        item.get(FIELD_MAP["size"]),
        "description": item.get(FIELD_MAP["description"]),
        "source":      item.get(FIELD_MAP["source"]),
        "is_bgaudio":  item.get(FIELD_MAP["is_bgaudio"]),
        "magnet":      item.get(FIELD_MAP["magnet"]),
    }


def process_items(items: list[dict], seen: set, results: list) -> int:
    """Deduplicate by the configured external_id field, append new items."""
    added = 0
    eid_field = FIELD_MAP["external_id"]
    for raw in items:
        key = raw.get(eid_field)
        if key is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        t = extract_torrent(raw)
        results.append(t)
        added += 1
        print(f"    [+] external_id={key!r}  title={t.get('title')!r}")
    return added


# ══════════════════════════════════════════════════════════════
#  Browser-side fetch
# ══════════════════════════════════════════════════════════════

_FETCH_JS = """
async ({ url, timeoutMs }) => {
    const ctrl  = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
        const res = await fetch(url, {
            credentials: "include",
            signal: ctrl.signal,
            headers: { "Accept": "application/json" }
        });
        clearTimeout(timer);
        if (!res.ok) return { error: "HTTP " + res.status, data: null };
        const data = await res.json();
        return { error: null, data };
    } catch (e) {
        clearTimeout(timer);
        return { error: e.message, data: null };
    }
}
"""

_first_call_done = False


def _unwrap(data) -> list | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in RESPONSE_WRAPPER_KEYS:
            if key in data and isinstance(data[key], list):
                return data[key]
        print(f"    ⚠  Unrecognised dict keys: {list(data.keys())}")
        return []
    print(f"    ⚠  Unexpected response type: {type(data)}")
    return None


async def fetch_page(page: Page, params: dict) -> list[dict] | None:
    """Fetch one page via browser-side fetch(). Returns items, [] or None."""
    global _first_call_done

    url    = f"{API_ENDPOINT}?{urlencode(params)}"
    result = await page.evaluate(_FETCH_JS, {"url": url, "timeoutMs": REQUEST_TIMEOUT})

    if result.get("error"):
        print(f"    ⚠  fetch error: {result['error']}")
        return None

    data = result.get("data")

    if not _first_call_done:
        _first_call_done = True
        print("\n  ── First API response structure ──")
        if isinstance(data, list):
            print(f"  Type  : list  (len={len(data)})")
            if data:
                print(f"  Keys  : {list(data[0].keys())}")
        elif isinstance(data, dict):
            print(f"  Type  : dict  keys={list(data.keys())}")
            for k in RESPONSE_WRAPPER_KEYS:
                if k in data and isinstance(data[k], list) and data[k]:
                    print(f"  Items under '{k}': {list(data[k][0].keys())}")
        print("  ──────────────────────────────────\n")

    return _unwrap(data)


# ══════════════════════════════════════════════════════════════
#  Login helper
# ══════════════════════════════════════════════════════════════

async def wait_for_login(page: Page) -> None:
    print("\n" + "=" * 56)
    print("  Browser opened.  Please:")
    print("  1.  Solve any Cloudflare challenge")
    print(f"  2.  Log in to {SITE_NAME}")
    print("  3.  Return here and press ENTER to begin scraping")
    print("=" * 56 + "\n")
    await page.goto(BASE_URL, timeout=LOGIN_TIMEOUT, wait_until="networkidle")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "Press ENTER when you are logged in ...\n")
    print("Session confirmed.\n")


# ══════════════════════════════════════════════════════════════
#  Checkpoint helpers — search (mode 1)
# ══════════════════════════════════════════════════════════════

def load_search_checkpoint() -> tuple[set, set, list]:
    path = Path(SEARCH_CHECKPOINT)
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                cp = json.load(f)
            results      = cp.get("torrents", [])
            seen_ids     = set(t["external_id"] for t in results if t.get("external_id"))
            done_queries = set(cp.get("done_queries", []))
            print(f"  Search checkpoint    : {len(seen_ids):,} torrents, "
                  f"{len(done_queries)} queries done.")
            return seen_ids, done_queries, results
        except Exception as exc:
            print(f"  Could not load search checkpoint ({exc}) — starting fresh.")
    return set(), set(), []


def save_search_checkpoint(seen_ids: set, done_queries: set, results: list) -> None:
    with Path(SEARCH_CHECKPOINT).open("w", encoding="utf-8") as f:
        json.dump({"done_queries": sorted(done_queries), "torrents": results},
                  f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
#  Checkpoint helpers — category (mode 2)
# ══════════════════════════════════════════════════════════════

def _read_checkpoint_ids(path: Path, torrents_key: str) -> tuple[set, list]:
    """Generic helper: read a checkpoint file, return (seen_ids, results)."""
    if not path.exists():
        return set(), []
    try:
        with path.open(encoding="utf-8") as f:
            cp = json.load(f)
        results  = cp.get(torrents_key, [])
        seen_ids = set(t["external_id"] for t in results if t.get("external_id"))
        return seen_ids, results
    except Exception as exc:
        print(f"  Could not read {path.name} ({exc}).")
        return set(), []


def load_cat_checkpoints() -> tuple[set, set, list, list]:
    """Returns (seen_ids, done_labels, old_results, new_results)."""
    old_ids, old_results = _read_checkpoint_ids(
        Path(SEARCH_CHECKPOINT), "torrents")
    if old_results:
        print(f"  Search checkpoint    : {len(old_ids):,} existing torrents.")
    else:
        print(f"  No search checkpoint found — starting category scrape from scratch.")

    cat_path = Path(CAT_CHECKPOINT)
    done_labels = set()
    new_results = []
    if cat_path.exists():
        try:
            with cat_path.open(encoding="utf-8") as f:
                cp = json.load(f)
            new_results = cp.get("new_torrents", [])
            done_labels = set(cp.get("done_categories", []))
            print(f"  Category checkpoint  : {len(new_results):,} new torrents, "
                  f"{len(done_labels)} categories done.")
        except Exception as exc:
            print(f"  Could not read category checkpoint ({exc}) — starting fresh.")

    seen_ids = old_ids.copy()
    for t in new_results:
        eid = t.get("external_id")
        if eid:
            seen_ids.add(eid)

    return seen_ids, done_labels, old_results, new_results


def save_cat_checkpoint(done_labels: set, new_results: list) -> None:
    with Path(CAT_CHECKPOINT).open("w", encoding="utf-8") as f:
        json.dump({"done_categories": sorted(done_labels),
                   "new_torrents": new_results}, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
#  Checkpoint helpers — Cyrillic (mode 3)
# ══════════════════════════════════════════════════════════════

def load_cyr_checkpoints() -> tuple[set, set, list, list]:
    """
    Loads search + category + Cyrillic checkpoints.
    Returns (seen_ids, done_queries, base_results, new_results).
    base_results — everything from modes 1+2 (never modified).
    new_results  — torrents found by Cyrillic search so far.
    """
    ids1, r1 = _read_checkpoint_ids(Path(SEARCH_CHECKPOINT), "torrents")
    if r1:
        print(f"  Search checkpoint    : {len(ids1):,} torrents.")

    ids2, r2 = _read_checkpoint_ids(Path(CAT_CHECKPOINT), "new_torrents")
    if r2:
        print(f"  Category checkpoint  : {len(ids2):,} torrents.")

    cyr_path    = Path(CYR_CHECKPOINT)
    done_q      = set()
    new_results = []
    if cyr_path.exists():
        try:
            with cyr_path.open(encoding="utf-8") as f:
                cp = json.load(f)
            new_results = cp.get("new_torrents", [])
            done_q      = set(cp.get("done_queries", []))
            print(f"  Cyrillic checkpoint  : {len(new_results):,} new torrents, "
                  f"{len(done_q)} queries done.")
        except Exception as exc:
            print(f"  Could not read Cyrillic checkpoint ({exc}) — starting fresh.")

    seen_ids = ids1 | ids2
    for t in new_results:
        eid = t.get("external_id")
        if eid:
            seen_ids.add(eid)

    base_results = r1 + r2
    return seen_ids, done_q, base_results, new_results


def save_cyr_checkpoint(done_queries: set, new_results: list) -> None:
    with Path(CYR_CHECKPOINT).open("w", encoding="utf-8") as f:
        json.dump({"done_queries": sorted(done_queries),
                   "new_torrents": new_results}, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
#  Checkpoint helpers — ID scrape (mode 4)
# ══════════════════════════════════════════════════════════════

def load_id_checkpoints() -> tuple[set, int, list, list]:
    """
    Loads all previous checkpoints + the ID checkpoint.
    Returns (seen_ids, last_id_tried, base_results, new_results).
    seen_ids      — all known external_ids across every mode
    last_id_tried — highest ID already attempted (0 if none)
    base_results  — everything from modes 1+2+3 (never modified)
    new_results   — torrents found by ID scrape so far
    """
    ids1, r1 = _read_checkpoint_ids(Path(SEARCH_CHECKPOINT), "torrents")
    ids2, r2 = _read_checkpoint_ids(Path(CAT_CHECKPOINT),    "new_torrents")
    ids3, r3 = _read_checkpoint_ids(Path(CYR_CHECKPOINT),    "new_torrents")

    counts = [(ids1, r1, "Search"), (ids2, r2, "Category"), (ids3, r3, "Cyrillic")]
    for ids, r, label in counts:
        if r:
            print(f"  {label} checkpoint : {len(ids):,} torrents.")

    id_path     = Path(ID_CHECKPOINT)
    last_tried  = 0
    new_results = []
    if id_path.exists():
        try:
            with id_path.open(encoding="utf-8") as f:
                cp = json.load(f)
            new_results = cp.get("new_torrents", [])
            last_tried  = cp.get("last_id_tried", 0)
            print(f"  ID checkpoint        : {len(new_results):,} new torrents, "
                  f"last ID tried: {last_tried:,}.")
        except Exception as exc:
            print(f"  Could not read ID checkpoint ({exc}) — starting fresh.")

    seen_ids = ids1 | ids2 | ids3
    for t in new_results:
        eid = t.get("external_id")
        if eid:
            seen_ids.add(eid)

    base_results = r1 + r2 + r3
    return seen_ids, last_tried, base_results, new_results


def save_id_checkpoint(last_tried: int, new_results: list) -> None:
    with Path(ID_CHECKPOINT).open("w", encoding="utf-8") as f:
        json.dump({"last_id_tried": last_tried,
                   "new_torrents":  new_results}, f, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
#  Generic search/query pagination helper
# ══════════════════════════════════════════════════════════════

async def scrape_queries(
    page:        Page,
    queries:     list[str],
    flags:       dict,
    seen:        set,
    results:     list,
    done_queries: set,
    save_fn,           # callable(done_queries, results)
    label:       str,
) -> None:
    """
    Shared pagination loop for any list of string queries.
    Mutates seen, results, done_queries in place.
    Calls save_fn(done_queries, results) after each completed query.
    """
    total = len(queries)
    for q_idx, query in enumerate(queries, 1):
        if query in done_queries:
            continue

        print(f"\n[{q_idx}/{total}]  {label} query='{query}'")
        offset         = 0
        new_this_query = 0

        while True:
            print(f"  offset={offset}", end="  ", flush=True)
            params = {**flags, QUERY_PARAM: query, OFFSET_PARAM: str(offset)}
            items  = await fetch_page(page, params)

            if items is None:
                print("<- error, skipping rest of this query")
                break
            if not items:
                print("<- empty — end of results")
                break

            eid_field = FIELD_MAP["external_id"]
            sample = [item.get(eid_field) for item in items[:5]]
            print(f"\n    [debug] first 5 external_ids: {sample}")

            added           = process_items(items, seen, results)
            new_this_query += added
            print(f"  <- {len(items)} items  |  +{added} new  |  "
                  f"total unique: {len(seen):,}")

            offset += PAGE_SIZE
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        done_queries.add(query)
        save_fn(done_queries, results)
        print(f"  + '{query}' complete — {new_this_query} new torrents")


# ══════════════════════════════════════════════════════════════
#  Mode 1 — Latin search
# ══════════════════════════════════════════════════════════════

async def run_search_scrape(page: Page) -> None:
    queries                      = generate_latin_queries()
    seen, done_queries, results  = load_search_checkpoint()

    print(f"\n  Queries planned  : {len(queries)}")
    print(f"  Queries done     : {len(done_queries)}")
    print(f"  Unique torrents  : {len(seen):,}\n")

    await scrape_queries(
        page, queries, CATEGORY_FLAGS_DEFAULT, seen, results, done_queries,
        save_fn=lambda dq, r: save_search_checkpoint(seen, dq, r),
        label="latin",
    )

    out = Path(SEARCH_OUTPUT)
    with out.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n  Saved {len(results):,} unique torrents -> {out.resolve()}")


# ══════════════════════════════════════════════════════════════
#  Mode 2 — Category browse
# ══════════════════════════════════════════════════════════════

async def run_category_scrape(page: Page) -> None:
    seen, done_labels, old_results, new_results = load_cat_checkpoints()

    print(f"\n  Total known IDs  : {len(seen):,}")
    print(f"  Categories total : {len(CATEGORIES)}")
    print(f"  Categories done  : {len(done_labels)}\n")

    for cat in CATEGORIES:
        label = cat["label"]
        flags = {k: v for k, v in cat.items() if k != "label"}

        if label in done_labels:
            print(f"  [SKIP] {label}")
            continue

        print(f"\n  ── {label}")
        offset       = 0
        new_this_cat = 0

        while True:
            print(f"    offset={offset}", end="  ", flush=True)
            params = {**flags, QUERY_PARAM: "", OFFSET_PARAM: str(offset)}
            items  = await fetch_page(page, params)

            if items is None:
                print("<- error, skipping rest of this category")
                break
            if not items:
                print("<- empty — end of category")
                break

            eid_field = FIELD_MAP["external_id"]
            sample = [item.get(eid_field) for item in items[:5]]
            print(f"\n      [debug] first 5 external_ids: {sample}")

            added        = process_items(items, seen, new_results)
            new_this_cat += added
            print(f"    <- {len(items)} items  |  +{added} new  |  "
                  f"total unique: {len(seen):,}")

            offset += PAGE_SIZE
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        done_labels.add(label)
        save_cat_checkpoint(done_labels, new_results)
        print(f"  ✓ Done — {new_this_cat} new torrents for this category")

    all_results = old_results + new_results
    out = Path(CAT_OUTPUT)
    with out.open("w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n  Saved {len(all_results):,} total torrents -> {out.resolve()}")
    print(f"  ({len(old_results):,} from search  +  {len(new_results):,} from categories)")


# ══════════════════════════════════════════════════════════════
#  Mode 3 — Cyrillic search
# ══════════════════════════════════════════════════════════════

async def run_cyrillic_scrape(page: Page) -> None:
    queries                          = generate_cyrillic_queries()
    seen, done_queries, base, new_r  = load_cyr_checkpoints()

    # Merge base + new_r into one list so the running total counter
    # starts from the full existing count and climbs from there.
    all_results = base + new_r

    print(f"\n  Cyrillic queries planned : {len(queries)}")
    print(f"  Queries done             : {len(done_queries)}")
    print(f"  Total known IDs          : {len(seen):,}\n")

    await scrape_queries(
        page, queries, CATEGORY_FLAGS_DEFAULT, seen, all_results, done_queries,
        save_fn=lambda dq, r: save_cyr_checkpoint(dq, r[len(base):]),
        label="cyrillic",
    )

    out = Path(CYR_OUTPUT)
    with out.open("w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n  Saved {len(all_results):,} total torrents -> {out.resolve()}")
    print(f"  ({len(base):,} from prev modes  +  {len(all_results) - len(base):,} new from Cyrillic)")


# ══════════════════════════════════════════════════════════════
#  Mode 4 — ID scrape
# ══════════════════════════════════════════════════════════════

async def run_id_scrape(page: Page) -> None:
    seen, last_tried, base_results, new_results = load_id_checkpoints()

    start_id   = max(ID_START, last_tried + 1)
    total_ids  = ID_END - start_id + 1
    skippable  = len([eid for eid in seen if isinstance(eid, int)
                      and ID_START <= eid <= ID_END])

    print(f"\n  ID range         : {start_id:,} – {ID_END:,}")
    print(f"  IDs to request   : {total_ids:,}")
    print(f"  Already known    : {skippable:,}  (will be skipped instantly)")
    print(f"  Total known IDs  : {len(seen):,}")
    est_hours = total_ids * ((MIN_DELAY + MAX_DELAY) / 2) / 3600
    print(f"  Est. time        : ~{est_hours:.0f} hours  (fully resumable)\n")

    batch_count = 0

    for torrent_id in range(start_id, ID_END + 1):
        # Skip IDs we already have — no request needed
        if torrent_id in seen:
            last_tried = torrent_id
            continue

        params = {**ID_SCRAPE_FLAGS, QUERY_PARAM: str(torrent_id), OFFSET_PARAM: "0"}
        items  = await fetch_page(page, params)

        if items is None:
            # Fetch error — save progress and continue
            print(f"  ⚠  Error at ID {torrent_id:,}, continuing...")
        elif items:
            added = process_items(items, seen, new_results)
            if added:
                print(f"  ID {torrent_id:,}  -> +{added}  "
                      f"total unique: {len(seen):,}")
        # (empty list = ID doesn't exist, silent skip)

        last_tried   = torrent_id
        batch_count += 1

        if batch_count % ID_BATCH_SAVE == 0:
            save_id_checkpoint(last_tried, new_results)
            pct = (torrent_id - start_id) / max(total_ids, 1) * 100
            print(f"  [checkpoint]  ID {torrent_id:,}  "
                  f"({pct:.1f}%)  new found: {len(new_results):,}")

        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    # Final save
    save_id_checkpoint(last_tried, new_results)

    all_results = base_results + new_results
    out = Path(ID_OUTPUT)
    with out.open("w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n  Saved {len(all_results):,} total torrents -> {out.resolve()}")
    print(f"  ({len(base_results):,} from prev modes  +  {len(new_results):,} new from ID scrape)")


# ══════════════════════════════════════════════════════════════
#  Mode selection
# ══════════════════════════════════════════════════════════════

def pick_mode() -> int:
    print("  Choose scrape mode:")
    print()
    print("  [1]  Latin search scrape")
    print("       Queries aa-zz + keywords + vowel-led 3-char combos.")
    print(f"       Checkpoint : {SEARCH_CHECKPOINT}")
    print(f"       Output     : {SEARCH_OUTPUT}")
    print()
    print("  [2]  Category browse")
    print(f"       Browses the {len(CATEGORIES)} categories defined in config.json.")
    print(f"       Checkpoint : {CAT_CHECKPOINT}")
    print(f"       Output     : {CAT_OUTPUT}  (search + categories merged)")
    print()
    print("  [3]  Cyrillic search scrape")
    print("       Queries аа-яя (900 combos) + vowel-led Cyrillic triples.")
    print(f"       Checkpoint : {CYR_CHECKPOINT}")
    print(f"       Output     : {CYR_OUTPUT}  (all prev + Cyrillic merged)")
    print()
    print("  [4]  ID scrape  ⚠  can be very long depending on id_range")
    print(f"       Tries every ID from {ID_START:,} to {ID_END:,}.")
    print(f"       Checkpoint : {ID_CHECKPOINT}  (saves every {ID_BATCH_SAVE} IDs)")
    print(f"       Output     : {ID_OUTPUT}  (all prev + ID results merged)")
    print()
    while True:
        choice = input("  Enter 1, 2, 3 or 4: ").strip()
        if choice in ("1", "2", "3", "4"):
            return int(choice)
        print("  Please type 1, 2, 3 or 4.")


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════

async def main() -> None:
    print("=" * 56)
    print(f"  {SITE_NAME} Torrent Scraper  v6")
    print("=" * 56)
    print("  Scrapes the site via its JSON API using a real")
    print("  Chromium session so Cloudflare cookies are included.")
    print("  Deduplicates by external_id — no duplicates ever.")
    print("  Auto-saves progress — every mode is fully resumable.")
    print(f"  Site config loaded from: {CONFIG_PATH.name}")
    print("=" * 56)
    print()

    mode = pick_mode()

    mode_names = {1: "Latin search", 2: "Category browse",
                  3: "Cyrillic search", 4: "ID scrape"}
    print()
    print("=" * 56)
    print(f"  Mode : {mode_names[mode]}")
    print("=" * 56 + "\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()
        await wait_for_login(page)

        if mode == 1:
            await run_search_scrape(page)
        elif mode == 2:
            await run_category_scrape(page)
        elif mode == 3:
            await run_cyrillic_scrape(page)
        else:
            await run_id_scrape(page)

        await browser.close()

    print(f"\n{'=' * 56}")
    print("  All done!")
    print(f"{'=' * 56}")


if __name__ == "__main__":
    asyncio.run(main())
