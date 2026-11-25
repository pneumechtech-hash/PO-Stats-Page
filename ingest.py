import fnmatch
import json
import os
import stat
import threading

from db import get_db

# PO_STATS_IMPORT_DIR points Docker at the synced folder; defaults to the app folder
WATCH_FOLDER = os.environ.get('PO_STATS_IMPORT_DIR') or os.path.dirname(os.path.abspath(__file__))
PATTERN = 'po-tracker-*.json'
IN_DOCKER = os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')  # Docker or Podman

_lock = threading.Lock()  # one import at a time (page load + button can overlap)
_last_problem = object()  # last folder problem written to the log, so it isn't repeated


def ingest_folder():
    """Imports new or changed po-tracker-*.json files.

    Every file is a full snapshot of the app, and the app's auto-export keeps
    rewriting the current day's file, so a file is re-imported whenever its
    size or modified time changes. Files are applied oldest to newest, and
    once one has changed every newer file is applied again after it, so the
    newest snapshot always has the final say.

    The result's 'problem' says in plain words why nothing could be imported
    (folder missing, empty or unreadable), so the page and the log can show it.
    """
    with _lock:
        result = _ingest()
        _log(result)
        return result


def _access_hint(path):
    """Who the app runs as versus who owns path, for permission errors."""
    if not hasattr(os, 'getuid'):
        return ''
    try:
        st = os.stat(path)
    except OSError:
        return ''
    return (f' The app runs as user {os.getuid()}, but {path} is owned by user {st.st_uid} '
            f'with permissions {stat.filemode(st.st_mode)}. See "Permissions" in the README.')


def _list_exports():
    """Returns (files, stats, errors, problem) for the export files in WATCH_FOLDER, oldest first."""
    volume_hint = ' Check the import folder volume in docker-compose.yml.' if IN_DOCKER else ''
    try:
        names = os.listdir(WATCH_FOLDER)
    except FileNotFoundError:
        return [], {}, [], f"The import folder {WATCH_FOLDER} doesn't exist.{volume_hint}"
    except NotADirectoryError:
        return [], {}, [], f'The import path {WATCH_FOLDER} is a file, not a folder.{volume_hint}'
    except PermissionError:
        return [], {}, [], f'No permission to read the import folder {WATCH_FOLDER}.{_access_hint(WATCH_FOLDER)}'

    files, stats, errors = [], {}, []
    # Sort alphabetically so YYYY-MM-DD order = chronological. Syncthing's
    # conflict copies are older versions of a file, so they're left out.
    for name in sorted(names):
        if not fnmatch.fnmatch(name, PATTERN) or '.sync-conflict-' in name:
            continue
        path = os.path.join(WATCH_FOLDER, name)
        try:
            st = os.stat(path)
        except OSError as e:
            errors.append({'file': name, 'error': f'Could not read it ({e.strerror}).{_access_hint(WATCH_FOLDER)}'})
            continue
        if stat.S_ISREG(st.st_mode):
            files.append(path)
            stats[path] = (st.st_mtime_ns, st.st_size)

    problem = None
    if not files and not errors:
        if names:
            shown = ', '.join(sorted(names)[:6]) + (f' and {len(names) - 6} more' if len(names) > 6 else '')
            problem = f'No {PATTERN} files in the import folder {WATCH_FOLDER}. It contains: {shown}.{volume_hint}'
        else:
            problem = f'The import folder {WATCH_FOLDER} is empty.{volume_hint}'
    return files, stats, errors, problem


def _ingest():
    files, stats, errors, problem = _list_exports()
    imported = []   # new or changed since last import
    reapplied = []  # unchanged, re-applied to keep newest-last order

    conn = get_db()
    known = {r['filename']: (r['mtime_ns'], r['size'])
             for r in conn.execute('SELECT filename, mtime_ns, size FROM imports')}

    changed = [i for i, fp in enumerate(files) if known.get(os.path.basename(fp)) != stats[fp]]
    start = changed[0] if changed else len(files)
    skipped = [os.path.basename(fp) for fp in files[:start]]

    for i, filepath in enumerate(files[start:], start):
        filename = os.path.basename(filepath)
        mtime_ns, size = stats[filepath]
        ok = 0
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            _import_orders(conn, data.get('orders', []), filename, is_archive=False)
            _import_orders(conn, data.get('archive', []), filename, is_archive=True)
            ok = 1
            (imported if i in changed else reapplied).append(filename)
        except OSError as e:
            # Couldn't open it (e.g. permissions). Not recorded, so it's tried
            # again next time — fixing permissions doesn't change the file.
            conn.rollback()
            errors.append({'file': filename, 'error': f'Could not read it ({e.strerror}).{_access_hint(filepath)}'})
            continue
        except Exception as e:
            conn.rollback()
            errors.append({'file': filename, 'error': str(e)})

        # Recorded even when the contents are broken, so a bad file isn't retried until it changes
        conn.execute('''
            INSERT INTO imports (filename, mtime_ns, size, ok, imported_at)
            VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT(filename) DO UPDATE SET
                mtime_ns = excluded.mtime_ns, size = excluded.size,
                ok = excluded.ok, imported_at = excluded.imported_at
        ''', (filename, mtime_ns, size, ok))
        conn.commit()

    conn.close()
    return {'imported': imported, 'reapplied': reapplied, 'skipped': skipped, 'errors': errors,
            'folder': WATCH_FOLDER, 'found': len(files), 'problem': problem}


def _log(r):
    """Writes what an import did to stdout (the container log)."""
    global _last_problem
    if r['problem'] != _last_problem:
        _last_problem = r['problem']
        print(f"Import: {r['problem']}" if r['problem']
              else f"Import folder {r['folder']}: {r['found']} export file(s) found", flush=True)
    if r['imported']:
        print(f"Imported {len(r['imported'])} file(s): {', '.join(r['imported'])}", flush=True)
    for e in r['errors']:
        print(f"Could not import {e['file']}: {e['error']}", flush=True)


def _import_orders(conn, orders, source_file, is_archive):
    for order in orders:
        order_id = order['id']

        # Explicitly delete child rows before upsert — ON DELETE CASCADE is unreliable
        # with Python's sqlite3 implicit transaction handling.
        conn.execute('DELETE FROM sessions WHERE order_id = ?', (order_id,))
        conn.execute('DELETE FROM order_tags WHERE order_id = ?', (order_id,))

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
