#!/usr/bin/env python3
"""Diagnostic: measure the timezone and date-format skew on this instance.

ServiceNow returns each datetime twice - `value` in UTC, `display_value` in
the integration user's timezone and date format. Reads now take the value
side and compare against utc_now(), so neither side of an elapsed-time
subtraction depends on a user profile.

This reports three things the change turns on:

  * the offset between the two representations, which determines whether
    timestamps written before the change need rebuilding;
  * the error the superseded code actually made, which is the difference
    between the ServiceNow user's offset and THIS SERVER'S offset - not
    either one alone. Where both happened to be set to the same zone the
    old figures were correct, but only by coincidence;
  * whether display_value parses at all, since a non-ISO date format would
    have emptied the timeline silently.

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

    server_local = datetime.datetime.now()
    server_utc = (datetime.datetime.now(datetime.timezone.utc)
                  .replace(tzinfo=None, microsecond=0))
    server_offset = round((server_local.replace(microsecond=0)
                           - server_utc).total_seconds() / 3600.0, 2)

    print('=== clocks ===')
    print(f'  this server (local)      : {server_local:{ISO}}  tz={time.tzname[0]}')
    print(f'  this server (UTC)        : {server_utc:{ISO}}')
    print(f'  this server offset       : {server_offset:+.2f} hours from UTC')
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

    snow_offset = skews[0]
    print(f'  ServiceNow display_value : {snow_offset:+.2f} hours from UTC')
    print(f'  This server local clock  : {server_offset:+.2f} hours from UTC')

    # What the superseded code actually did: compare a display_value against
    # the server's LOCAL clock. The error was the difference between the two
    # offsets, not either one on its own.
    legacy_error = server_offset - snow_offset
    print(f'  Superseded comparison    : {legacy_error:+.2f} hours of error')
    print()

    if abs(legacy_error) < 0.01:
        print('  The two clocks happen to agree, so age and inactivity were correct')
        print('  under the previous code - by coincidence, not by design. Correctness')
        print('  depended on this server and the ServiceNow user profile being set to')
        print('  the same zone; changing either would have skewed every figure with')
        print('  nothing raised. Reading the value field removes that dependency.')
    else:
        print(f'  Age and inactivity were wrong by {abs(legacy_error):.2f} hours')
        print(f'  ({abs(legacy_error) / 24.0:.3f} days) for every incident, in the same')
        print('  direction. Thresholds affected: aged_after_days, inactivity_days,')
        print('  prolonged_inactivity_days, closure_silence_days.')

    print()
    if abs(snow_offset) >= 0.01:
        print(f'  Timestamps already stored were written on the display clock, so they')
        print(f'  are {abs(snow_offset):.2f} hours from the UTC values written from now on.')
        print('  ticket_signal also mixed clocks within a single row: projected_breach_at')
        print('  came from task_sla, which was already read from the value field.')
        print()
        print('  Rebuild the derived tables after migrating - see')
        print('  db/migrations/002_utc_and_minutes.sql.')
    else:
        print('  The integration user is on UTC, so stored timestamps need no rebuild.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
