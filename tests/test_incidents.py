"""Tests for the incident query builder, chiefly the scope clause.

Every incident read is narrowed by _group_clause(), so a defect there does not
surface as an error - it silently empties the backlog, the digest and the
backfill at once. Two ways to get it wrong, both of which we hit live:

  * assignment_group holds a sys_id, so comparing it to a display name with
    = or IN matches nothing however the name is spelled. Only the dot-walk
    assignment_group.name reaches sys_user_group.name.
  * ServiceNow DROPS an unparseable condition rather than rejecting it, so a
    clause built from syntax the instance does not recognise matches every
    record. A broken filter can fail open as easily as closed.

These assert on the emitted query string because that string is the contract
with ServiceNow, and nothing else in the suite can catch a regression in it.
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.snow.incidents import CLOSED_STATES, OPEN_STATES, IncidentReader

GROUPS = ['IT Service Desk', 'Service Desk - EMEA']


class FakeClient:
    """Records the params of every call and returns a canned page."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls = []

    def get(self, table, params):
        self.calls.append((table, params))
        return list(self.rows)

    def get_all(self, table, params, max_records=None):
        self.calls.append((table, params))
        return list(self.rows)

    @property
    def last_query(self):
        return self.calls[-1][1]['sysparm_query']


def reader(groups=GROUPS, rows=None):
    return IncidentReader(FakeClient(rows), groups)


class TestGroupClause:
    def test_dot_walks_to_the_group_name(self):
        """assignment_groupIN compares against a sys_id and never matches."""
        assert (reader(['Service Desk'])._group_clause()
                == '^assignment_group.nameINService Desk')

    def test_several_groups_are_comma_joined(self):
        assert (reader()._group_clause()
                == '^assignment_group.nameINIT Service Desk,Service Desk - EMEA')

    def test_no_groups_means_no_clause(self):
        assert reader([])._group_clause() == ''
        assert reader(None)._group_clause() == ''

    def test_blank_entries_are_dropped(self):
        assert reader(['', '  ', 'Service Desk'])._group_clause() == (
            '^assignment_group.nameINService Desk')

    def test_never_emits_the_bare_reference_comparison(self):
        """The regression: '^assignment_groupIN' silently matches nothing."""
        clause = reader()._group_clause()
        assert '^assignment_groupIN' not in clause
        assert '^assignment_group=' not in clause


class TestScopeIsAppliedEverywhere:
    """A query that forgets the clause leaks other teams' tickets into the
    board; one that malforms it empties the board. Both are silent."""

    @pytest.mark.parametrize('call', [
        lambda r: r.get_aged_open_incidents(5),
        lambda r: r.sample_resolved_within(30),
    ])
    def test_scope_is_present(self, call):
        r = reader()
        call(r)
        assert '^assignment_group.nameIN' in r.client.last_query

    def test_closed_between_is_scoped(self):
        r = reader()
        r.get_closed_between(datetime.datetime(2026, 1, 1),
                             datetime.datetime(2026, 2, 1))
        assert '^assignment_group.nameIN' in r.client.last_query

    def test_unscoped_reader_emits_no_group_condition(self):
        r = reader([])
        r.get_aged_open_incidents(5)
        assert 'assignment_group' not in r.client.last_query


class TestQueryShapes:
    def test_aged_open_uses_open_states(self):
        r = reader()
        r.get_aged_open_incidents(5)
        assert f'stateIN{OPEN_STATES}' in r.client.last_query

    def test_closed_between_uses_closed_states(self):
        r = reader()
        r.get_closed_between(datetime.datetime(2026, 1, 1),
                             datetime.datetime(2026, 2, 1))
        assert f'stateIN{CLOSED_STATES}' in r.client.last_query

    def test_resolved_probe_uses_an_explicit_window(self):
        """Not RELATIVEGE: an unrecognised unit is dropped, and a probe that
        matches everything would report health while the backfill finds none."""
        r = reader()
        r.sample_resolved_within(30)
        query = r.client.last_query
        assert 'RELATIVE' not in query
        assert 'resolved_at>=javascript:gs.dateGenerate' in query

    def test_resolved_probe_does_not_paginate(self):
        r = reader()
        r.sample_resolved_within(30, limit=1)
        assert r.client.calls[-1][1]['sysparm_limit'] == 1


class TestUnresolvableGroups:
    def test_all_present_returns_empty(self):
        r = reader(rows=[{'name': g} for g in GROUPS])
        assert r.unresolvable_groups() == []

    def test_reports_the_missing_one(self):
        r = reader(rows=[{'name': 'IT Service Desk'}])
        assert r.unresolvable_groups() == ['Service Desk - EMEA']

    def test_reports_all_when_nothing_matches(self):
        r = reader(rows=[])
        assert r.unresolvable_groups() == GROUPS

    def test_handles_reference_shaped_names(self):
        """This instance returns reference fields as dicts even under
        sysparm_display_value=true."""
        r = reader(rows=[{'name': {'display_value': g, 'link': 'x'}} for g in GROUPS])
        assert r.unresolvable_groups() == []

    def test_no_groups_makes_no_call(self):
        r = reader([])
        assert r.unresolvable_groups() == []
        assert r.client.calls == []

    def test_queries_sys_user_group_not_incident(self):
        r = reader(rows=[{'name': g} for g in GROUPS])
        r.unresolvable_groups()
        assert r.client.calls[-1][0] == 'sys_user_group'

    def test_matching_is_exact_not_substring(self):
        """'Service Desk' must not be satisfied by 'IT Service Desk'."""
        r = reader(['Service Desk'], rows=[{'name': 'IT Service Desk'}])
        assert r.unresolvable_groups() == ['Service Desk']
