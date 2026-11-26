"""Dashboard settings, stored as one JSON document in the database.

The server uses them for gaps, targets and goals (so the Homepage widget
agrees with the page), and the page uses them for labels and what to show.
"""
import copy
import json
import re

from db import get_db

# Optional dashboard sections that can be turned off in Settings
SECTIONS = ('goal', 'unconfirmed', 'idle', 'compare', 'tag_stats', 'heatmap', 'gaps', 'patterns', 'timesheet')

DEFAULTS = {
    'work_start': '07:30',
    'work_end': '16:00',
    'lunch_start': '12:00',
    'lunch_end': '12:30',
    'work_days': [0, 1, 2, 3, 4],   # Monday = 0
    'daily_target_h': 7.5,          # hours you aim to book per work day
    'min_gap_min': 5,               # shorter breaks don't count as inactive
    'idle_days': 7,                 # unfinished orders untouched this long are flagged
    'monthly_goal_h': None,         # None: daily target x work days in the month
    'month_goals': {},              # per-month overrides, e.g. {"2026-12": 120}
    'sections': {name: True for name in SECTIONS},
}

_TIME = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_MONTH = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def to_min(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def load(conn=None):
    """Current settings: the saved ones on top of the defaults."""
    own = conn is None
    conn = conn or get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'settings'").fetchone()
    if own:
        conn.close()
    merged = copy.deepcopy(DEFAULTS)
    if row:
        saved = json.loads(row['value'])
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS and k != 'sections'})
        merged['sections'].update({k: bool(v) for k, v in saved.get('sections', {}).items() if k in SECTIONS})
    return merged


def update(changes):
    """Validates changes (any subset of the settings), saves the result and
    returns it. Raises ValueError with a readable message if something is off."""
    if not isinstance(changes, dict):
        raise ValueError('Settings must be a JSON object.')
    unknown = set(changes) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(sorted(unknown))}")

    s = load()
    for key, value in changes.items():
        if key == 'sections':
            if not isinstance(value, dict):
                raise ValueError('sections must be an object of true/false values.')
            s['sections'].update({k: bool(v) for k, v in value.items() if k in SECTIONS})
        elif key == 'month_goals':
            if not isinstance(value, dict):
                raise ValueError('month_goals must be an object like {"2026-12": 120}.')
            goals = dict(s['month_goals'])
            for month, hours in value.items():
                if not _MONTH.match(str(month)):
                    raise ValueError(f'"{month}" is not a month (use YYYY-MM).')
                if hours is None or hours == '':
                    goals.pop(month, None)      # null removes the override
                else:
                    goals[month] = _hours(hours, f'The goal for {month}', 744)
            s['month_goals'] = dict(sorted(goals.items()))
        else:
            s[key] = value

    for key in ('work_start', 'work_end', 'lunch_start', 'lunch_end'):
        if not isinstance(s[key], str) or not _TIME.match(s[key]):
            raise ValueError(f'{key.replace("_", " ").capitalize()} must be a time like 07:30.')
    if to_min(s['work_end']) <= to_min(s['work_start']):
        raise ValueError('The work day must end after it starts.')
    if to_min(s['lunch_end']) < to_min(s['lunch_start']):
        raise ValueError('Lunch must end after it starts.')
    days = s['work_days']
    if (not isinstance(days, list) or not days
            or any(not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days)):
        raise ValueError('Pick at least one work day.')
    s['work_days'] = sorted(set(days))
    s['daily_target_h'] = _hours(s['daily_target_h'], 'The daily target', 24)
    s['min_gap_min'] = _whole(s['min_gap_min'], 'The minimum gap', 0, 240)
    s['idle_days'] = _whole(s['idle_days'], 'Idle after', 1, 365)
    if s['monthly_goal_h'] in (None, ''):
        s['monthly_goal_h'] = None
    else:
        s['monthly_goal_h'] = _hours(s['monthly_goal_h'], 'The monthly goal', 744)

    conn = get_db()
    conn.execute('''
        INSERT INTO settings (key, value) VALUES ('settings', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    ''', (json.dumps(s),))
    conn.commit()
    conn.close()
    return s


def _hours(value, what, most):
    try:
        h = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what} must be a number of hours.') from None
    if not 0 < h <= most:
        raise ValueError(f'{what} must be more than 0 and at most {most} hours.')
    return round(h, 2)


def _whole(value, what, least, most):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f'{what} must be a whole number.') from None
    if isinstance(value, float) and value != n or not least <= n <= most:
        raise ValueError(f'{what} must be a whole number from {least} to {most}.')
    return n
