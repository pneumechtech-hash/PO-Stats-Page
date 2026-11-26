import csv
import datetime
import io
import json
import os
import statistics
import threading
import time
from collections import namedtuple

from flask import Flask, Response, jsonify, render_template, request

import settings as cfg
from db import get_db, init_db
from ingest import import_lock, ingest_folder

app = Flask(__name__)

# Runs on import so a production server (gunicorn in Docker) gets the schema too
init_db()

# Seconds between background checks of the import folder (0 turns them off)
IMPORT_EVERY = int(os.environ.get('PO_STATS_IMPORT_EVERY', '60'))


def _import_loop():
    """Imports at startup, then keeps checking the import folder, so the data
    (and the Homepage widget) stay current while nobody has the page open.
    The first round also tells the container log whether the folder is readable."""
    while True:
        try:
            ingest_folder()
        except Exception as e:  # keep going; the next round tries again
            print(f'Import failed: {e}', flush=True)
        time.sleep(IMPORT_EVERY)


# Under gunicorn only: `python app.py` runs Flask's reloader, which loads this
# file twice; there the page's own import checks are enough.
if __name__ != '__main__' and IMPORT_EVERY > 0:
    threading.Thread(target=_import_loop, name='auto-import', daemon=True).start()


# The work day from Settings, in minutes after midnight
Window = namedtuple('Window', 'start end lunch_start lunch_end min_gap')


def _window(s):
    return Window(cfg.to_min(s['work_start']), cfg.to_min(s['work_end']),
                  cfg.to_min(s['lunch_start']), cfg.to_min(s['lunch_end']), s['min_gap_min'])


def _work_days_between(s, frm, to):
    return sum(1 for n in range((to - frm).days + 1)
               if (frm + datetime.timedelta(n)).weekday() in s['work_days'])


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/healthz')
def healthz():
    get_db().close()
    return 'ok'


@app.route('/api/import', methods=['POST'])
def do_import():
    result = ingest_folder()
    return jsonify(result)


@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(cfg.load())


@app.route('/api/settings', methods=['PUT'])
def put_settings():
    try:
        return jsonify(cfg.update(request.get_json(silent=True)))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/summary')
def summary():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')
    conn = get_db()

    if from_str and to_str:
        tag_times = conn.execute('''
            SELECT ot.tag, COALESCE(SUM(s.duration), 0) AS total_ms
            FROM order_tags ot
            JOIN sessions s ON s.order_id = ot.order_id
            WHERE date(s.start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
            GROUP BY ot.tag
            ORDER BY total_ms DESC
        ''', (from_str, to_str)).fetchall()
        day_times = conn.execute('''
            SELECT date(start / 1000, 'unixepoch', 'localtime') AS day,
                   COALESCE(SUM(duration), 0) AS total_ms
            FROM sessions
            WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
            GROUP BY day ORDER BY day
        ''', (from_str, to_str)).fetchall()
    else:
        tag_times = conn.execute('''
            SELECT ot.tag, COALESCE(SUM(s.duration), 0) AS total_ms
            FROM order_tags ot
            LEFT JOIN sessions s ON s.order_id = ot.order_id
            GROUP BY ot.tag ORDER BY total_ms DESC
        ''').fetchall()
        day_times = conn.execute('''
            SELECT date(start / 1000, 'unixepoch', 'localtime') AS day,
                   COALESCE(SUM(duration), 0) AS total_ms
            FROM sessions GROUP BY day ORDER BY day
        ''').fetchall()

    conn.close()
    return jsonify({
        'tag_times': [{'tag': r['tag'], 'total_ms': r['total_ms']} for r in tag_times],
        'day_times': [{'day': r['day'], 'total_ms': r['total_ms']} for r in day_times],
    })


@app.route('/api/gaps')
def gaps():
    from_str = request.args.get('from') or request.args.get('date')
    to_str   = request.args.get('to', from_str)

    conn = get_db()
    w = _window(cfg.load(conn))
    now = datetime.datetime.now()

    # Only iterate dates that actually have sessions — avoids looping through
    # thousands of empty calendar days when the range is very wide or unset.
    if from_str and to_str:
        dates = conn.execute('''
            SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day
            FROM sessions
            WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
            ORDER BY day
        ''', (from_str, to_str)).fetchall()
    else:
        dates = conn.execute('''
            SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day
            FROM sessions ORDER BY day
        ''').fetchall()

    results = []
    for row in dates:
        day_data = _compute_day_gaps(conn, row['day'], w, now)
        if day_data['has_sessions']:
            day_data['sessions'] = _get_day_sessions(conn, row['day'])
        results.append(day_data)
    conn.close()

    days_with_data  = [d for d in results if d['has_sessions']]
    total_gap_min   = sum(d['total_gap_min'] for d in days_with_data)
    avg_gap_min     = round(total_gap_min / len(days_with_data)) if days_with_data else 0
    total_worked_min = sum(d.get('total_worked_min', 0) for d in days_with_data)

    return jsonify({
        'days': results,
        'summary': {
            'days_with_data':  len(days_with_data),
            'total_gap_min':   total_gap_min,
            'avg_gap_min':     avg_gap_min,
            'total_worked_min': total_worked_min,
        },
    })


