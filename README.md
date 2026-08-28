## Torrent Site Scraper (config-driven)

A Playwright-based scraper that logs into a torrent site through a
real Chromium browser (so Cloudflare and login sessions work
normally), then pulls torrent metadata from the site's JSON search
API across four modes: Latin search, category browse, Cyrillic
search, and full ID sweep.

Ships pre-configured for *zamunda.rip* and works out of the box.
All site specific details are in config.json, so it can be pointed
at a different torrent site with a similar JSON API by editing that
file, so no Python changes are required for compatible sites.

---

## Requirements

- Python 3.10+
- Google Chrome/Chromium (installed automatically by Playwright below)

Install the Python dependency:

```
pip install -r requirements.txt
playwright install chromium
```

---

## Using it as-is (default: zamunda.rip)

No configuration needed. `config.json` in this folder is already filled
in for zamunda.rip.

1. Run the scraper:

   ```
   python zamunda_scraper_v6.py
   ```

2. A Chromium window opens. Solve any Cloudflare issues (if there are any) and log in to the site normally,then return to the terminal and press ENTER.

3. Pick a mode when prompted:
4. 
| Mode | What it does | Output file |
|---|---|---|
| **1 — Latin search** | Searches `aa`–`zz` + common keywords +
3-letter combos | `zamunda_torrents.json` |
| **2 — Category browse** | Walks the category endpoints
defined in `config.json` | `zamunda_final.json` |
| **3 — Cyrillic search** | Searches `аа`–`яя` + Cyrillic
3-letter combos (Bulgaria-specific) |
`zamunda_cyrillic_final.json` |
| **4 — ID scrape** | Tries every ID in the configured range
directly | `zamunda_id_final.json` |

Each mode builds on the ones before it — mode 2 reads mode 1's
results to avoid duplicates, mode 3 reads modes 1+2, mode 4
reads modes 1+2+3. Run them in order (1 → 2 → 3 → 4) for full
coverage, or run only the ones you need.

4. Stopping and resuming: every mode saves a checkpoint file after
each completed query/category/ID-batch. If you stop the script
or it crashes, just run it again and pick the same mode, it
automatically skips everything already done and continues from
where it left off. Nothing is ever re-downloaded or duplicated.

5. Rate limiting: a small random delay (0.2–0.4s by default) is
added between requests. Mode 4 (ID scrape) can take a long time
depending on the size of the configured ID range — it's
designed to be safely interruptible at any point.

### Files this produces

| File | Purpose |
|---|---|
| `zamunda_checkpoint.json` | Mode 1 progress + results |
| `zamunda_category_checkpoint.json` | Mode 2 progress + new
results |
| `zamunda_cyrillic_checkpoint.json` | Mode 3 progress + new
results |
| `zamunda_id_checkpoint.json` | Mode 4 progress + new results |
| `zamunda_torrents.json` | Mode 1 final output |
| `zamunda_final.json` | Mode 2 final output (modes 1+2 merged) |
| `zamunda_cyrillic_final.json` | Mode 3 final output (modes
1+2+3 merged) |
| `zamunda_id_final.json` | Mode 4 final output (modes 1+2+3+4
merged - the complete archive) |

Checkpoint files are read on startup; the `*_final.json` /
`zamunda_torrents.json` output files are write-only and never read
back in.

---

## Tweaking the config

Everything site-specific lives in `config.json`. To adapt this
scraper to a different torrent site with a comparable JSON search
API, edit the fields below — no Python code changes needed.

```json
{
"site_name": "Zamunda",
"base_url": "https://zamunda.rip",
"api_path": "/api/torrents",
"query_param": "q",
"offset_param": "offset",
"page_size": 50,

"category_flags": {
 "bg_audio": false,
 "bg_movies": false,
 "bg_arena": false,
 "zelka": false
},
"id_scrape_flags": {
 "bg_audio": true,
 "bg_movies": true,
 "bg_arena": true,
 "zelka": true
},
"categories": [
 { "label": "...", "bg_audio": true, ... }
],

"field_map": {
 "external_id": "external_id",
 "title": "title",
 "category": "category",
 "size": "size",
 "description": "description",
 "source": "source",
 "is_bgaudio": "is_bgaudio",
 "magnet": "link"
},

"response_wrapper_keys": [
 "torrents", "results", "data", "items"
],
"id_range": { "start": 100000, "end": 860000 },
"extra_seeds": ["2024", "hd", "bluray", "..."]
}

Field-by-field:
-site_name — display label only, shown in the terminal banner.
-base_url — the site's root URL. Used both to open the login
-page and as the prefix for the API endpoint.
-api_path — appended to base_url to form the full API
endpoint (e.g. /api/torrents →
https://zamunda.rip/api/torrents).
-query_param / offset_param — the exact query-string
parameter names the site's API expects for the search term and
pagination offset. If the target site uses term=foo&skip=50
instead of ?q=foo&offset=50, set these to "term" and
"skip".
-page_size — how many results the API returns per page; used
to increment the offset between requests.
-category_flags — the "all off" baseline flags sent with every
Latin/Cyrillic search query. Keys must match what the target API
expects; all should default to false here.
-id_scrape_flags — the flags sent during mode 4 (ID scrape).
Typically all true so an exact-ID query returns a match
regardless of category.
-categories — the list mode 2 walks. Each entry needs a
"label" (just for display) plus the flag values for that
category. Add, remove, or rename entries freely to match the
target site's categories.
-field_map — maps the scraper's internal field names (left
side, don't change these) to whatever the target API actually
calls them (right side, edit freely). For example, if a site's
API returns "name" instead of "title" and "magnet_url"
instead of "link", update just those two values.
-response_wrapper_keys — if the API wraps its results in an
object instead of returning a bare array (e.g.
{"results": [...]} instead of [...]), list every key the
response might use here. The scraper checks each one in order.
-id_range — the start/end bounds for mode 4's exhaustive ID
sweep. Set these to match the target site's known ID range.
-extra_seeds — extra keyword queries appended to the generated
aa–zz combos in mode 1 (years, formats, common terms).
Freely editable list of strings.

Not configurable: mode 3 (Cyrillic search) is intentionally
hardcoded to the Bulgarian alphabet, since it's specific to
Bulgarian torrent trackers. If the target site isn't Bulgarian,
simply don't use mode 3 — modes 1, 2, and 4 remain fully
config-driven and site-agnostic.


Tip: run mode 1 for a couple of queries first after changing
the config - the script prints the first API response's raw
structure (keys and types) to the terminal, which is the fastest
way to confirm your field_map and response_wrapper_keys are
correct before committing to a long run.
