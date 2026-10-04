"""Activity stream grouping.

The property under test is that one save reads as one event. ServiceNow writes
an audit row per column, so an incident being created arrives as a dozen rows
sharing a timestamp; shown one per line that is twelve lines of noise before
anybody has done anything, and the work note that matters is off the screen.

The grouping has to hold two things apart that look similar:

  * field changes from one save, which belong together
  * two journal entries written in the same second, which do not - they are
    two things two people wrote, and merging them would attribute one to the
    other

Everything here is presentation. The tests at the end pin that: the timeline
the signal layer reads is unchanged by grouping it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.signals import build_timeline
from web.activity import (CARD_COMMENT, CARD_FIELD_CHANGES, CARD_WORK_NOTE,
                          card_kind, field_label, group_activity, initials)

SYSTEM_ACCOUNTS = ['system', 'guest', 'cac.rest']
CALLER = 'Emily Nuxoll'
AGENT = 'Sinchana Naganna'

OPENED = '2026-09-22 09:18:45'
COMMENTED = '2026-09-22 09:42:59'
NOTED = '2026-09-22 09:55:26'


def change(field, new, when=OPENED, user=CALLER, old=''):
    return {'field': field, 'new': new, 'old': old,
            'update_time': when, 'user_name': user}


def creation_history():
    """What ServiceNow returns for a newly raised incident: every column
    written at the same instant by the same person."""
    return [
        change('u_assigned_region', 'AMER'),
        change('assignment_group', 'Service Desk'),
        change('caller_id', 'Emily Nuxoll'),
        change('category', 'Documentation'),
        change('contact_type', 'Self-service'),
        change('cmdb_ci', 'AL94LBWH3'),
        change('impact', 'Medium'),
        change('opened_at', '2026-09-22 09:18:44'),
        change('priority', '3 - Medium'),
        change('short_description', 'Feature Bullet Add to C1 Spec Sheets'),
        change('state', 'New'),
        change('urgency', 'Medium'),
        change('description', 'A long paragraph the caller typed.'),
    ]


def grouped(history):
    return group_activity(build_timeline(history, CALLER, SYSTEM_ACCOUNTS))


# ---------------------------------------------------------------------------
# the merge
# ---------------------------------------------------------------------------

class TestGrouping:
    def test_one_save_is_one_card(self):
        """Ten audit rows, one thing that happened."""
        cards = grouped(creation_history())
        assert len(cards) == 1
        assert cards[0]['kind'] == CARD_FIELD_CHANGES
        assert cards[0]['label'] == 'Field changes'

    def test_every_changed_field_is_listed_on_that_card(self):
        card = grouped(creation_history())[0]
        labels = [c['label'] for c in card['changes']]
        assert labels == [
            'Assigned region', 'Assignment group', 'Caller', 'Category',
            'Channel', 'Configuration Item', 'Impact', 'Opened', 'Priority',
            'Short description', 'State', 'Urgency',
        ]

    def test_the_description_is_not_listed(self):
        """A paragraph is not a field value, and on the creation entry it is
        longer than every other row put together."""
        card = grouped(creation_history())[0]
        assert 'Description' not in [c['label'] for c in card['changes']]

    def test_fields_are_listed_alphabetically(self):
        """The audit order is the order the columns sit in the table, which
        means nothing to a reader and differs between identical saves."""
        card = grouped(creation_history())[0]
        labels = [c['label'] for c in card['changes']]
        assert labels == sorted(labels, key=str.lower)

    def test_a_later_save_is_a_separate_card(self):
        history = creation_history() + [
            change('state', 'In Progress', when=NOTED, user=AGENT, old='New')]
        cards = grouped(history)
        assert len(cards) == 2

    def test_two_people_saving_in_the_same_second_are_not_merged(self):
        """Merging would attribute one person's change to the other."""
        history = [
            change('state', 'In Progress', user=AGENT, old='New'),
            change('priority', '2 - High', user=CALLER, old='3 - Medium'),
        ]
        cards = grouped(history)
        assert len(cards) == 2
        assert {c['actor'] for c in cards} == {AGENT, CALLER}

    def test_newest_card_first(self):
        history = creation_history() + [
            change('work_notes', 'picked this up', when=NOTED, user=AGENT)]
        cards = grouped(history)
        assert cards[0]['when'] == NOTED
        assert cards[-1]['when'] == OPENED

    def test_an_empty_timeline_produces_no_cards(self):
        assert group_activity([]) == []

    def test_the_limit_counts_cards_not_fields(self):
        """Ten fields in one save must not consume ten of the budget."""
        cards = group_activity(
            build_timeline(creation_history(), CALLER, SYSTEM_ACCOUNTS), limit=1)
        assert len(cards) == 1
        assert len(cards[0]['changes']) == 12


# ---------------------------------------------------------------------------
# the three kinds
# ---------------------------------------------------------------------------

