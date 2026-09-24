#!/usr/bin/env python3
"""List assignment groups matching a term, with their open/closed volumes.

servicenow.assignment_groups must hold names that match sys_user_group.name
EXACTLY - the encoded query uses assignment_groupIN, and a near-miss returns
zero rows silently rather than erroring.

    python ops/find_groups.py "Service Desk"

Read-only.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.context import get_context                        # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('term', nargs='?', default='Service Desk',
                        help='substring to search group names for')
    parser.add_argument('--config')
    args = parser.parse_args()

    ctx = get_context(args.config)
    client = ctx.snow

    groups = client.get_all('sys_user_group', {
        'sysparm_query': f'active=true^nameLIKE{args.term}^ORDERBYname',
        'sysparm_fields': 'sys_id,name,email,manager',
        'sysparm_display_value': 'true',
    })

    if not groups:
        print(f'No active group name contains {args.term!r}.')
        return 1

    print(f'{len(groups)} active group(s) matching {args.term!r}:')
    print()

    rows = []
    for group in groups:
        name = group.get('name') or ''
        open_count = _count(client, f'active=true^stateIN1,2,3^assignment_group={name}')
        closed_count = _count(client, f'stateIN6,7^resolved_atRELATIVEGE@day@ago@30'
                                      f'^assignment_group={name}')
        rows.append((name, open_count, closed_count))

    width = max(len(r[0]) for r in rows)
    print(f'{"NAME":<{width}}  {"OPEN":>6}  {"RESOLVED/30d":>12}')
    print('-' * (width + 22))
    for name, open_count, closed_count in rows:
        print(f'{name:<{width}}  {open_count:>6}  {closed_count:>12}')

    print()
    print('Copy the ones you want into servicenow.assignment_groups in conf.json,')
    print('spelled exactly as shown:')
    print()
    print('    "assignment_groups": [')
    print(',\n'.join(f'        "{name}"' for name, o, c in rows if o or c))
    print('    ],')

    configured = ctx.settings.assignment_groups
    known = {r[0] for r in rows}
    unmatched = [g for g in configured if g not in known]
    if unmatched:
        print()
        print(f'Currently configured but NOT an exact group name: {unmatched}')
        print('Those contribute nothing - every query scoped to them returns zero.')

    return 0


def _count(client, query: str) -> str:
    """Rows on one page; '200+' when the page saturates."""
    try:
        rows = client.get('incident', {
            'sysparm_query': query,
            'sysparm_fields': 'number',
            'sysparm_limit': 200,
        })
    except Exception:
        return 'err'
    return f'{len(rows)}+' if len(rows) >= 200 else str(len(rows))


if __name__ == '__main__':
    sys.exit(main())
