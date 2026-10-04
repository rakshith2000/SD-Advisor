"""Tickets leaving the board.

Every fetch this service makes carries three conditions - active=true, an open
state, and the group scope - so a query returns what matches and can never
return what stopped matching. The moment a ticket is resolved or reassigned
out of our queues it vanishes from the feed, nothing updates the local row
again, and it sits on the board forever showing the queue it used to be in.

Reconciliation is the only thing standing between the board and that. These
tests pin the three ways a ticket departs, because only one of them clears the
flag the old check looked at:

    closed       active=false               the easy case
    resolved     active usually STAYS true   until auto-close, days later
    reassigned   active=true, still open     it is simply not ours any more

and the two ways it must NOT be retired: a transport error, and a ticket that
is still open and still in scope.
"""

import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.sync import OPEN_STATE_VALUES, TicketSync

NOW = datetime.datetime(2026, 10, 4, 12, 0, 0)
TRACKED = ['Service Desk', 'Service Desk APAC']


def field(display, value=None):
    """A sysparm_display_value=all field."""
    return {'display_value': display, 'value': display if value is None else value}


def incident(number='INC1000001', state='2', active='true',
             group='Service Desk', updated='2026-10-02 15:55:08'):
    return {
        'number': field(number),
        'sys_id': field('abc123'),
        'short_description': field('A thing broke'),
        'description': field(''),
        'caller_id': field('Jeremy Barnett'),
        'assigned_to': field('Dipti Saha'),
        'assignment_group': field(group),
        # state is a choice: the label is in display_value, the number in value.
        'state': {'display_value': 'In Progress', 'value': state},
        'hold_reason': field(''),
        'priority': field('3 - Medium'),
        'impact': field('Medium'),
        'urgency': field('Medium'),
        'category': field('PC'),
        'subcategory': field('Software'),
        'cmdb_ci': field(''),
        'contact_type': field('Self-service'),
        'rfc': field(''),
        'problem_id': field(''),
        'opened_at': {'display_value': '27-09-2026 21:34:13',
                      'value': '2026-09-28 02:34:13'},
        'sys_created_on': {'display_value': '', 'value': ''},
        'sys_updated_on': {'display_value': '', 'value': updated},
        'reassignment_count': field('1'),
        'reopen_count': field('0'),
        'active': {'display_value': 'true', 'value': active},
    }


class FakeDb:
    """Just enough of the db surface for _retire_closed."""

    def __init__(self, tracked_numbers):
        self.rows = [{'incident_number': n, 'content_hash': None}
                     for n in tracked_numbers]
        self.updates = []
        self.upserts = []

    def retrieve(self, table, columns=None, conditions=None, **kw):
        return [dict(r) for r in self.rows]

    def upsert(self, table, record, update_columns=None):
        self.upserts.append(record)

    def update(self, table, values, conditions=None):
        number = conditions[0]['val']
        self.updates.append((number, values))

    def set_sync_watermark(self, *a, **kw):
        pass

    def get_sync_watermark(self, *a, **kw):
        return None

    # -- assertions -------------------------------------------------------

    @property
    def retired(self):
        return [n for n, v in self.updates if v.get('active') == 0]


class FakeReader:
    def __init__(self, by_number=None, raises=False):
        self.by_number = by_number or {}
        self.raises = raises
        self.calls = []

    def get_by_number(self, number):
        self.calls.append(number)
        if self.raises:
            raise RuntimeError('ServiceNow unreachable')
        return self.by_number.get(number)


class FakeSettings:
    def __init__(self, groups=TRACKED, **values):
        self.assignment_groups = list(groups)
        self.aged_after_days = 5
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def sync_for(tracked_numbers, by_number, groups=TRACKED, raises=False, **settings):
    db = FakeDb(tracked_numbers)
    reader = FakeReader(by_number, raises=raises)
    return TicketSync(db, reader, FakeSettings(groups, **settings)), db, reader


def reconcile(sync, fetched=(), full=True):
    return sync._retire_closed(list(fetched), full, NOW)


# ---------------------------------------------------------------------------
# the three ways a ticket departs
# ---------------------------------------------------------------------------

