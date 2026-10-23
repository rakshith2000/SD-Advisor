"""Scope enforcement and redirect safety.

Two defects these exist for.

1. Scope was applied on /board and /agents and nowhere else. A lead restricted
   to one assignment group could read every other group through /api/board,
   and could open any incident by number through /ticket/{number}. The filter
   was real on the two routes anyone looks at and absent on the five they do
   not, which is the worst arrangement - it reads as enforced.

2. visible_groups() returned every group when a user's scope was empty. That
   was deliberate and correct while every account was created by hand and the
   service covered one queue. It became wrong twice over: accounts are now
   created automatically on first SSO login, and the service now covers
   several queues, so "nobody set their groups" would put another team's
   incident descriptions and caller names on a lead's board.

   An empty scope now means "everything" for ADMIN alone.

Both are about failing in the right direction. An empty scope must mean "show
nothing", never "show everything"; and an out-of-scope ticket must be
indistinguishable from one that does not exist.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.auth import UserStore, safe_next

ALL_GROUPS = ['Desktop Support', 'Network Operations', 'Service Desk']


def user(role='LEAD', groups=None):
    return {'username': 'test.user', 'role': role, 'groups': groups or []}


@pytest.fixture
def store():
    return UserStore(db=None)


# ---------------------------------------------------------------------------
# visible_groups - building a query filter
# ---------------------------------------------------------------------------

class TestVisibleGroups:
    def test_scoped_user_gets_only_their_groups(self, store):
        assert store.visible_groups(
            user(groups=['Service Desk']), ALL_GROUPS) == ['Service Desk']

    def test_lead_with_no_scope_sees_nothing(self, store):
        """A lead leads a queue. Which one is a fact somebody has to record -
        it cannot be defaulted to "all of them" once there is more than one,
        because that default is a disclosure."""
        assert store.visible_groups(user('LEAD'), ALL_GROUPS) == []

    def test_admin_with_no_scope_sees_everything(self, store):
        """The one role an empty scope widens for. An administrator decides
        access requests across the estate; they cannot do that through a
        keyhole."""
        assert store.visible_groups(user('ADMIN'), ALL_GROUPS) == ALL_GROUPS

    def test_viewer_with_no_scope_sees_nothing(self, store):
        """The inversion. An auto-provisioned account starts entitled to
        nothing, not to the whole estate."""
        assert store.visible_groups(user('VIEWER'), ALL_GROUPS) == []

    def test_only_admin_is_widened_by_an_empty_scope(self, store):
        """Stated once, as a rule, so a fourth role cannot be added and
        quietly inherit the wrong half of it."""
        widened = [role for role in ('ADMIN', 'LEAD', 'VIEWER')
                   if store.visible_groups(user(role), ALL_GROUPS)]
        assert widened == ['ADMIN']

    def test_viewer_with_a_scope_sees_it(self, store):
        assert store.visible_groups(
            user('VIEWER', ['Service Desk']), ALL_GROUPS) == ['Service Desk']

    def test_a_renamed_group_drops_out_rather_than_being_passed_through(self, store):
        """A stale scope entry must not reach the query as a value that
        matches nothing - it should simply not be in the filter."""
        assert store.visible_groups(
            user(groups=['Service Desk', 'Retired Queue']), ALL_GROUPS) == ['Service Desk']

    def test_a_scope_of_only_stale_groups_yields_nothing(self, store):
        assert store.visible_groups(user(groups=['Gone', 'Also Gone']), ALL_GROUPS) == []

    def test_matching_ignores_case_and_padding(self, store):
        assert store.visible_groups(
            user(groups=['  service desk  ']), ALL_GROUPS) == ['Service Desk']

    def test_blank_entries_are_not_a_scope(self, store):
        """['', '  '] is an empty scope, not a restriction to nothing - so it
        follows the empty-scope rule for the role rather than being treated as
        a list with entries in it."""
        assert store.visible_groups(user('ADMIN', ['', '  ']), ALL_GROUPS) == ALL_GROUPS
        assert store.visible_groups(user('LEAD', ['', '  ']), ALL_GROUPS) == []
        assert store.visible_groups(user('VIEWER', ['', '  ']), ALL_GROUPS) == []


# ---------------------------------------------------------------------------
# can_see_group - judging one record
# ---------------------------------------------------------------------------

class TestCanSeeGroup:
    def test_in_scope(self, store):
        assert store.can_see_group(user(groups=['Service Desk']), 'Service Desk')

    def test_out_of_scope(self, store):
        assert not store.can_see_group(user(groups=['Service Desk']), 'Network Operations')

    def test_unrestricted_admin_sees_any_group(self, store):
        assert store.can_see_group(user('ADMIN'), 'Network Operations')

    def test_unrestricted_lead_sees_none(self, store):
        assert not store.can_see_group(user('LEAD'), 'Network Operations')

    def test_unrestricted_viewer_sees_none(self, store):
        assert not store.can_see_group(user('VIEWER'), 'Network Operations')

    def test_case_and_padding_insensitive(self, store):
        assert store.can_see_group(user(groups=['Service Desk']), '  SERVICE DESK ')

    @pytest.mark.parametrize('group', [None, '', '   '])
    def test_a_ticket_with_no_group_is_visible_only_to_unrestricted_users(self, store, group):
        """It belongs to no queue, so it cannot be in a scoped user's queue -
        but it must not become invisible to everyone, or an unassigned ticket
        would silently drop off the estate. Administrators are now the only
        ones who hold it, which is also why they are the only ones who can
        notice it has no owner."""
        assert store.can_see_group(user('ADMIN'), group)
        assert not store.can_see_group(user('LEAD'), group)
        assert not store.can_see_group(user(groups=['Service Desk']), group)
        assert not store.can_see_group(user('VIEWER'), group)

    def test_consistent_with_visible_groups(self, store):
        """The two helpers answer the same question in different shapes, so
        they must never disagree - a ticket in a group returned by
        visible_groups must pass can_see_group."""
        for role in ('ADMIN', 'LEAD', 'VIEWER'):
            for scope in ([], ['Service Desk'], ['Service Desk', 'Desktop Support']):
                who = user(role, scope)
                allowed = set(store.visible_groups(who, ALL_GROUPS))
                for group in ALL_GROUPS:
                    assert store.can_see_group(who, group) == (group in allowed), (
                        f'{role} scope={scope} group={group}')


# ---------------------------------------------------------------------------
# safe_next
# ---------------------------------------------------------------------------

class TestSafeNext:
    @pytest.mark.parametrize('value', [
        '/board',
        '/ticket/INC0012345',
        '/board?group=Service+Desk&min_score=40',
        '/ticket/INC1#timeline',
        '/admin/role-requests/42',
    ])
    def test_relative_paths_survive_intact(self, value):
        assert safe_next(value) == value

    @pytest.mark.parametrize('value', [
        'https://evil.example/',
        'http://evil.example/',
        '//evil.example/',                 # protocol-relative
        '/\\evil.example/',                # normalised to // by browsers
        '/\\/evil.example',
        'evil.example',
        '',
        '   ',
        None,
        'javascript:alert(1)',
        'data:text/html,<script>alert(1)</script>',
    ])
    def test_everything_off_site_falls_back(self, value):
        assert safe_next(value) == '/board'

    @pytest.mark.parametrize('value', [
        '/board\nLocation: https://evil.example',
        '/board\r\nSet-Cookie: x=1',
    ])
    def test_header_injection_falls_back(self, value):
        """A newline in a Location header is a response-splitting primitive."""
        assert safe_next(value) == '/board'

    def test_the_fallback_is_configurable(self):
        assert safe_next('https://evil.example', '/profile') == '/profile'

    def test_whitespace_is_trimmed_not_rejected(self):
        assert safe_next('  /board  ') == '/board'
