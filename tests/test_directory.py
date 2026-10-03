"""Deriving a new account's assignment group scope from ServiceNow.

On first single sign-on the advisor reads sys_user_grmember and uses the
intersection with the groups it already tracks as that person's initial scope,
rather than leaving it empty for an administrator to fill in.

This is safe because membership of an assignment group already grants
visibility of those tickets in ServiceNow itself - mirroring it here gives
nobody access they did not have. Role is never derived this way.

Three properties carry the safety, and each has tests here.

The result is always intersected with the tracked groups. sys_user_grmember
covers approval, notification, distribution and CAB groups as well as
assignment groups, and a long-serving employee is easily in thirty; without the
intersection the derived scope would be unbounded.

Nothing in this path may fail a sign-in. An unreachable instance, a malformed
response or an unmatched person all have to resolve to "learned nothing", which
the caller turns into the configured fallback.

And a scope an administrator set is never overwritten by it. That is the
groups_source column, tested in test_access_requests.py and here at the
jit_upsert boundary.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snow.directory import SANE_MEMBERSHIP_LIMIT, DirectoryReader

TRACKED = ['Desktop Support', 'Network Operations', 'Service Desk']


def reference(label):
    """A reference field under sysparm_display_value=all."""
    return {'display_value': label, 'value': '0' * 32, 'link': 'https://x/api/now/table/y/z'}


class FakeClient:
    def __init__(self, by_query=None, rows=None, raises=None):
        self.by_query = by_query or {}
        self.rows = rows
        self.raises = raises
        self.queries = []

    def _respond(self, params):
        self.queries.append(params['sysparm_query'])
        if self.raises:
            raise self.raises
        if self.rows is not None:
            return list(self.rows)
        for fragment, result in self.by_query.items():
            if fragment in params['sysparm_query']:
                return list(result)
        return []

    def get_all(self, table, params, max_records=None):
        assert table == 'sys_user_grmember'
        return self._respond(params)

    def get(self, table, params):
        return self._respond(params)


def membership(*names):
    return [{'group': reference(n), 'user': reference('Jane Doe')} for n in names]


# ---------------------------------------------------------------------------

class TestIntersection:
    def test_only_tracked_groups_are_granted(self):
        """The property that bounds this. The other 27 memberships - approval
        groups, distribution lists, CAB - must not become board scope."""
        reader = DirectoryReader(FakeClient(rows=membership(
            'Service Desk', 'CAB Approvers', 'All Staff Notifications',
            'Desktop Support', 'Payroll Distribution')))

        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == [
                'Desktop Support', 'Service Desk']

    def test_matching_ignores_case_and_padding(self):
        reader = DirectoryReader(FakeClient(rows=membership('  service desk ')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == ['Service Desk']

    def test_the_tracked_spelling_is_what_gets_stored(self):
        """A near-miss on an assignment group name matches no tickets and
        errors nowhere, so the stored value has to be the canonical one."""
        reader = DirectoryReader(FakeClient(rows=membership('SERVICE DESK')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == ['Service Desk']

    def test_no_overlap_grants_nothing(self):
        reader = DirectoryReader(FakeClient(rows=membership('CAB Approvers')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == []

    def test_no_tracked_groups_grants_nothing_rather_than_everything(self):
        """Without a set to intersect against there is no bound, so the answer
        is none - not all."""
        reader = DirectoryReader(FakeClient(rows=membership('Service Desk')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=[]) == []

    def test_the_result_is_ordered_and_deduplicated(self):
        reader = DirectoryReader(FakeClient(rows=membership(
            'Service Desk', 'Desktop Support', 'Service Desk')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == [
                'Desktop Support', 'Service Desk']


class TestMatching:
    def test_email_is_tried_first(self):
        client = FakeClient(by_query={'user.email=': membership('Service Desk')})
        DirectoryReader(client).assignment_groups_for(
            email='jane@example.com', username='jane.doe', known_groups=TRACKED)

        assert client.queries[0].startswith('user.email=jane@example.com')
        assert len(client.queries) == 1          # no need for the fallback

    def test_username_is_the_fallback_when_email_matches_nothing(self):
        """For an instance where Entra and ServiceNow disagree on the login
        name or the mail attribute is unset."""
        client = FakeClient(by_query={'user.user_name=': membership('Service Desk')})
        groups = DirectoryReader(client).assignment_groups_for(
            email='stale@example.com', username='jane.doe', known_groups=TRACKED)

        assert groups == ['Service Desk']
        assert len(client.queries) == 2
        assert 'user.user_name=jane.doe' in client.queries[1]

    def test_retired_groups_are_excluded_in_the_query(self):
        """A post-filter would be too late - the name would already be in the
        list and would widen the scope with a dead entry."""
        client = FakeClient(rows=membership('Service Desk'))
        DirectoryReader(client).assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED)
        assert 'group.active=true' in client.queries[0]

    def test_an_unmatched_person_gets_nothing(self):
        reader = DirectoryReader(FakeClient(rows=[]))
        assert reader.assignment_groups_for(
            email='nobody@example.com', username='nobody', known_groups=TRACKED) == []

    def test_no_identifiers_at_all_queries_nothing(self):
        client = FakeClient(rows=membership('Service Desk'))
        assert DirectoryReader(client).assignment_groups_for(known_groups=TRACKED) == []
        assert client.queries == []


class TestFailureIsNeverFatal:
    def test_an_unreachable_instance_returns_empty(self):
        """A sign-in must not depend on this. The person authenticated
        correctly; the scope is an enrichment."""
        reader = DirectoryReader(FakeClient(raises=ConnectionError('instance down')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == []

    def test_a_timeout_returns_empty(self):
        reader = DirectoryReader(FakeClient(raises=TimeoutError('read timed out')))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == []

    def test_a_flat_payload_does_not_raise(self):
        """Backwards compatibility with display_value=true responses."""
        reader = DirectoryReader(FakeClient(rows=[{'group': 'Service Desk'}]))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == ['Service Desk']

    def test_rows_with_no_group_are_skipped(self):
        reader = DirectoryReader(FakeClient(rows=[
            {'group': reference('')}, {'user': reference('Jane')},
            {'group': reference('Service Desk')}]))
        assert reader.assignment_groups_for(
            email='jane@example.com', known_groups=TRACKED) == ['Service Desk']

    def test_an_implausible_membership_count_is_refused(self):
        """More groups than a person has is a sign the match hit a shared or
        generic account, not a busy one. Grant nothing rather than guess."""
        reader = DirectoryReader(FakeClient(
            rows=membership(*[f'Group {i}' for i in range(SANE_MEMBERSHIP_LIMIT + 5)]
                            + ['Service Desk'])))
        assert reader.assignment_groups_for(
            email='shared.mailbox@example.com', known_groups=TRACKED) == []


class TestProbe:
    def test_reports_reachable(self):
        assert DirectoryReader(FakeClient(rows=membership('Service Desk'))).probe() == {
            'readable': True, 'sample_rows': 1}

    def test_reports_the_error_rather_than_raising(self):
        """doctor turns this into a FAIL line; an exception would abort the
        whole run and hide the checks after it."""
        status = DirectoryReader(FakeClient(raises=PermissionError('ACL denied'))).probe()
        assert status['readable'] is False
        assert 'ACL denied' in status['error']

    def test_can_report_one_persons_memberships(self):
        reader = DirectoryReader(FakeClient(rows=membership('Service Desk', 'CAB')))
        assert reader.probe(email='jane@example.com')['memberships'] == [
            'Service Desk', 'CAB']


# ---------------------------------------------------------------------------
# scope provenance at the jit_upsert boundary
# ---------------------------------------------------------------------------

from web.auth import UserStore                                    # noqa: E402


class FakeDb:
    def __init__(self, rows=None):
        self.rows = [dict(r) for r in (rows or [])]
        self._next_id = len(self.rows) + 1

    def query_one(self, sql, params=None):
        params = params or ()
        key = 'external_id' if 'external_id = %s' in sql else 'username'
        for row in self.rows:
            if row.get(key) == params[0]:
                if key == 'username' and 'external_id IS NULL' in sql and row.get('external_id'):
                    continue
                return dict(row)
        return None

    def insert(self, table, data, ignore=False):
        self.rows.append({'id': self._next_id, **data})
        self._next_id += 1
        return 1

    def update(self, table, values, conditions=None):
        target = conditions[0]['val']
        for row in self.rows:
            if row['id'] == target:
                row.update(values)
                return 1
        return 0


IDENTITY = {'external_id': 'entra-999', 'username': 'jane.doe',
            'email': 'jane.doe@example.com', 'full_name': 'Jane Doe'}


def account(**over):
    base = {'id': 1, 'username': 'jane.doe', 'external_id': 'entra-999',
            'full_name': 'Jane Doe', 'email': 'jane.doe@example.com',
            'role': 'VIEWER', 'auth_source': 'OIDC', 'role_source': 'MANUAL',
            'groups_source': 'SERVICENOW', 'assignment_groups': 'Service Desk',
            'active': 1}
    base.update(over)
    return base


class TestScopeProvenance:
    def test_a_new_account_records_where_its_scope_came_from(self):
        db = FakeDb()
        store = UserStore(db)
        store.jit_upsert(IDENTITY, default_groups=['Service Desk'],
                         groups_source='SERVICENOW')

        assert db.rows[0]['groups_source'] == 'SERVICENOW'
        assert db.rows[0]['assignment_groups'] == 'Service Desk'
        assert db.rows[0]['groups_synced_at'] is not None

    def test_an_empty_derived_scope_is_recorded_as_manual(self):
        """Nothing was derived, so nothing is eligible for re-derivation -
        otherwise an unmatched person's empty scope would be overwritten on
        every sign-in after an administrator fixed it by hand."""
        db = FakeDb()
        UserStore(db).jit_upsert(IDENTITY, default_groups=[],
                                 groups_source='SERVICENOW')
        assert db.rows[0]['groups_source'] == 'MANUAL'
        assert db.rows[0]['groups_synced_at'] is None

    def test_a_role_is_never_derived_from_servicenow(self):
        db = FakeDb()
        UserStore(db).jit_upsert(IDENTITY, default_role='VIEWER',
                                 default_groups=['Service Desk'],
                                 groups_source='SERVICENOW')
        assert db.rows[0]['role'] == 'VIEWER'
        assert db.rows[0]['role_source'] == 'MANUAL'

    def test_an_administrator_set_scope_survives_a_refresh(self):
        """The reason groups_source exists. Reverting a deliberate grant on
        the holder's next sign-in is not reported as a bug - they just
        quietly lose half the board."""
        db = FakeDb([account(groups_source='MANUAL',
                             assignment_groups='Network Operations')])
        UserStore(db).jit_upsert(IDENTITY, default_groups=['Service Desk'],
                                 groups_source='SERVICENOW', refresh_groups=True)
        assert db.rows[0]['assignment_groups'] == 'Network Operations'

    def test_a_scope_granted_with_a_lead_request_survives_a_refresh(self):
        db = FakeDb([account(role='LEAD', groups_source='REQUEST',
                             assignment_groups='Desktop Support, Service Desk')])
        UserStore(db).jit_upsert(IDENTITY, default_groups=['Service Desk'],
                                 groups_source='SERVICENOW', refresh_groups=True)
        assert db.rows[0]['assignment_groups'] == 'Desktop Support, Service Desk'
        assert db.rows[0]['role'] == 'LEAD'

    def test_a_derived_scope_is_refreshed_when_asked(self):
        """A team move is picked up without an administrator noticing."""
        db = FakeDb([account(groups_source='SERVICENOW',
                             assignment_groups='Service Desk')])
        UserStore(db).jit_upsert(IDENTITY, default_groups=['Network Operations'],
                                 groups_source='SERVICENOW', refresh_groups=True)
        assert db.rows[0]['assignment_groups'] == 'Network Operations'

    def test_refresh_is_off_by_default(self):
        db = FakeDb([account(groups_source='SERVICENOW',
                             assignment_groups='Service Desk')])
        UserStore(db).jit_upsert(IDENTITY, default_groups=['Network Operations'],
                                 groups_source='SERVICENOW')
        assert db.rows[0]['assignment_groups'] == 'Service Desk'

    def test_an_empty_derivation_never_wipes_an_existing_scope(self):
        """ServiceNow being unreachable returns [], and that must not be
        mistaken for "this person belongs to nothing any more"."""
        db = FakeDb([account(groups_source='SERVICENOW',
                             assignment_groups='Service Desk')])
        UserStore(db).jit_upsert(IDENTITY, default_groups=[],
                                 groups_source='SERVICENOW', refresh_groups=True)
        assert db.rows[0]['assignment_groups'] == 'Service Desk'

    def test_an_existing_local_account_keeps_its_role_and_scope(self):
        """A hand-provisioned lead signing in through SSO for the first time
        must not be demoted to the auto-provisioned defaults."""
        db = FakeDb([{'id': 1, 'username': 'jane.doe', 'external_id': None,
                      'role': 'LEAD', 'auth_source': 'LOCAL',
                      'groups_source': 'MANUAL',
                      'assignment_groups': 'Network Operations', 'active': 1,
                      'full_name': 'Jane Doe', 'email': 'jane.doe@example.com'}])
        UserStore(db).jit_upsert(IDENTITY, default_role='VIEWER',
                                 default_groups=['Service Desk'],
                                 groups_source='SERVICENOW', refresh_groups=True)

        assert db.rows[0]['role'] == 'LEAD'
        assert db.rows[0]['assignment_groups'] == 'Network Operations'
        assert db.rows[0]['external_id'] == 'entra-999'      # now linked
