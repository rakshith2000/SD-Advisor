"""ServiceNow user and group directory lookups.

Exists for one job: when someone signs in through single sign-on for the first
time, work out which assignment groups they already belong to in ServiceNow and
use that as their initial scope on the board.

The reason this is safe to do automatically, where granting a role would not
be: membership of an assignment group already gives that person visibility of
those tickets *in ServiceNow itself*. Mirroring it here grants no access they
did not already have - it only saves an administrator from typing it in. Role
is a different matter and stays a human decision.

Two things keep it conservative.

Every result is intersected with the groups the advisor actually tracks.
sys_user_grmember covers every kind of group - approval, notification,
distribution, CAB - not only assignment groups, and a long-serving employee can
easily be in thirty. The intersection means the worst case is the scope they
would have been given by hand.

Nothing here is allowed to fail a login. ServiceNow being slow or unreachable
must degrade to the configured default scope, not to a sign-in error: the
person has authenticated correctly and the enrichment is a convenience.
"""

from typing import Any, Dict, List, Optional, Sequence

from core.logging_setup import get_logger
from core.snow.base import DISPLAY_AND_VALUE, display_value

log = get_logger('core.snow.directory')

# A membership list longer than this is a sign the match hit the wrong person
# - a shared or generic account - rather than a genuinely busy one.
SANE_MEMBERSHIP_LIMIT = 200


class DirectoryReader:
    def __init__(self, client):
        self.client = client

    # -- public ------------------------------------------------------------

    def assignment_groups_for(self, email: Optional[str] = None,
                              username: Optional[str] = None,
                              known_groups: Optional[Sequence[str]] = None) -> List[str]:
        """Groups this person belongs to, narrowed to ones the advisor tracks.

        Returns [] for an unmatched person, an unreachable instance, or a
        membership list that overlaps nothing we track. The caller treats all
        three the same way, because for the purpose of deciding an initial
        scope they are the same: we learned nothing, so grant nothing.
        """
        memberships = self._memberships(email=email, username=username)
        if not memberships:
            return []

        if not known_groups:
            # Without a set to intersect against there is no bound on what
            # this would grant, so it returns nothing rather than everything.
            log.warning('No known assignment groups to match against - '
                        'not deriving a scope for %s', email or username)
            return []

        tracked = {g.strip().lower(): g.strip() for g in known_groups if g and g.strip()}
        matched = {tracked[name.lower()] for name in memberships
                   if name.lower() in tracked}

        log.info('ServiceNow membership for %s: %d group(s), %d tracked by the advisor',
                 email or username, len(memberships), len(matched))
        return sorted(matched, key=str.lower)

    # -- internals ---------------------------------------------------------

    def _memberships(self, email: Optional[str] = None,
                     username: Optional[str] = None) -> List[str]:
        """Every active group this person is a member of, by display name.

        Tried on email first. Email is the claim Entra reliably populates and
        the field ServiceNow reliably holds, so it is the join most likely to
        succeed; user_name is the fallback for an instance where the two
        directories disagree on the login name.
        """
        for field, value in (('user.email', email), ('user.user_name', username)):
            if not value:
                continue
            try:
                rows = self._query(field, value)
            except Exception:
                # Logged, not raised. A login must not depend on this.
                log.exception('ServiceNow group lookup failed for %s=%s', field, value)
                return []

            if rows:
                names = [n for n in (display_value(r.get('group')) for r in rows) if n]
                if len(names) > SANE_MEMBERSHIP_LIMIT:
                    log.warning(
                        '%s=%s resolved to %d group memberships, which is more than a '
                        'person usually has - refusing to derive a scope from it in '
                        'case this matched a shared account.', field, value, len(names))
                    return []
                return names

        log.info('No ServiceNow group membership found for %s',
                 email or username or '(no identifier)')
        return []

    def _query(self, field: str, value: str) -> List[Dict[str, Any]]:
        """One sys_user_grmember read.

        `group` is requested as a reference and read through display_value()
        rather than dot-walked to group.name in sysparm_fields: a dot-walked
        field name comes back as a literal 'group.name' key whose shape varies
        with the display_value mode, whereas a plain reference under `all` is
        the {display_value, value} pair every other reader here already
        handles.

        group.active=true is part of the query, not a post-filter. A retired
        group would otherwise contribute a name that matches nothing and
        quietly widen the stored scope with a dead entry.
        """
        return self.client.get_all('sys_user_grmember', {
            'sysparm_query': f'{field}={value}^group.active=true',
            'sysparm_fields': 'group,user',
            'sysparm_display_value': DISPLAY_AND_VALUE,
        }, max_records=SANE_MEMBERSHIP_LIMIT + 1)

    # -- diagnostics -------------------------------------------------------

    def probe(self, email: Optional[str] = None,
              username: Optional[str] = None) -> Dict[str, Any]:
        """Read-only check for run.py doctor and manual troubleshooting."""
        try:
            rows = self.client.get('sys_user_grmember', {
                'sysparm_query': 'group.active=true',
                'sysparm_fields': 'group,user',
                'sysparm_display_value': DISPLAY_AND_VALUE,
                'sysparm_limit': 1,
            })
        except Exception as exc:
            return {'readable': False, 'error': str(exc)[:200]}

        result: Dict[str, Any] = {'readable': True, 'sample_rows': len(rows)}
        if email or username:
            result['memberships'] = self._memberships(email=email, username=username)
        return result
