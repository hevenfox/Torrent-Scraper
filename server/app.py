"""
Zamunda Archive — Local Web App
================================
Requirements:
    pip install flask werkzeug

Usage:
    1. Put zamunda_id_final.json in the same folder as this file
    2. python app.py
    3. Open http://localhost:5000 in your browser

First run creates:
    - zamunda.db  (SQLite database with users + torrent edits)
    - An admin account: username=admin  password=admin123
      (change this after first login via Settings)
"""

import json
import os
import sqlite3
import hashlib
import secrets
from functools import wraps
from pathlib import Path
from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, g)

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

BASE_DIR   = Path(__file__).parent
DB_PATH    = BASE_DIR / "zamunda.db"
JSON_PATH  = BASE_DIR / "zamunda_final.json"

# ── In-memory torrent store (loaded once at startup) ─────────────────────────
TORRENTS: list[dict] = []
TORRENT_INDEX: dict  = {}   # external_id -> position in TORRENTS list
CATEGORIES: list[str] = []


def load_torrents():
    global TORRENTS, TORRENT_INDEX, CATEGORIES
    if not JSON_PATH.exists():
        print(f"⚠  {JSON_PATH} not found — starting with empty catalogue.")
        return
    print(f"Loading {JSON_PATH} ...")
    with JSON_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    TORRENTS = data
    TORRENT_INDEX = {str(t.get("external_id", "")): i
                     for i, t in enumerate(TORRENTS)
                     if t.get("external_id") is not None}
    cats = set()
    for t in TORRENTS:
        c = t.get("category")
        if c:
            cats.add(str(c))
    CATEGORIES = sorted(cats)
    print(f"Loaded {len(TORRENTS):,} torrents, {len(CATEGORIES)} categories.")


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            username  TEXT UNIQUE NOT NULL,
            password  TEXT NOT NULL,
            is_admin  INTEGER DEFAULT 0,
            language  TEXT DEFAULT 'en',
            theme     TEXT DEFAULT 'dark',
            created   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS torrent_edits (
            external_id  TEXT PRIMARY KEY,
            title        TEXT,
            category     TEXT,
            size         TEXT,
            description  TEXT,
            source       TEXT,
            is_bgaudio   INTEGER,
            magnet       TEXT,
            edited_by    TEXT,
            edited_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS hidden_torrents (
            external_id  TEXT PRIMARY KEY,
            hidden_by    TEXT,
            hidden_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS hidden_categories (
            category     TEXT PRIMARY KEY,
            hidden_by    TEXT,
            hidden_at    TEXT DEFAULT (datetime('now'))
        );
    """)
    # Create default admin if not exists
    pw = hash_password("admin123")
    db.execute("""
        INSERT OR IGNORE INTO users (username, password, is_admin)
        VALUES ('admin', ?, 1)
    """, (pw,))
    db.commit()
    db.close()
    print("Database ready.")


def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        if not session.get("is_admin"):
            return jsonify({"error": "Admin only"}), 403
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?",
                      (session["user_id"],)).fetchone()


# ── Hidden sets (reloaded on each request — fast since DB is local) ───────────

def get_hidden():
    """Return (hidden_torrent_ids: set, hidden_categories: set)."""
    db   = get_db()
    hids = set(r[0] for r in db.execute("SELECT external_id FROM hidden_torrents").fetchall())
    hcats = set(r[0] for r in db.execute("SELECT category FROM hidden_categories").fetchall())
    return hids, hcats


# ── Apply any admin edits to a torrent dict ───────────────────────────────────

def apply_edits(torrent: dict) -> dict:
    """Overlay any admin edits saved in the DB onto the raw torrent dict."""
    db  = get_db()
    eid = str(torrent.get("external_id", ""))
    row = db.execute("SELECT * FROM torrent_edits WHERE external_id=?",
                     (eid,)).fetchone()
    if not row:
        return torrent
    result = dict(torrent)
    for col in ("title", "category", "size", "description",
                "source", "is_bgaudio", "magnet"):
        if row[col] is not None:
            result[col] = row[col]
    return result


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    user = get_current_user()
    if user and user["is_admin"]:
        cats = CATEGORIES
    else:
        _, hcats = get_hidden()
        cats = [c for c in CATEGORIES if c not in hcats]
    return render_template("index.html",
                           user=user,
                           categories=cats,
                           total=len(TORRENTS))


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/register")
def register_page():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/settings")
@login_required
def settings_page():
    user = get_current_user()
    return render_template("settings.html", user=user)


@app.route("/admin")
@login_required
def admin_page():
    if not session.get("is_admin"):
        return redirect(url_for("index"))
    user = get_current_user()
    return render_template("admin.html", user=user, categories=CATEGORIES)


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    db  = get_db()
    row = db.execute("SELECT * FROM users WHERE username=?",
                     (username,)).fetchone()
    if not row or row["password"] != hash_password(password):
        return jsonify({"error": "Invalid username or password"}), 401
    session["user_id"]  = row["id"]
    session["username"] = row["username"]
    session["is_admin"] = bool(row["is_admin"])
    return jsonify({"ok": True, "is_admin": bool(row["is_admin"]),
                    "language": row["language"], "theme": row["theme"]})


@app.route("/api/register", methods=["POST"])
def api_register():
    data     = request.get_json()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 3:
        return jsonify({"error": "Username must be at least 3 characters"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password) VALUES (?,?)",
                   (username, hash_password(password)))
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already taken"}), 409
    row = db.execute("SELECT * FROM users WHERE username=?",
                     (username,)).fetchone()
    session["user_id"]  = row["id"]
    session["username"] = row["username"]
    session["is_admin"] = False
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    data     = request.get_json()
    language = data.get("language", "en")
    theme    = data.get("theme", "dark")
    new_pw   = data.get("new_password", "").strip()
    db       = get_db()
    if new_pw:
        if len(new_pw) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        db.execute("UPDATE users SET language=?, theme=?, password=? WHERE id=?",
                   (language, theme, hash_password(new_pw), session["user_id"]))
    else:
        db.execute("UPDATE users SET language=?, theme=? WHERE id=?",
                   (language, theme, session["user_id"]))
    db.commit()
    return jsonify({"ok": True, "language": language, "theme": theme})


# ── Torrent search API ────────────────────────────────────────────────────────

@app.route("/api/torrents")
def api_torrents():
    q          = (request.args.get("q") or "").strip().lower()
    category   = request.args.get("category", "").strip()
    is_bgaudio = request.args.get("bg_audio", "")
    page       = max(1, int(request.args.get("page", 1)))
    per_page   = 50
    sort_by    = request.args.get("sort", "id_desc")  # id_desc | title_asc | size_desc

    results = TORRENTS

    # Hide torrents and categories from non-admins
    if not session.get("is_admin"):
        hids, hcats = get_hidden()
        if hids or hcats:
            results = [t for t in results
                       if str(t.get("external_id", "")) not in hids
                       and str(t.get("category") or "") not in hcats]

    # Filter by search query (title or exact external_id)
    if q:
        try:
            qid = int(q)
            results = [t for t in results
                       if t.get("external_id") == qid or
                       q in (t.get("title") or "").lower()]
        except ValueError:
            results = [t for t in results
                       if q in (t.get("title") or "").lower()]

    # Filter by category
    if category:
        results = [t for t in results
                   if str(t.get("category") or "") == category]

    # Filter bg audio
    if is_bgaudio == "1":
        results = [t for t in results if t.get("is_bgaudio")]

    # Sort
    if sort_by == "title_asc":
        results = sorted(results, key=lambda t: (t.get("title") or "").lower())
    elif sort_by == "size_desc":
        def parse_size(t):
            try:
                return float(str(t.get("size") or "0").split()[0])
            except Exception:
                return 0.0
        results = sorted(results, key=parse_size, reverse=True)
    else:  # id_desc (default — newest first)
        results = sorted(results,
                         key=lambda t: t.get("external_id") or 0,
                         reverse=True)

    total   = len(results)
    start   = (page - 1) * per_page
    page_items = results[start:start + per_page]

    # Apply any admin edits
    page_items = [apply_edits(t) for t in page_items]

    return jsonify({
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "torrents": page_items,
    })


@app.route("/api/torrent/<external_id>")
def api_torrent_detail(external_id):
    idx = TORRENT_INDEX.get(str(external_id))
    if idx is None:
        return jsonify({"error": "Not found"}), 404
    t = apply_edits(TORRENTS[idx])
    return jsonify(t)


# ── Admin edit API ────────────────────────────────────────────────────────────

@app.route("/api/admin/torrent/<external_id>", methods=["POST"])
@admin_required
def api_admin_edit(external_id):
    data = request.get_json()
    db   = get_db()
    db.execute("""
        INSERT INTO torrent_edits
            (external_id, title, category, size, description,
             source, is_bgaudio, magnet, edited_by)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(external_id) DO UPDATE SET
            title=excluded.title,
            category=excluded.category,
            size=excluded.size,
            description=excluded.description,
            source=excluded.source,
            is_bgaudio=excluded.is_bgaudio,
            magnet=excluded.magnet,
            edited_by=excluded.edited_by,
            edited_at=datetime('now')
    """, (
        str(external_id),
        data.get("title"),
        data.get("category"),
        data.get("size"),
        data.get("description"),
        data.get("source"),
        1 if data.get("is_bgaudio") else 0,
        data.get("magnet"),
        session.get("username"),
    ))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/torrent/<external_id>", methods=["DELETE"])
@admin_required
def api_admin_delete_edit(external_id):
    """Remove any overrides — restores the original JSON data."""
    db = get_db()
    db.execute("DELETE FROM torrent_edits WHERE external_id=?", (str(external_id),))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/users")
@admin_required
def api_admin_users():
    db   = get_db()
    rows = db.execute(
        "SELECT id, username, is_admin, language, theme, created FROM users"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/user/<int:uid>", methods=["POST"])
@admin_required
def api_admin_edit_user(uid):
    data = request.get_json()
    db   = get_db()
    if "is_admin" in data:
        db.execute("UPDATE users SET is_admin=? WHERE id=?",
                   (1 if data["is_admin"] else 0, uid))
    if "password" in data and data["password"]:
        db.execute("UPDATE users SET password=? WHERE id=?",
                   (hash_password(data["password"]), uid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/user/<int:uid>", methods=["DELETE"])
@admin_required
def api_admin_delete_user(uid):
    if uid == session.get("user_id"):
        return jsonify({"error": "Cannot delete yourself"}), 400
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    return jsonify({"ok": True})


# ── Hide / unhide API ────────────────────────────────────────────────────────

@app.route("/api/admin/hide/torrent/<external_id>", methods=["POST"])
@admin_required
def api_hide_torrent(external_id):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO hidden_torrents (external_id, hidden_by) VALUES (?,?)",
               (str(external_id), session.get("username")))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/hide/torrent/<external_id>", methods=["DELETE"])
@admin_required
def api_unhide_torrent(external_id):
    db = get_db()
    db.execute("DELETE FROM hidden_torrents WHERE external_id=?", (str(external_id),))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/hide/category", methods=["POST"])
@admin_required
def api_hide_category():
    category = (request.get_json() or {}).get("category", "").strip()
    if not category:
        return jsonify({"error": "No category"}), 400
    db = get_db()
    db.execute("INSERT OR IGNORE INTO hidden_categories (category, hidden_by) VALUES (?,?)",
               (category, session.get("username")))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/hide/category", methods=["DELETE"])
@admin_required
def api_unhide_category():
    category = (request.get_json() or {}).get("category", "").strip()
    if not category:
        return jsonify({"error": "No category"}), 400
    db = get_db()
    db.execute("DELETE FROM hidden_categories WHERE category=?", (category,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/hidden")
@admin_required
def api_hidden_list():
    """Return currently hidden torrent IDs and categories."""
    db    = get_db()
    trows = db.execute("SELECT external_id, hidden_by, hidden_at FROM hidden_torrents").fetchall()
    crows = db.execute("SELECT category, hidden_by, hidden_at FROM hidden_categories").fetchall()
    return jsonify({
        "torrents":   [dict(r) for r in trows],
        "categories": [dict(r) for r in crows],
    })


# ── Stats API ─────────────────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    bg_count = sum(1 for t in TORRENTS if t.get("is_bgaudio"))
    return jsonify({
        "total":      len(TORRENTS),
        "categories": len(CATEGORIES),
        "bg_audio":   bg_count,
    })


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    load_torrents()
    print("\n  Zamunda Archive running at http://localhost:5000\n")
    app.run(debug=False, host="127.0.0.1", port=5000)
