#!/usr/bin/env python3
"""Diagnostic: measure the timezone and date-format skew on this instance.

Every incident read in this project uses sysparm_display_value=true, which
returns datetimes in the integration user's timezone and date format. Signals
then compare those values against datetime.datetime.now(), which is this
server's local clock. Where the two differ, every age and inactivity figure is
wrong by the offset - silently, and in the same direction for every incident.

This prints the three clocks side by side so the skew is a number rather than
a theory.

Read-only.

    python ops/probe_timezone.py
"""

import datetime
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.context import get_context                        # noqa: E402
from core.snow.base import TS_FORMAT, parse_ts              # noqa: E402

ISO = '%Y-%m-%d %H:%M:%S'


def main() -> int:
    ctx = get_context()
    client = ctx.snow

    print('=== clocks ===')
    print(f'  this server (local)      : {datetime.datetime.now():{ISO}}  '
          f'tz={time.tzname[0]}')
    print(f'  this server (UTC)        : {datetime.datetime.utcnow():{ISO}}')
    print()

    # The integration user's own profile.
    profile = client.get('sys_user', {
        'sysparm_query': f'user_name={client.username}',
        'sysparm_fields': 'user_name,time_zone,date_format,time_format',
        'sysparm_display_value': 'true',
        'sysparm_limit': 1,
    })
    if profile:
        row = profile[0]
        print('=== integration user profile ===')
        print(f"  user_name   : {row.get('user_name')}")
        print(f"  time_zone   : {row.get('time_zone') or '(inherits system default)'}")
        print(f"  date_format : {row.get('date_format') or '(inherits system default)'}")
        print(f"  time_format : {row.get('time_format') or '(inherits system default)'}")
        print()

    # Same field, both representations. value is UTC; display_value is the
    # user's timezone rendered in the user's date format.
    sample = client.get('incident', {
        'sysparm_query': 'ORDERBYDESCsys_updated_on',
        'sysparm_fields': 'number,sys_updated_on,opened_at',
        'sysparm_display_value': 'all',
        'sysparm_limit': 1,
    })
    if not sample:
        print('No incidents readable - cannot measure the skew.')
        return 1

    row = sample[0]

    def pair(field):
        node = row.get(field) or {}
        if not isinstance(node, dict):
            return str(node), ''
        return str(node.get('value') or ''), str(node.get('display_value') or '')

    print('=== same field, both representations ===')
    number = row.get('number')
    number = number.get('value') if isinstance(number, dict) else number
    print(f'  incident: {number}')

    skews = []
    for field in ('sys_updated_on', 'opened_at'):
        raw, shown = pair(field)
        print(f'  {field}')
        print(f'      value (UTC)       : {raw!r}')
        print(f'      display_value     : {shown!r}')

        as_utc = parse_ts(raw)
        as_shown = parse_ts(shown)

        if as_shown is None and shown:
            print('      PARSE FAILURE on display_value. pipeline.signals.parse_ts')
            print('      accepts only %Y-%m-%d %H:%M:%S, so every timeline timestamp')
            print('      would silently become None - idle_days would run from')
            print('      opened_at and NO_ACTION_RECORDED would flag every incident.')
            continue

        if as_utc and as_shown:
            delta_hours = (as_shown - as_utc).total_seconds() / 3600.0
            skews.append(delta_hours)
            print(f'      display - value   : {delta_hours:+.2f} hours')
    print()

    print('=== what this means ===')
    if not skews:
        print('  Could not compute a skew.')
        return 1

    skew = skews[0]
    if abs(skew) < 0.01:
        print('  The integration user is on UTC, so display_value and value agree.')
        print('  Age and inactivity figures are correct today, but they depend on a')
        print('  ServiceNow user profile setting rather than on anything in this')
        print('  repository. An admin changing that profile would skew every figure')
        print('  with no error raised.')
    else:
        print(f'  display_value is {skew:+.2f} hours from UTC.')
        print()
        print('  Incident reads use display_value=true, so opened_at and the history')
        print('  timeline arrive on that offset clock. signals compares them against')
        print(f'  this server\'s now(), so age_days and idle_days are wrong by'
              f' {abs(skew):.2f} hours')
        print(f'  ({abs(skew) / 24.0:.3f} days) for every incident, in the same direction.')
        print()
        print('  Thresholds affected: aged_after_days, inactivity_days,')
        print('  prolonged_inactivity_days, closure_silence_days.')
        print()
        print('  task_sla is NOT affected: core/snow/sla.py already reads planned_end_time')
        print('  and business_time_left from the value field, so projected_breach_at is')
        print('  true UTC. That means ticket_signal currently mixes both clocks in one row.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