class TestDepartures:
    def test_a_closed_ticket_is_retired(self):
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(state='7', active='false')})
        assert reconcile(sync) == 1
        assert db.retired == ['INC1']

    def test_a_resolved_ticket_is_retired_even_though_it_is_still_active(self):
        """The one that bites. On most instances `active` is not cleared until
        auto-close runs days later, so a flag check alone leaves a resolved
        incident on the board for the whole window."""
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(state='6', active='true')})
        assert reconcile(sync) == 1
        assert db.retired == ['INC1']

    def test_a_ticket_reassigned_out_of_our_queues_is_retired(self):
        """Still open, still active, simply not ours - and invisible to every
        scoped query from the moment it moved, so nothing else can catch it."""
        sync, db, _ = sync_for(
            ['INC1'], {'INC1': incident(state='2', active='true', group='Desktop Support')})
        assert reconcile(sync) == 1
        assert db.retired == ['INC1']

    def test_a_ticket_that_cannot_be_read_at_all_is_retired(self):
        """Deleted, or the integration user lost access. Either way nobody can
        open it, so offering it on the board wastes a lead's click."""
        sync, db, _ = sync_for(['INC1'], {})
        assert reconcile(sync) == 1
        assert db.retired == ['INC1']

    @pytest.mark.parametrize('state', sorted(OPEN_STATE_VALUES))
    def test_an_open_state_is_not_retired(self, state):
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(state=state)})
        assert reconcile(sync) == 0
        assert db.retired == []

    @pytest.mark.parametrize('state', ['6', '7', '8'])
    def test_a_non_open_state_is_retired(self, state):
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(state=state)})
        assert reconcile(sync) == 1


# ---------------------------------------------------------------------------
# what must survive
# ---------------------------------------------------------------------------

class TestWhatMustNotBeRetired:
    def test_a_ticket_still_in_the_fetch_is_not_re_read(self):
        """It was just synced. Re-reading every tracked ticket on every full
        sync would be hundreds of calls for nothing."""
        sync, db, reader = sync_for(['INC1'], {'INC1': incident()})
        assert reconcile(sync, fetched=[incident(number='INC1')]) == 0
        assert reader.calls == []

    def test_a_transport_error_changes_nothing(self):
        """Retiring on a failed read would empty the board during an outage,
        and the next full sync would have nothing left to put back."""
        sync, db, _ = sync_for(['INC1', 'INC2'], {}, raises=True)
        assert reconcile(sync) == 0
        assert db.updates == []

    def test_a_delta_sync_never_retires(self):
        """A delta legitimately omits every ticket that was not touched. Acting
        on that absence would clear the board every ten minutes."""
        sync, db, reader = sync_for(['INC1'], {'INC1': incident(state='7')})
        assert reconcile(sync, full=False) == 0
        assert reader.calls == []
        assert db.updates == []

    def test_an_open_in_scope_ticket_is_refreshed_rather_than_retired(self):
        """It fell out of the fetch windows, not out of our remit. The refresh
        is the whole point of re-reading it."""
        sync, db, _ = sync_for(
            ['INC1'], {'INC1': incident(number='INC1', group='Service Desk APAC')})
        assert reconcile(sync) == 0
        assert db.retired == []
        assert [u['incident_number'] for u in db.upserts] == ['INC1']

    def test_group_matching_ignores_case_and_padding(self):
        """A configured name differing only in case would otherwise retire
        every ticket in that queue on the next full sync."""
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(group='  service desk  ')})
        assert reconcile(sync) == 0

    def test_an_unscoped_deployment_retires_on_state_alone(self):
        """With no configured groups every group is in scope, so the group
        test must not fire - it would retire the entire board."""
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(group='Anything At All')},
                               groups=[])
        assert reconcile(sync) == 0


# ---------------------------------------------------------------------------
# the record left behind
# ---------------------------------------------------------------------------

class TestTheRowIsRefreshedBeforeRetirement:
    def test_a_reassigned_ticket_records_where_it_went(self):
        """Otherwise the row keeps saying Service Desk forever, and anyone
        reading the history later is misled about why it left."""
        sync, db, _ = sync_for(
            ['INC1'], {'INC1': incident(group='Desktop Support')})
        reconcile(sync)
        assert db.upserts[0]['assignment_group'] == 'Desktop Support'

    def test_the_refresh_does_not_leave_the_ticket_active(self):
        """flatten_incident always writes active=1, so the retire has to come
        after the refresh - the other order would quietly undo itself."""
        sync, db, _ = sync_for(['INC1'], {'INC1': incident(state='6')})
        reconcile(sync)
        assert db.upserts[0]['active'] == 1       # what the refresh wrote
        assert db.retired == ['INC1']             # and what followed it


# ---------------------------------------------------------------------------
# bounding the work
# ---------------------------------------------------------------------------

class TestCeiling:
    def test_a_mistyped_group_name_cannot_cause_a_call_storm(self):
        """A near-miss in the configured group narrows every fetch to nothing,
        so the entire board lands in one reconcile pass."""
        numbers = [f'INC{i:04d}' for i in range(50)]
        sync, db, reader = sync_for(
            numbers, {n: incident(number=n, state='7') for n in numbers},
            **{'sync.max_reconcile_per_run': 10})
        assert reconcile(sync) == 10
        assert len(reader.calls) == 10

    def test_the_ceiling_defaults_high_enough_for_a_normal_day(self):
        numbers = [f'INC{i:04d}' for i in range(40)]
        sync, db, reader = sync_for(
            numbers, {n: incident(number=n, state='7') for n in numbers})
        assert reconcile(sync) == 40
