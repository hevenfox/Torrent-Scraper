# zArhiv Archive — Local Web App

A fast local site to browse, search and manage your torrent archive.

## Features
- Filter by category, BG Audio, sort by newest / title / size
- Search by title OR paste an exact torrent ID
- Click any torrent to see full details and open/copy the magnet link
- User accounts with register / login
- Per-user settings: language (EN / BG) and theme (dark / light)
- Admin panel: edit any torrent field, manage users

## Setup

### 1. Install Python dependencies
```
pip install flask werkzeug
```

### 2. Put your data file in the same folder as app.py
The app expects:
```
zamunda_id_final.json   ← your scraped torrent data
app.py
```

### 3. Run the app
```
python app.py
```

### 4. Open in your browser
```
http://localhost:5000
```

### 5. Default admin account
- Username: `admin`
- Password: `admin123`

**Change this immediately** after first login via the Settings page.

## File structure
```
zamunda_web/
├── app.py                  ← Flask backend
├── requirements.txt
├── zamunda_id_final.json   ← your data (copy here)
├── zamunda.db              ← created automatically on first run
└── templates/
    ├── base.html
    ├── index.html          ← browse page
    ├── login.html
    ├── register.html
    ├── settings.html
    └── admin.html
```

## Notes
- The torrent data is loaded into memory at startup for fast searching.
  200k torrents uses roughly 200–400 MB of RAM depending on field sizes.
- All torrent edits made in the admin panel are stored in `zamunda.db`
  and overlaid on top of the JSON data — the original file is never modified.
- To stop the server press Ctrl+C in the terminal.
