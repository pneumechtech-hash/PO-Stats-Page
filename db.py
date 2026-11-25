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
    ''')
    # Change detection for re-importing updated files (older databases lack these)
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(imports)')}
    for name, decl in (('mtime_ns', 'INTEGER'), ('size', 'INTEGER'), ('ok', 'INTEGER DEFAULT 1')):
        if name not in cols:
            conn.execute(f'ALTER TABLE imports ADD COLUMN {name} {decl}')
    conn.commit()
    conn.close()