def _get_day_sessions(conn, date_str):
    rows = conn.execute('''
        SELECT s.start, s.duration, o.po, o.notes
        FROM sessions s
        JOIN orders o ON o.id = s.order_id
        WHERE date(s.start / 1000, 'unixepoch', 'localtime') = ?
          AND s.duration >= 1000
        ORDER BY s.start
    ''', (date_str,)).fetchall()

    result = []
    for r in rows:
        s_dt = datetime.datetime.fromtimestamp(r['start'] / 1000)
        e_dt = datetime.datetime.fromtimestamp((r['start'] + r['duration']) / 1000)
        result.append({
            'po':           r['po'],
            'notes':        r['notes'] or '',
            'start':        s_dt.strftime('%H:%M'),
            'end':          e_dt.strftime('%H:%M'),
            'duration_min': round(r['duration'] / 60000),
        })
    return result


def _compute_day_gaps(conn, date_str, w, now=None):
    """Inactive gaps in one day's work window (w), lunch excluded. For today
    (when now is given) gaps only count up to now: the rest of the day hasn't
    happened yet."""
    rows = conn.execute('''
        SELECT start, duration
        FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') = ?
          AND duration >= 1000
        ORDER BY start
    ''', (date_str,)).fetchall()

    gap_end = w.end
    if now is not None and date_str == now.date().isoformat():
        gap_end = max(w.start, min(w.end, now.hour * 60 + now.minute))

    if not rows:
        return {
            'date': date_str, 'has_sessions': False,
            'gaps': [], 'active_intervals': [],
            'day_start': _fmt_min(w.start), 'day_end': _fmt_min(w.end), 'gap_until': _fmt_min(gap_end),
            'total_gap_min': 0,
        }

    # Convert sessions to [start_min, end_min] in local time
    intervals = []
    for s in rows:
        s_dt = datetime.datetime.fromtimestamp(s['start'] / 1000)
        e_dt = datetime.datetime.fromtimestamp((s['start'] + s['duration']) / 1000)
        s_min = s_dt.hour * 60 + s_dt.minute + s_dt.second / 60
        e_min = e_dt.hour * 60 + e_dt.minute + e_dt.second / 60
        intervals.append([s_min, e_min])

    # Merge overlapping intervals
    intervals.sort()
    merged = [intervals[0][:]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    last_end = merged[-1][1]
    # Display window: always starts at the work start; extends if they worked late
    display_end = max(w.end, last_end)

    # Gap analysis is anchored to the work window
    gaps_out = []
    cursor = float(w.start)

    for iv_s, iv_e in merged:
        # Only care about the portion of each session inside the work window
        effective_s = max(iv_s, w.start)
        effective_e = min(iv_e, w.end)

        if effective_e <= w.start:
            continue  # session entirely before work window

        if effective_s > cursor:
            # Capped at the window end: a session after hours doesn't make
            # the time between the end of the day and that session a gap
            _split_gap(cursor, min(effective_s, gap_end), w, gaps_out)

        cursor = max(cursor, min(iv_e, w.end))

        if cursor >= w.end:
            break

    # Gap from last tracked activity to end of work day (left early)
    if cursor < gap_end:
        _split_gap(cursor, gap_end, w, gaps_out)

    return {
        'date': date_str,
        'has_sessions': True,
        'day_start': _fmt_min(w.start),
        'day_end': _fmt_min(display_end),
        'gap_until': _fmt_min(gap_end),
        'first_session': _fmt_min(merged[0][0]),
        'last_session': _fmt_min(last_end),
        'gaps': gaps_out,
        'active_intervals': [{'start': _fmt_min(s), 'end': _fmt_min(e)} for s, e in merged],
        'total_gap_min': sum(g['duration_min'] for g in gaps_out),
        'total_worked_min': round(sum(r['duration'] for r in rows) / 60000),
    }


def _split_gap(gap_start, gap_end, w, out):
    """Add gap segments to out, carving out the lunch window."""
    if gap_end <= gap_start:
        return
    no_lunch = w.lunch_end <= w.lunch_start or gap_end <= w.lunch_start or gap_start >= w.lunch_end
    pieces = ([(gap_start, gap_end)] if no_lunch
              else [(gap_start, min(gap_end, w.lunch_start)), (max(gap_start, w.lunch_end), gap_end)])
    for s, e in pieces:
        if e - s >= w.min_gap and e > s:
            out.append({'start': _fmt_min(s), 'end': _fmt_min(e), 'duration_min': round(e - s)})


def _fmt_min(minutes):
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f'{h:02d}:{m:02d}'


@app.route('/api/worked')
def worked():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')

    conn = get_db()
    s = cfg.load(conn)

    if not from_str or not to_str:
        # All time: derive range from actual session data
        row = conn.execute('''
            SELECT date(MIN(start) / 1000, 'unixepoch', 'localtime') AS min_day,
                   date(MAX(start) / 1000, 'unixepoch', 'localtime') AS max_day
            FROM sessions
        ''').fetchone()
        if row['min_day'] is None:
            conn.close()
            return jsonify({'from': None, 'to': None, 'total_ms': 0, 'working_days': 0, 'target_ms': 0})
        from_str, to_str = row['min_day'], row['max_day']

    total_ms = conn.execute('''
        SELECT COALESCE(SUM(duration), 0)
        FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
    ''', (from_str, to_str)).fetchone()[0]
    active_days = conn.execute('''
        SELECT COUNT(DISTINCT date(start / 1000, 'unixepoch', 'localtime'))
        FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
    ''', (from_str, to_str)).fetchone()[0]
    conn.close()

    working_days = _work_days_between(s, datetime.date.fromisoformat(from_str), datetime.date.fromisoformat(to_str))
    return jsonify({
        'from': from_str, 'to': to_str,
        'total_ms': total_ms,
        'active_days': active_days,   # days that had at least one session
        'working_days': working_days, # work days (from Settings) in the range, used for target
        'target_ms': round(working_days * s['daily_target_h'] * 3_600_000),
    })


@app.route('/api/orders')
def orders():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')
    conn = get_db()

    if from_str and to_str:
        rows = conn.execute('''
            SELECT
                o.id, o.po, o.notes, o.created, o.finished, o.finished_at, o.is_archive,
                GROUP_CONCAT(DISTINCT ot.tag) AS tags,
                COALESCE(ss.total_ms, 0) AS total_ms,
                COALESCE(ss.session_count, 0) AS session_count,
                COALESCE(ss.cnf_count, 0) AS cnf_count,
                COALESCE(ss.pending_count, 0) AS pending_count
            FROM orders o
            LEFT JOIN order_tags ot ON ot.order_id = o.id
            INNER JOIN (
                SELECT order_id,
                       SUM(duration) AS total_ms,
                       COUNT(id) AS session_count,
                       SUM(CASE WHEN cnf = 1 THEN 1 ELSE 0 END) AS cnf_count,
                       SUM(CASE WHEN cnf = 0 AND duration > 0 THEN 1 ELSE 0 END) AS pending_count
                FROM sessions
                WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
                GROUP BY order_id
            ) ss ON ss.order_id = o.id
            GROUP BY o.id
            ORDER BY o.created DESC
        ''', (from_str, to_str)).fetchall()
    else:
        rows = conn.execute('''
            SELECT
                o.id, o.po, o.notes, o.created, o.finished, o.finished_at, o.is_archive,
                GROUP_CONCAT(DISTINCT ot.tag) AS tags,
                COALESCE(ss.total_ms, 0) AS total_ms,
                COALESCE(ss.session_count, 0) AS session_count,
                COALESCE(ss.cnf_count, 0) AS cnf_count,
                COALESCE(ss.pending_count, 0) AS pending_count
            FROM orders o
            LEFT JOIN order_tags ot ON ot.order_id = o.id
            LEFT JOIN (
                SELECT order_id,
                       SUM(duration) AS total_ms,
                       COUNT(id) AS session_count,
                       SUM(CASE WHEN cnf = 1 THEN 1 ELSE 0 END) AS cnf_count,
                       SUM(CASE WHEN cnf = 0 AND duration > 0 THEN 1 ELSE 0 END) AS pending_count
                FROM sessions
                GROUP BY order_id
            ) ss ON ss.order_id = o.id
            GROUP BY o.id
            ORDER BY o.created DESC
        ''').fetchall()
    conn.close()

    return jsonify([{
        'id': r['id'],
        'po': r['po'],
        'notes': r['notes'],
        'created': r['created'],
        'finished': bool(r['finished']),
        'finished_at': r['finished_at'],
        'is_archive': bool(r['is_archive']),
        'tags': r['tags'].split(',') if r['tags'] else [],
        'total_ms': r['total_ms'],
        'session_count': r['session_count'],
        'cnf_count': r['cnf_count'] or 0,
        'pending_count': r['pending_count'] or 0,
    } for r in rows])


def _parse_time(hhmm):
    h, m = map(int, hhmm.split(':'))
    return h * 60 + m


DOW_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


@app.route('/api/patterns')
def patterns():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')
    conn = get_db()
    s = cfg.load(conn)
    w = _window(s)
    now = datetime.datetime.now()

    if from_str and to_str:
        dates = conn.execute('''
            SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day
            FROM sessions
            WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
            ORDER BY day
        ''', (from_str, to_str)).fetchall()
    else:
        dates = conn.execute('''
            SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day
            FROM sessions ORDER BY day
        ''').fetchall()

    SLOT = 30
    in_lunch = lambda slot: w.lunch_start <= slot < w.lunch_end
    slot_counts = {}          # slot_start_min -> number of days where slot was inactive
    dow_gaps    = [[] for _ in range(7)]

    for row in dates:
        day_data = _compute_day_gaps(conn, row['day'], w, now)
        if not day_data['has_sessions']:
            continue

        d = datetime.date.fromisoformat(row['day'])
        dow_gaps[d.weekday()].append(day_data['total_gap_min'])

        # Collect inactive slots for this day first, then count once per slot.
        # Slots are 30-minute steps from the start of the work day.
        inactive_today = set()
        for gap in day_data['gaps']:
            gap_s = _parse_time(gap['start'])
            gap_e = _parse_time(gap['end'])
            slot = w.start + ((gap_s - w.start) // SLOT) * SLOT
            while slot < gap_e:
                if w.start <= slot < w.end and not in_lunch(slot):
                    inactive_today.add(slot)
                slot += SLOT
        for slot in inactive_today:
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

    conn.close()

    total_days = len(dates)
    slots = sorted([
        {
            'label':         f'{_fmt_min(slot)}–{_fmt_min(min(slot + SLOT, w.end))}',
            'inactive_days': slot_counts.get(slot, 0),
            'pct':           round(slot_counts.get(slot, 0) / total_days * 100) if total_days else 0,
        }
        for slot in range(w.start, w.end, SLOT)
        if not in_lunch(slot)
    ], key=lambda x: -x['inactive_days'])

    by_dow = [
        {
            'day':          DOW_NAMES[i],
            'avg_gap_min':  round(sum(dow_gaps[i]) / len(dow_gaps[i])) if dow_gaps[i] else 0,
            'days_tracked': len(dow_gaps[i]),
        }
        for i in s['work_days']
    ]

    return jsonify({
        'total_days': total_days,
        'slots':      slots,
        'by_dow':     by_dow,
    })


@app.route('/api/lifetime')
def lifetime():
    conn = get_db()
    w = _window(cfg.load(conn))
    now = datetime.datetime.now()

    total_orders    = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
    active_orders   = conn.execute('SELECT COUNT(*) FROM orders WHERE finished = 0').fetchone()[0]
    finished_orders = conn.execute('SELECT COUNT(*) FROM orders WHERE finished = 1').fetchone()[0]
    total_ms        = conn.execute('SELECT COALESCE(SUM(duration), 0) FROM sessions').fetchone()[0]

    dates = conn.execute('''
        SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day
        FROM sessions ORDER BY day
    ''').fetchall()

    total_gap_min = sum(_compute_day_gaps(conn, r['day'], w, now)['total_gap_min'] for r in dates)

    min_max = conn.execute('''
        SELECT date(MIN(start) / 1000, 'unixepoch', 'localtime') AS min_date,
               date(MAX(start) / 1000, 'unixepoch', 'localtime') AS max_date
        FROM sessions
    ''').fetchone()

    conn.close()
    return jsonify({
        'total_orders':    total_orders,
        'active_orders':   active_orders,
        'finished_orders': finished_orders,
        'total_ms':        total_ms,
        'total_gap_min':   total_gap_min,
        'working_days':    len(dates),
        'min_date':        min_max['min_date'],
        'max_date':        min_max['max_date'],
    })


@app.route('/api/velocity')
def velocity():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')
    conn = get_db()

    if from_str and to_str:
        rows = conn.execute('''
            SELECT date(finished_at / 1000, 'unixepoch', 'localtime') AS finish_date
            FROM orders
            WHERE finished = 1 AND finished_at IS NOT NULL
              AND date(finished_at / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
            ORDER BY finish_date
        ''', (from_str, to_str)).fetchall()
    else:
        rows = conn.execute('''
            SELECT date(finished_at / 1000, 'unixepoch', 'localtime') AS finish_date
            FROM orders
            WHERE finished = 1 AND finished_at IS NOT NULL
            ORDER BY finish_date
        ''').fetchall()
    conn.close()

    weekly = {}
    for row in rows:
        d = datetime.date.fromisoformat(row['finish_date'])
        week_start = (d - datetime.timedelta(days=d.weekday())).isoformat()
        weekly[week_start] = weekly.get(week_start, 0) + 1

    return jsonify([{'week': k, 'count': v} for k, v in sorted(weekly.items())])


@app.route('/api/orders/<int:order_id>/sessions')
def order_sessions(order_id):
    conn = get_db()
    order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    sessions = conn.execute('''
        SELECT start, duration, cnf FROM sessions
        WHERE order_id = ? AND duration >= 1000
        ORDER BY start
    ''', (order_id,)).fetchall()

    tags = [r['tag'] for r in conn.execute(
        'SELECT tag FROM order_tags WHERE order_id = ?', (order_id,)
    ).fetchall()]
    conn.close()

    result = []
    for s in sessions:
        s_dt = datetime.datetime.fromtimestamp(s['start'] / 1000)
        e_dt = datetime.datetime.fromtimestamp((s['start'] + s['duration']) / 1000)
        result.append({
            'date':         s_dt.strftime('%Y-%m-%d'),
            'start':        s_dt.strftime('%H:%M'),
            'end':          e_dt.strftime('%H:%M'),
            'duration_min': round(s['duration'] / 60000),
            'duration_ms':  s['duration'],
            'cnf':          bool(s['cnf']),
        })

    return jsonify({
        'order': {
            'id': order['id'], 'po': order['po'], 'notes': order['notes'],
            'created': order['created'], 'finished': bool(order['finished']),
            'finished_at': order['finished_at'], 'tags': tags,
        },
        'sessions': result,
    })


ORDER_COLUMNS = ('id', 'po', 'notes', 'created', 'finished', 'finished_at', 'source_file', 'is_archive')


def _remove_order_rows(conn, order_id):
    conn.execute('DELETE FROM sessions WHERE order_id = ?', (order_id,))
    conn.execute('DELETE FROM order_tags WHERE order_id = ?', (order_id,))
    conn.execute('DELETE FROM orders WHERE id = ?', (order_id,))


@app.route('/api/orders/<int:order_id>', methods=['DELETE'])
def delete_order(order_id):
    """Removes an order from the stats. It's remembered in deleted_orders, so
    imports skip it even while the app's exports still contain it, and a copy
    is kept there so Settings can restore it."""
    with import_lock:
        conn = get_db()
        order = conn.execute('SELECT * FROM orders WHERE id = ?', (order_id,)).fetchone()
        if not order:
            conn.close()
            return jsonify({'error': 'That order is no longer in the stats.'}), 404
        tags = [r['tag'] for r in conn.execute('SELECT tag FROM order_tags WHERE order_id = ?', (order_id,))]
        sessions = [dict(r) for r in conn.execute(
            'SELECT start, duration, cnf FROM sessions WHERE order_id = ? ORDER BY start', (order_id,))]
        total_ms = sum(s['duration'] or 0 for s in sessions)
        copy = {'order': {k: order[k] for k in ORDER_COLUMNS}, 'tags': tags, 'sessions': sessions}
        conn.execute('''
            INSERT OR REPLACE INTO deleted_orders (id, po, notes, total_ms, deleted_at, data)
            VALUES (?, ?, ?, ?, datetime('now', 'localtime'), ?)
        ''', (order_id, order['po'], order['notes'] or '', total_ms, json.dumps(copy)))
        _remove_order_rows(conn, order_id)
        conn.commit()
        conn.close()
    return jsonify({'deleted': {'id': order_id, 'po': order['po'], 'notes': order['notes'] or '',
                                'total_ms': total_ms, 'sessions': len(sessions)}})


@app.route('/api/deleted_orders')
def deleted_orders():
    conn = get_db()
    rows = conn.execute('''
        SELECT id, po, notes, total_ms, deleted_at FROM deleted_orders
        ORDER BY deleted_at DESC, id DESC
    ''').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/deleted_orders/<int:order_id>/restore', methods=['POST'])
def restore_order(order_id):
    """Puts a deleted order back as it was when deleted. Imports stop skipping
    it, so the next change to it in the app updates it as usual."""
    with import_lock:
        conn = get_db()
        row = conn.execute('SELECT data FROM deleted_orders WHERE id = ?', (order_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': "That order isn't in the deleted list."}), 404
        copy = json.loads(row['data'])
        o = copy['order']
        _remove_order_rows(conn, order_id)
        conn.execute(f'INSERT INTO orders ({", ".join(ORDER_COLUMNS)}) VALUES ({", ".join("?" * len(ORDER_COLUMNS))})',
                     [o.get(k) for k in ORDER_COLUMNS])
        conn.executemany('INSERT OR IGNORE INTO order_tags (order_id, tag) VALUES (?, ?)',
                         [(order_id, t) for t in copy['tags']])
        conn.executemany('INSERT INTO sessions (order_id, start, duration, cnf) VALUES (?, ?, ?, ?)',
                         [(order_id, s['start'], s['duration'], s['cnf']) for s in copy['sessions']])
        conn.execute('DELETE FROM deleted_orders WHERE id = ?', (order_id,))
        conn.commit()
        conn.close()
    return jsonify({'restored': {'id': order_id, 'po': o['po']}})


def _idle_count(conn, threshold_days, now_s):
    return conn.execute('''
        SELECT COUNT(*) FROM (
            SELECT MAX(s.start) AS last_ms FROM orders o
            LEFT JOIN sessions s ON s.order_id = o.id
            WHERE o.finished = 0 GROUP BY o.id
        ) WHERE last_ms IS NULL OR last_ms < ?
    ''', ((now_s - threshold_days * 86400) * 1000,)).fetchone()[0]


@app.route('/api/idle')
def idle():
    conn = get_db()
    threshold_days = int(request.args.get('days') or cfg.load(conn)['idle_days'])

    rows = conn.execute('''
        SELECT o.id, o.po, o.notes,
               GROUP_CONCAT(DISTINCT ot.tag) AS tags,
               MAX(s.start) AS last_session_ms,
               COUNT(s.id) AS session_count,
               COALESCE(SUM(s.duration), 0) AS total_ms
        FROM orders o
        LEFT JOIN order_tags ot ON ot.order_id = o.id
        LEFT JOIN sessions s ON s.order_id = o.id
        WHERE o.finished = 0
        GROUP BY o.id
        ORDER BY COALESCE(last_session_ms, 0) ASC
    ''').fetchall()
    conn.close()

    now_ms       = datetime.datetime.now().timestamp() * 1000
    threshold_ms = threshold_days * 86_400_000

    results = []
    for r in rows:
        last_ms = r['last_session_ms']
        if last_ms is None or (now_ms - last_ms) > threshold_ms:
            idle_days = None if last_ms is None else round((now_ms - last_ms) / 86_400_000)
            results.append({
                'id':            r['id'],
                'po':            r['po'],
                'notes':         r['notes'],
                'tags':          r['tags'].split(',') if r['tags'] else [],
                'idle_days':     idle_days,
                'session_count': r['session_count'],
                'total_ms':      r['total_ms'],
            })

    return jsonify({'orders': results, 'threshold_days': threshold_days})


# ── Goals, comparisons and to-dos ─────────────────────────────────────────

def _window_passed(w, minute):
    """How much of a work day's window, lunch excluded, has passed at this
    minute of the day: 0 before it starts, 1 once it has ended."""
    def worked(t):
        t = max(w.start, min(w.end, t))
        lunch = max(0, min(t, w.lunch_end) - max(w.start, w.lunch_start))
        return t - w.start - lunch
    total = worked(w.end)
    return worked(minute) / total if total > 0 else float(minute >= w.end)


def _month_progress(conn, s, month, now):
    """Progress toward a month's hour goal, and the pace needed to reach it.
    The goal is the month's own goal, else the default monthly goal, else the
    daily target times the work days in the month.

    Today counts toward the expected hours as its work window passes, so the
    pace line moves through the day, and it stops counting as a day left once
    the work day has ended. now is a datetime (a plain date means midnight)."""
    if not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(now, datetime.time())
    today = now.date()
    y, m = map(int, month.split('-'))
    first = datetime.date(y, m, 1)
    last = datetime.date(y + (m == 12), m % 12 + 1, 1) - datetime.timedelta(days=1)
    work = [first + datetime.timedelta(n) for n in range((last - first).days + 1)
            if (first + datetime.timedelta(n)).weekday() in s['work_days']]
    today_part = (_window_passed(_window(s), now.hour * 60 + now.minute + now.second / 60)
                  if today in work else 0.0)
    days_passed = sum(1 for d in work if d < today) + today_part   # e.g. 18.5 at today's midpoint
    left = len(work) - int(days_passed)    # work days with work still to do, today until it ends

    if month in s['month_goals']:
        goal, source = s['month_goals'][month], 'month'
    elif s['monthly_goal_h']:
        goal, source = s['monthly_goal_h'], 'default'
    else:
        goal, source = s['daily_target_h'] * len(work), 'daily'

    tracked_ms = conn.execute('''
        SELECT COALESCE(SUM(duration), 0) FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
    ''', (first.isoformat(), last.isoformat())).fetchone()[0]
    tracked_h = tracked_ms / 3_600_000
    expected_h = goal * days_passed / len(work) if work else 0   # where steady pace would be by now
    remaining_h = max(0.0, goal - tracked_h)

    return {
        'month': month,
        'goal_h': round(goal, 2),
        'goal_source': source,
        'tracked_ms': tracked_ms,
        'pct': round(tracked_h / goal * 100) if goal else 0,
        'work_days': len(work),
        'work_days_done': len(work) - left,
        'work_days_left': left,
        'expected_h': round(expected_h, 2),
        'ahead_h': round(tracked_h - expected_h, 2),
        'remaining_h': round(remaining_h, 2),
        'per_day_h': round(remaining_h / left, 2) if left else None,
    }


@app.route('/api/goal')
def goal():
    today = datetime.date.today()
    month = request.args.get('month') or today.strftime('%Y-%m')
    try:
        datetime.date.fromisoformat(month + '-01')
    except ValueError:
        return jsonify({'error': 'month must look like 2026-11'}), 400
    conn = get_db()
    result = _month_progress(conn, cfg.load(conn), month, datetime.datetime.now())
    conn.close()
    return jsonify(result)


@app.route('/api/period')
def period():
    """Headline numbers for one date range, so the page can compare two."""
    from_str, to_str = request.args.get('from'), request.args.get('to')
    if not from_str or not to_str:
        return jsonify({'error': 'from and to are required'}), 400
    conn = get_db()
    w = _window(cfg.load(conn))
    now = datetime.datetime.now()
    row = conn.execute('''
        SELECT COALESCE(SUM(duration), 0) AS tracked_ms,
               COUNT(DISTINCT order_id) AS orders
        FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
    ''', (from_str, to_str)).fetchone()
    days = [r['day'] for r in conn.execute('''
        SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
    ''', (from_str, to_str))]
    finished = conn.execute('''
        SELECT COUNT(*) FROM orders
        WHERE finished = 1 AND date(finished_at / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
    ''', (from_str, to_str)).fetchone()[0]
    gap_min = sum(_compute_day_gaps(conn, d, w, now)['total_gap_min'] for d in days)
    conn.close()
    return jsonify({
        'from': from_str, 'to': to_str,
        'tracked_ms': row['tracked_ms'],
        'active_days': len(days),
        'orders': row['orders'],
        'finished': finished,
        'gap_min': gap_min,
    })


@app.route('/api/unconfirmed')
def unconfirmed():
    """Unfinished orders with worked time not yet entered in SAP (sessions
    without CNF), oldest first. Finished orders count as done, whatever their
    sessions say. Not tied to the period filter: it's a to-do list."""
    conn = get_db()
    rows = conn.execute('''
        SELECT o.id, o.po, o.notes, o.finished,
               p.pending_ms, p.pending_count, p.oldest_ms,
               (SELECT GROUP_CONCAT(tag) FROM order_tags WHERE order_id = o.id) AS tags
        FROM orders o
        JOIN (
            SELECT order_id, SUM(duration) AS pending_ms, COUNT(*) AS pending_count,
                   MIN(start) AS oldest_ms
            FROM sessions WHERE cnf = 0 AND duration > 0
            GROUP BY order_id
        ) p ON p.order_id = o.id
        WHERE o.finished = 0
        ORDER BY p.oldest_ms
    ''').fetchall()
    conn.close()
    return jsonify({
        'total_ms': sum(r['pending_ms'] for r in rows),
        'orders': [{
            'id': r['id'], 'po': r['po'], 'notes': r['notes'], 'finished': bool(r['finished']),
            'tags': r['tags'].split(',') if r['tags'] else [],
            'pending_ms': r['pending_ms'], 'pending_count': r['pending_count'], 'oldest_ms': r['oldest_ms'],
        } for r in rows],
    })


@app.route('/api/tag_stats')
def tag_stats():
    """Typical time per finished order, per tag — useful for estimating.
    Uses orders finished in the period (all finished orders without one)."""
    from_str, to_str = request.args.get('from'), request.args.get('to')
    conn = get_db()
    where, args = 'o.finished = 1', ()
    if from_str and to_str:
        where += " AND date(o.finished_at / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?"
        args = (from_str, to_str)
    rows = conn.execute(f'''
        SELECT ot.tag, t.total_ms
        FROM orders o
        JOIN order_tags ot ON ot.order_id = o.id
        JOIN (SELECT order_id, SUM(duration) AS total_ms FROM sessions GROUP BY order_id) t
          ON t.order_id = o.id
        WHERE {where} AND t.total_ms > 0
    ''', args).fetchall()
    conn.close()

    by_tag = {}
    for r in rows:
        by_tag.setdefault(r['tag'], []).append(r['total_ms'])
    stats = [{
        'tag': tag,
        'orders': len(times),
        'median_ms': round(statistics.median(times)),
        'avg_ms': round(sum(times) / len(times)),
        'min_ms': min(times),
        'max_ms': max(times),
    } for tag, times in by_tag.items()]
    stats.sort(key=lambda x: (-x['orders'], x['tag'].lower()))
    return jsonify(stats)


@app.route('/api/timesheet.csv')
def timesheet():
    """Time per day and order as a CSV, for filling in timesheets."""
    from_str, to_str = request.args.get('from'), request.args.get('to')
    conn = get_db()
    where, args = '', ()
    if from_str and to_str:
        where, args = "WHERE date(s.start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?", (from_str, to_str)
    rows = conn.execute(f'''
        SELECT date(s.start / 1000, 'unixepoch', 'localtime') AS day, o.id, o.po, o.notes,
               SUM(s.duration) AS total_ms, COUNT(*) AS sessions,
               SUM(CASE WHEN s.cnf = 1 OR s.duration <= 0 THEN 1 ELSE 0 END) AS done
        FROM sessions s JOIN orders o ON o.id = s.order_id
        {where}
        GROUP BY day, o.id
        ORDER BY day, MIN(s.start)
    ''', args).fetchall()
    tags = {}
    for r in conn.execute('SELECT order_id, tag FROM order_tags ORDER BY tag'):
        tags.setdefault(r['order_id'], []).append(r['tag'])
    conn.close()

    out = io.StringIO()
    out.write('﻿')  # lets Excel read the file as UTF-8
    writer = csv.writer(out)
    writer.writerow(['Date', 'PO', 'Notes', 'Tags', 'Hours', 'Time', 'Sessions', 'In SAP'])
    for r in rows:
        minutes = round(r['total_ms'] / 60000)
        in_sap = 'Yes' if r['done'] == r['sessions'] else 'No' if r['done'] == 0 else 'Partly'
        writer.writerow([r['day'], r['po'], r['notes'] or '', ', '.join(tags.get(r['id'], [])),
                         f"{r['total_ms'] / 3_600_000:.2f}", f'{minutes // 60}:{minutes % 60:02d}',
                         r['sessions'], in_sap])

    name = f'timesheet-{from_str}-to-{to_str}.csv' if from_str and to_str else 'timesheet-all.csv'
    return Response(out.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


# ── Homepage widget ───────────────────────────────────────────────────────

def _fmt_hm(seconds):
    minutes = int(seconds // 60)
    return f'{minutes // 60}h {minutes % 60:02d}m' if minutes >= 60 else f'{minutes}m'


def _fmt_h(hours):
    return f'{hours:.1f}'.rstrip('0').rstrip('.') + 'h'


def _fmt_ago(seconds):
    if seconds < 90:
        return 'just now'
    if seconds < 3600:
        return f'{round(seconds / 60)} min ago'
    if seconds < 48 * 3600:
        return f'{round(seconds / 3600)} h ago'
    return f'{round(seconds / 86400)} days ago'


@app.route('/api/homepage')
def homepage_summary():
    """Flat summary for Homepage's Custom API widget (gethomepage.dev).
    Text fields are ready to show as they are; *_seconds fields suit
    `format: duration`. The week runs Monday to today."""
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    now = time.time()
    conn = get_db()
    s = cfg.load(conn)

    def tracked_seconds(frm, to):
        return conn.execute('''
            SELECT COALESCE(SUM(duration), 0) FROM sessions
            WHERE date(start / 1000, 'unixepoch', 'localtime') BETWEEN ? AND ?
        ''', (frm.isoformat(), to.isoformat())).fetchone()[0] / 1000

    today_s = tracked_seconds(today, today)
    week_s = tracked_seconds(monday, today)
    orders_today = conn.execute('''
        SELECT COUNT(DISTINCT order_id) FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') = ?
    ''', (today.isoformat(),)).fetchone()[0]
    active = conn.execute('SELECT COUNT(*) FROM orders WHERE finished = 0').fetchone()[0]
    idle = _idle_count(conn, s['idle_days'], now)
    pending_ms = conn.execute('''
        SELECT COALESCE(SUM(s.duration), 0) FROM sessions s JOIN orders o ON o.id = s.order_id
        WHERE s.cnf = 0 AND s.duration > 0 AND o.finished = 0
    ''').fetchone()[0]
    month = _month_progress(conn, s, today.strftime('%Y-%m'), datetime.datetime.now())
    # When the newest export was written (Syncthing keeps the phone's file times)
    last_ns = conn.execute('SELECT MAX(mtime_ns) FROM imports WHERE ok = 1').fetchone()[0]
    conn.close()

    week_target_s = s['daily_target_h'] * len(s['work_days']) * 3600
    ahead = month['ahead_h']
    last_sync = (datetime.datetime.fromtimestamp(last_ns / 1e9, datetime.timezone.utc)
                 .isoformat(timespec='seconds') if last_ns else None)
    return jsonify({
        'today':          _fmt_hm(today_s),
        'today_seconds':  round(today_s),
        'week':           _fmt_hm(week_s),
        'week_seconds':   round(week_s),
        'week_pct':       round(week_s / week_target_s * 100) if week_target_s else 0,
        'month':          _fmt_hm(month['tracked_ms'] / 1000),
        'month_seconds':  round(month['tracked_ms'] / 1000),
        'month_goal':     _fmt_h(month['goal_h']),
        'month_pct':      month['pct'],
        'month_left':     _fmt_hm(month['remaining_h'] * 3600),
        'month_pace':     ('on pace' if abs(ahead) < 0.05 else
                           f"{_fmt_hm(abs(ahead) * 3600)} {'ahead' if ahead > 0 else 'behind'}"),
        'month_per_day':  _fmt_hm(month['per_day_h'] * 3600) if month['per_day_h'] is not None else '—',
        'orders_today':   orders_today,
        'active_orders':  active,
        'idle_orders':    idle,
        'not_in_sap':     _fmt_hm(pending_ms / 1000),
        'last_sync':      last_sync,
        'last_sync_ago':  _fmt_ago(max(0, now - last_ns / 1e9)) if last_ns else 'never',
    })


if __name__ == '__main__':
    print('Starting PO Tracker Stats at http://localhost:5000')
    app.run(debug=True, port=5000)