class TestCardKinds:
    def test_a_caller_comment_is_its_own_card(self):
        history = [change('comments', 'The same bullet needs adding to C2.',
                          when=COMMENTED)]
        card = grouped(history)[0]
        assert card['kind'] == CARD_COMMENT
        assert card['label'] == 'Additional comments'
        assert card['text'].startswith('The same bullet')

    def test_a_work_note_is_its_own_card(self):
        history = [change('work_notes', 'assigning to the next level team.',
                          when=NOTED, user=AGENT)]
        card = grouped(history)[0]
        assert card['kind'] == CARD_WORK_NOTE
        assert card['label'] == 'Work notes'

    def test_a_comment_and_a_field_change_in_one_save_stay_apart(self):
        """They are read for different reasons; a comment buried in a list of
        field values is a comment nobody reads."""
        history = [
            change('state', 'In Progress', user=AGENT, old='New'),
            change('work_notes', 'picked this up', user=AGENT),
        ]
        cards = grouped(history)
        assert {c['kind'] for c in cards} == {CARD_FIELD_CHANGES, CARD_WORK_NOTE}

    def test_two_notes_in_the_same_second_remain_two_cards(self):
        history = [
            change('work_notes', 'first note', user=AGENT),
            change('work_notes', 'second note', user=AGENT),
        ]
        assert len(grouped(history)) == 2

    @pytest.mark.parametrize('field,expected', [
        ('comments', CARD_COMMENT),
        ('additional_comments', CARD_COMMENT),
        ('work_notes', CARD_WORK_NOTE),
        ('state', CARD_FIELD_CHANGES),
        ('', CARD_FIELD_CHANGES),
        (None, CARD_FIELD_CHANGES),
    ])
    def test_field_routes_to_the_right_card(self, field, expected):
        assert card_kind(field) == expected


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------

class TestValues:
    def test_the_new_value_is_shown(self):
        history = [change('priority', '2 - High', old='3 - Medium')]
        row = grouped(history)[0]['changes'][0]
        assert row['value'] == '2 - High'

    def test_the_previous_value_is_kept_for_context(self):
        """This is an audit tool - 'Priority: 2 - High' without what it was is
        half the fact."""
        history = [change('priority', '2 - High', old='3 - Medium')]
        assert grouped(history)[0]['changes'][0]['previous'] == '3 - Medium'

    def test_a_creation_entry_has_no_previous_value(self):
        row = grouped([change('state', 'New')])[0]['changes'][0]
        assert row['previous'] == ''

    def test_a_cleared_field_is_marked_rather_than_blank(self):
        """An empty cell reads as 'no data' when the truth is 'cleared'."""
        row = grouped([change('hold_reason', '', old='Awaiting Caller')])[0]['changes'][0]
        assert row['value'] == ''
        assert row['previous'] == 'Awaiting Caller'

    def test_a_timestamp_value_is_flagged_for_conversion(self):
        """Otherwise an 'Opened' row renders raw UTC and contradicts every
        other time on the page."""
        row = grouped([change('opened_at', '2026-09-22 09:18:44')])[0]['changes'][0]
        assert row['is_timestamp'] is True

    @pytest.mark.parametrize('value', ['3 - Medium', 'Service Desk', '',
                                       '2026-09-22', 'AL94LBWH3'])
    def test_ordinary_values_are_not_mistaken_for_timestamps(self, value):
        row = grouped([change('priority', value, old='x')])[0]['changes'][0]
        assert row['is_timestamp'] is False


# ---------------------------------------------------------------------------
# labels and avatars
# ---------------------------------------------------------------------------

class TestLabels:
    @pytest.mark.parametrize('field,expected', [
        ('cmdb_ci', 'Configuration Item'),
        ('contact_type', 'Channel'),
        ('assignment_group', 'Assignment group'),
        ('opened_at', 'Opened'),
    ])
    def test_servicenow_labels_are_used_where_they_differ(self, field, expected):
        assert field_label(field) == expected

    def test_a_custom_column_loses_its_servicenow_prefix(self):
        """Every instance carries custom columns and ours are the ones nobody
        can enumerate in advance, so the fallback matters more than the table."""
        assert field_label('u_assigned_region') == 'Assigned region'
        assert field_label('u_business_service') == 'Business service'

    def test_a_missing_field_name_does_not_produce_a_blank_label(self):
        assert field_label('') == 'Field'
        assert field_label(None) == 'Field'

    @pytest.mark.parametrize('name,expected', [
        ('Emily Nuxoll', 'EN'),
        ('Sinchana Naganna', 'SN'),
        ('Rakshithgowda Kumbaradoddi Mariswamy', 'RM'),
        ('system', 'SY'),
        ('first.last', 'FL'),
    ])
    def test_initials(self, name, expected):
        assert initials(name) == expected

    def test_an_unnamed_actor_still_gets_a_mark(self):
        """The avatar is a circle; an empty one looks like a rendering fault."""
        assert initials('') == '?'
        assert initials(None) == '?'


# ---------------------------------------------------------------------------
# the contract this feature is not allowed to break
# ---------------------------------------------------------------------------

class TestSignalsAreUnaffected:
    def test_grouping_does_not_mutate_the_timeline(self):
        """The same list feeds the idle clock. If grouping reordered or
        rewrote it, every signal on the ticket would move and nothing would
        report it."""
        timeline = build_timeline(creation_history(), CALLER, SYSTEM_ACCOUNTS)
        before = [dict(e) for e in timeline]
        group_activity(timeline)
        assert timeline == before

    def test_the_hidden_description_is_still_in_the_timeline(self):
        """Hidden from one card, not dropped from the record - it still counts
        as agent activity."""
        timeline = build_timeline(creation_history(), CALLER, SYSTEM_ACCOUNTS)
        assert 'description' in [e['field'] for e in timeline]

    def test_build_timeline_still_carries_the_arrow_text(self):
        """Other callers read `text`; adding old/new alongside must not have
        replaced it."""
        timeline = build_timeline([change('state', 'In Progress', old='New')],
                                  CALLER, SYSTEM_ACCOUNTS)
        assert timeline[0]['text'] == 'New -> In Progress'
        assert timeline[0]['old'] == 'New'
        assert timeline[0]['new'] == 'In Progress'
