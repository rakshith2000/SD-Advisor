#!/usr/bin/env python3
"""Diagnostic: work out why get_closed_between() returns nothing.

The production query is three clauses joined with ^, and ServiceNow answers
several kinds of invalid query with an empty result set rather than an error.
That makes a zero-row backfill ambiguous. This runs each clause on its own so
the one responsible is obvious.

Read-only. Fetches at most one page per probe.

    python ops/probe_resolved.py [--days 30]
"""

import argparse
import datetime
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.context import get_context                        # noqa: E402
from core.snow.base import js_date                          # noqa: E402

PAGE = 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=30)
    parser.add_argument('--config')
    args = parser.parse_args()

    ctx = get_context(args.config)
    client = ctx.snow
    groups = ctx.settings.assignment_groups

    end = datetime.datetime.now()
    start = end - datetime.timedelta(days=args.days)
    plain_start = start.strftime('%Y-%m-%d %H:%M:%S')
    plain_end = end.strftime('%Y-%m-%d %H:%M:%S')

    print(f'Window     : {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}')
    print(f'Groups     : {groups or "(none configured - no group filter)"}')
    print(f'Instance   : {client.base_url}')
    print()

    def probe(label: str, query: str, fields: str = 'number,state,resolved_at'):
        try:
            rows = client.get('incident', {
                'sysparm_query': query,
                'sysparm_fields': fields,
                'sysparm_display_value': 'true',
                'sysparm_limit': PAGE,
            })
        except Exception as exc:
            print(f'  ERROR  {label:<42} {str(exc)[:110]}')
            return []
        count = f'{len(rows)}+' if len(rows) >= PAGE else str(len(rows))
        flag = '      ' if rows else '  ZERO'
        print(f'{flag}  {label:<42} {count:>6}')
        return rows

    group_clause = ('^assignment_group.nameIN' + ','.join(groups)) if groups else ''

    print('--- does anything closed exist at all -------------------------------')
    any_closed = probe('stateIN6,7  (no date, no group)', 'stateIN6,7')
    probe('state=6  (Resolved)', 'state=6')
    probe('state=7  (Closed)', 'state=7')
    probe('resolved_atISNOTEMPTY', 'stateIN6,7^resolved_atISNOTEMPTY')
    probe('closed_atISNOTEMPTY', 'stateIN6,7^closed_atISNOTEMPTY')

    print()
    print('--- the assignment_group clause on its own --------------------------')
    if groups:
        # assignment_group stores a sys_id. The bare forms compare that sys_id
        # against a display name and cannot match; only the dot-walk reaches
        # sys_user_group.name. LIKE is the exception - CONTAINS on a reference
        # resolves to the display value - so it appears to work and misleads.
        probe('assignment_group.nameIN  (PRODUCTION FORM)', f'stateIN6,7{group_clause}')
        probe('assignment_group.name= <first>',
              f'stateIN6,7^assignment_group.name={groups[0]}')
        probe('[sys_id cmp] assignment_group= <first>',
              f'stateIN6,7^assignment_group={groups[0]}')
        probe('[display cmp] assignment_groupLIKE <first>',
              f'stateIN6,7^assignment_groupLIKE{groups[0]}')
    else:
        print('       no assignment_groups configured - clause is empty, skipping')

    print()
    print('--- the date clause on its own --------------------------------------')
    probe('resolved_at>= javascript:gs.dateGenerate',
          f'stateIN6,7^resolved_at>={js_date(start)}')
    probe('resolved_at>= plain literal',
          f'stateIN6,7^resolved_at>={plain_start}')
    probe('resolved_atBETWEEN js (PRODUCTION FORM)',
          f'stateIN6,7^resolved_atBETWEEN{js_date(start)}@{js_date(end)}')
    probe('resolved_atBETWEEN plain literals',
          f'stateIN6,7^resolved_atBETWEEN{plain_start}@{plain_end}')
    # Control, not a candidate. An unparseable condition is DROPPED by
    # ServiceNow rather than rejected, so a clause the instance does not
    # understand matches everything. If either of these lands far above the
    # BETWEEN count, that unit name is not recognised here.
    probe('[control] RELATIVEGE@day@ago@30',
          f'stateIN6,7^resolved_atRELATIVEGE@day@ago@{args.days}')
    probe('[control] RELATIVEGE@dayofweek@ago@30',
          f'stateIN6,7^resolved_atRELATIVEGE@dayofweek@ago@{args.days}')

    print()
    print('--- the exact production query --------------------------------------')
    probe('full get_closed_between()',
          f'stateIN6,7^resolved_atBETWEEN{js_date(start)}@{js_date(end)}{group_clause}')

    print()
    print('--- what a closed ticket actually looks like ------------------------')
    if not any_closed:
        print('  No closed incidents visible to this account at all. Either the')
        print('  integration user has no read ACL on resolved records, or state')
        print('  6/7 are not the values this instance uses.')
        return 1

    sample = client.get('incident', {
        'sysparm_query': 'stateIN6,7^ORDERBYDESCsys_updated_on',
        'sysparm_fields': ('number,state,resolved_at,closed_at,opened_at,'
                           'assignment_group,category,subcategory,close_code,close_notes'),
        'sysparm_display_value': 'true',
        'sysparm_limit': 3,
    })
    for row in sample:
        notes = (row.get('close_notes') or '').strip()
        print(f"  {row.get('number')}")
        for key in ('state', 'opened_at', 'resolved_at', 'closed_at',
                    'assignment_group', 'category', 'subcategory', 'close_code'):
            print(f"      {key:<18} {row.get(key) or '(empty)'!r}")
        print(f"      {'close_notes len':<18} {len(notes)}"
              f"{'  <-- under 30, would be skipped' if len(notes) <= 30 else ''}")
        print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
