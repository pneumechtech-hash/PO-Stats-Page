import json
import os
import glob

from db import get_db

WATCH_FOLDER = os.path.dirname(os.path.abspath(__file__))


def ingest_folder():
    conn = get_db()
    imported = []
    skipped = []
    errors = []

    pattern = os.path.join(WATCH_FOLDER, 'po-tracker-*.json')
    # Sort alphabetically so YYYY-MM-DD order = chronological
    files = sorted(glob.glob(pattern))

    for filepath in files:
        filename = os.path.basename(filepath)

        existing = conn.execute(
            'SELECT id FROM imports WHERE filename = ?', (filename,)
        ).fetchone()

        if existing:
            skipped.append(filename)
            continue

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            _import_orders(conn, data.get('orders', []), filename, is_archive=False)
            _import_orders(conn, data.get('archive', []), filename, is_archive=True)

            conn.execute('INSERT INTO imports (filename) VALUES (?)', (filename,))
            conn.commit()
            imported.append(filename)
        except Exception as e:
            conn.rollback()
            errors.append({'file': filename, 'error': str(e)})

    conn.close()
    return {'imported': imported, 'skipped': skipped, 'errors': errors}


def _import_orders(conn, orders, source_file, is_archive):
    for order in orders:
        order_id = order['id']

        conn.execute('''
            INSERT OR REPLACE INTO orders
                (id, po, notes, created, finished, finished_at, source_file, is_archive)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order_id,
            order.get('po', ''),
            order.get('notes', ''),
            order.get('created'),
            1 if order.get('finished') else 0,
            order.get('finishedAt'),
            source_file,
            1 if is_archive else 0,
        ))

        # INSERT OR REPLACE cascades a delete of child rows, so these are clean inserts
        for tag in order.get('tags', []):
            conn.execute(
                'INSERT OR IGNORE INTO order_tags (order_id, tag) VALUES (?, ?)',
                (order_id, tag)
            )

        for session in order.get('sessions', []):
            conn.execute('''
                INSERT INTO sessions (order_id, start, duration, cnf)
                VALUES (?, ?, ?, ?)
            ''', (
                order_id,
                session.get('start'),
                session.get('duration', 0),
                1 if session.get('cnf') else 0,
            ))
