import sqlite3
import os

# PO_STATS_DB lets Docker keep the database on a volume; defaults to the app folder
DB_PATH = os.environ.get('PO_STATS_DB') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'po_stats.db')


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    # WAL lets the dashboard keep reading while an import is writing
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE NOT NULL,
            imported_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            po TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created INTEGER,
            finished INTEGER DEFAULT 0,
            finished_at INTEGER,
            source_file TEXT,
            is_archive INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS order_tags (
            order_id INTEGER NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (order_id, tag),
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            start INTEGER NOT NULL,
            duration INTEGER NOT NULL DEFAULT 0,
            cnf INTEGER DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Orders deleted on the dashboard. Imports skip these ids, so an order
        -- stays deleted while the app's exports still contain it. data holds
        -- the order, its tags and sessions as JSON, for Restore.
        CREATE TABLE IF NOT EXISTS deleted_orders (
            id INTEGER PRIMARY KEY,
            po TEXT NOT NULL,
            notes TEXT DEFAULT '',
            total_ms INTEGER DEFAULT 0,
            deleted_at TEXT DEFAULT (datetime('now', 'localtime')),
            data TEXT NOT NULL
        );

        -- Days off marked in the phone app, from the newest export. A row
        -- without from_time/to_time is a whole day; with them, only that part.
        CREATE TABLE IF NOT EXISTS days_off (
            date TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'other',
            from_time TEXT,
            to_time TEXT,
            note TEXT DEFAULT ''
        );
    ''')
    # Change detection for re-importing updated files (older databases lack these)
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(imports)')}
    for name, decl in (('mtime_ns', 'INTEGER'), ('size', 'INTEGER'), ('ok', 'INTEGER DEFAULT 1')):
        if name not in cols:
            conn.execute(f'ALTER TABLE imports ADD COLUMN {name} {decl}')
    conn.commit()
    conn.close()
