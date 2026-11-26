import datetime
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from db import get_db, init_db
from ingest import ingest_folder

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


def _fmt_hm(seconds):
    minutes = int(seconds // 60)
    return f'{minutes // 60}h {minutes % 60:02d}m' if minutes >= 60 else f'{minutes}m'


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
    # Same rule as the dashboard's idle list: unfinished, no session in 7 days
    idle = conn.execute('''
        SELECT COUNT(*) FROM (
            SELECT MAX(s.start) AS last_ms FROM orders o
            LEFT JOIN sessions s ON s.order_id = o.id
            WHERE o.finished = 0 GROUP BY o.id
        ) WHERE last_ms IS NULL OR last_ms < ?
    ''', ((now - 7 * 86400) * 1000,)).fetchone()[0]
    # When the newest export was written (Syncthing keeps the phone's file times)
    last_ns = conn.execute('SELECT MAX(mtime_ns) FROM imports WHERE ok = 1').fetchone()[0]
    conn.close()

    last_sync = (datetime.datetime.fromtimestamp(last_ns / 1e9, datetime.timezone.utc)
                 .isoformat(timespec='seconds') if last_ns else None)
    return jsonify({
        'today':          _fmt_hm(today_s),
        'today_seconds':  round(today_s),
        'week':           _fmt_hm(week_s),
        'week_seconds':   round(week_s),
        'week_pct':       round(week_s / (40 * 3600) * 100),  # of a 40 h week
        'orders_today':   orders_today,
        'active_orders':  active,
        'idle_orders':    idle,
        'last_sync':      last_sync,
        'last_sync_ago':  _fmt_ago(max(0, now - last_ns / 1e9)) if last_ns else 'never',
    })


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


WORK_START = 7 * 60 + 30   # 07:30 — expected start of day
WORK_END   = 16 * 60       # 16:00 — expected end of day
LUNCH_START = 12 * 60
LUNCH_END   = 12 * 60 + 30
MIN_GAP_MIN = 5


@app.route('/api/gaps')
def gaps():
    from_str = request.args.get('from') or request.args.get('date')
    to_str   = request.args.get('to', from_str)

    conn = get_db()

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
        day_data = _compute_day_gaps(conn, row['day'])
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


def _compute_day_gaps(conn, date_str):
    rows = conn.execute('''
        SELECT start, duration
        FROM sessions
        WHERE date(start / 1000, 'unixepoch', 'localtime') = ?
          AND duration >= 1000
        ORDER BY start
    ''', (date_str,)).fetchall()

    if not rows:
        return {
            'date': date_str, 'has_sessions': False,
            'gaps': [], 'active_intervals': [],
            'day_start': _fmt_min(WORK_START), 'day_end': _fmt_min(WORK_END),
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
    # Display window: always start at 7:30; extend past 4 PM if they worked late
    display_end = max(WORK_END, last_end)

    # Gap analysis is anchored to the work window (7:30–16:00)
    gaps_out = []
    cursor = float(WORK_START)

    for iv_s, iv_e in merged:
        # Only care about the portion of each session inside the work window
        effective_s = max(iv_s, WORK_START)
        effective_e = min(iv_e, WORK_END)

        if effective_e <= WORK_START:
            continue  # session entirely before work window

        if effective_s > cursor:
            _split_gap(cursor, effective_s, LUNCH_START, LUNCH_END, MIN_GAP_MIN, gaps_out)

        cursor = max(cursor, min(iv_e, WORK_END))

        if cursor >= WORK_END:
            break

    # Gap from last tracked activity to end of work day (left early)
    if cursor < WORK_END:
        _split_gap(cursor, WORK_END, LUNCH_START, LUNCH_END, MIN_GAP_MIN, gaps_out)

    return {
        'date': date_str,
        'has_sessions': True,
        'day_start': _fmt_min(WORK_START),
        'day_end': _fmt_min(display_end),
        'first_session': _fmt_min(merged[0][0]),
        'last_session': _fmt_min(last_end),
        'gaps': gaps_out,
        'active_intervals': [{'start': _fmt_min(s), 'end': _fmt_min(e)} for s, e in merged],
        'total_gap_min': sum(g['duration_min'] for g in gaps_out),
        'total_worked_min': round(sum(r['duration'] for r in rows) / 60000),
    }


def _split_gap(gap_start, gap_end, lunch_start, lunch_end, min_gap, out):
    """Add gap segments to out, carving out the lunch window."""
    if gap_end <= lunch_start or gap_start >= lunch_end:
        # No lunch overlap
        dur = gap_end - gap_start
        if dur >= min_gap:
            out.append({'start': _fmt_min(gap_start), 'end': _fmt_min(gap_end),
                        'duration_min': round(dur)})
    else:
        # Before lunch
        if gap_start < lunch_start:
            dur = lunch_start - gap_start
            if dur >= min_gap:
                out.append({'start': _fmt_min(gap_start), 'end': _fmt_min(lunch_start),
                            'duration_min': round(dur)})
        # After lunch
        if gap_end > lunch_end:
            dur = gap_end - lunch_end
            if dur >= min_gap:
                out.append({'start': _fmt_min(lunch_end), 'end': _fmt_min(gap_end),
                            'duration_min': round(dur)})


def _fmt_min(minutes):
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f'{h:02d}:{m:02d}'


@app.route('/api/worked')
def worked():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')

    conn = get_db()

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

    from_dt = datetime.date.fromisoformat(from_str)
    to_dt   = datetime.date.fromisoformat(to_str)
    working_days = sum(
        1 for n in range((to_dt - from_dt).days + 1)
        if (from_dt + datetime.timedelta(n)).weekday() < 5
    )
    target_ms = working_days * 8 * 3_600_000

    return jsonify({
        'from': from_str, 'to': to_str,
        'total_ms': total_ms,
        'active_days': active_days,   # days that had at least one session
        'working_days': working_days, # Mon–Fri calendar days (used for target)
        'target_ms': target_ms,
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
                COALESCE(ss.cnf_count, 0) AS cnf_count
            FROM orders o
            LEFT JOIN order_tags ot ON ot.order_id = o.id
            INNER JOIN (
                SELECT order_id,
                       SUM(duration) AS total_ms,
                       COUNT(id) AS session_count,
                       SUM(CASE WHEN cnf = 1 THEN 1 ELSE 0 END) AS cnf_count
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
                COALESCE(ss.cnf_count, 0) AS cnf_count
            FROM orders o
            LEFT JOIN order_tags ot ON ot.order_id = o.id
            LEFT JOIN (
                SELECT order_id,
                       SUM(duration) AS total_ms,
                       COUNT(id) AS session_count,
                       SUM(CASE WHEN cnf = 1 THEN 1 ELSE 0 END) AS cnf_count
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
    } for r in rows])


def _parse_time(hhmm):
    h, m = map(int, hhmm.split(':'))
    return h * 60 + m


@app.route('/api/patterns')
def patterns():
    from_str = request.args.get('from')
    to_str   = request.args.get('to')
    conn = get_db()

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
    slot_counts = {}          # slot_start_min -> number of days where slot was inactive
    dow_gaps    = [[] for _ in range(7)]

    for row in dates:
        day_data = _compute_day_gaps(conn, row['day'])
        if not day_data['has_sessions']:
            continue

        d = datetime.date.fromisoformat(row['day'])
        dow_gaps[d.weekday()].append(day_data['total_gap_min'])

        # Collect inactive slots for this day first, then count once per slot
        inactive_today = set()
        for gap in day_data['gaps']:
            gap_s = _parse_time(gap['start'])
            gap_e = _parse_time(gap['end'])
            slot = (gap_s // SLOT) * SLOT
            while slot < gap_e:
                if WORK_START <= slot < WORK_END and not (LUNCH_START <= slot < LUNCH_END):
                    inactive_today.add(slot)
                slot += SLOT
        for slot in inactive_today:
            slot_counts[slot] = slot_counts.get(slot, 0) + 1

    conn.close()

    total_days = len(dates)
    slots = sorted([
        {
            'label':         f'{_fmt_min(s)}–{_fmt_min(s + SLOT)}',
            'inactive_days': slot_counts.get(s, 0),
            'pct':           round(slot_counts.get(s, 0) / total_days * 100) if total_days else 0,
        }
        for s in range(WORK_START, WORK_END, SLOT)
        if not (LUNCH_START <= s < LUNCH_END)
    ], key=lambda x: -x['inactive_days'])

    dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']
    by_dow = [
        {
            'day':          dow_names[i],
            'avg_gap_min':  round(sum(dow_gaps[i]) / len(dow_gaps[i])) if dow_gaps[i] else 0,
            'days_tracked': len(dow_gaps[i]),
        }
        for i in range(5)
    ]

    return jsonify({
        'total_days': total_days,
        'slots':      slots,
        'by_dow':     by_dow,
    })


@app.route('/api/lifetime')
def lifetime():
    conn = get_db()

    total_orders    = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
    active_orders   = conn.execute('SELECT COUNT(*) FROM orders WHERE finished = 0').fetchone()[0]
    finished_orders = conn.execute('SELECT COUNT(*) FROM orders WHERE finished = 1').fetchone()[0]
    total_ms        = conn.execute('SELECT COALESCE(SUM(duration), 0) FROM sessions').fetchone()[0]

    dates = conn.execute('''
        SELECT DISTINCT date(start / 1000, 'unixepoch', 'localtime') AS day
        FROM sessions ORDER BY day
    ''').fetchall()

    total_gap_min = sum(_compute_day_gaps(conn, r['day'])['total_gap_min'] for r in dates)

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


@app.route('/api/idle')
def idle():
    threshold_days = int(request.args.get('days', 7))
    conn = get_db()

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


if __name__ == '__main__':
    print('Starting PO Tracker Stats at http://localhost:5000')
    app.run(debug=True, port=5000)
