"""Naming the team that owes the next action, and how long things have sat.

**The label.** pending_action_owner stores SERVICE_DESK, which is a role and
not a queue: it means "the team this incident is assigned to owes the next
action", as opposed to the caller, a vendor or a linked record. Rendering
that as the configured team label - "Assigned team" - is correct and tells a
reader nothing. On a board covering three queues, "Desktop Support" tells
them who to go and ask.

The filter dropdown keeps the generic label on purpose. It filters on the
stored token, so it is asking about the category; the row is reporting the
instance. A dropdown listing every queue name would be filtering on a
different thing from what it appears to filter on.

**The durations.** The board keeps "10 Days 12 Hrs" because someone reading
it is deciding what to do in the next ten minutes. Email gets whole days:
read once, and the hours never change the decision.

    python -m pytest tests/test_owner_label.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.timeutil import MINUTES_PER_DAY, format_days, format_duration
from delivery.digest import DEFAULT_TEAM_LABEL, DigestBuilder

ROOT = Path(__file__).resolve().parent.parent


class FakeSettings:
    def __init__(self, **values):
        self._values = values
        self.assignment_groups = ['Service Desk', 'Desktop Support']
        self.shadow_mode = False
        self.aged_after_days = 5

    def get(self, key, default=None):
        return self._values.get(key, default)


def builder(**settings):
    obj = DigestBuilder.__new__(DigestBuilder)
    obj.settings = FakeSettings(**settings)
    obj.base_url = ''
    from delivery.digest import owner_labels
    obj.owner_labels = owner_labels(obj.settings)
    return obj


def row(owner='SERVICE_DESK', group='Desktop Support', **extra):
    base = {'incident_number': 'INC1', 'pending_action_owner': owner,
            'assignment_group': group, 'attention_score': 50,
            'age_minutes': 15150, 'idle_minutes': 7200,
            'recommended_action': 'RESOLVE', 'confidence': 0.8,
            'risk_flags': []}
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# the label
# ---------------------------------------------------------------------------

class TestTheOwningTeamIsNamed:
    def test_a_team_owned_ticket_shows_its_own_queue(self):
        decorated = builder().decorate(row())
        assert decorated['pending_action_owner_label'] == 'Desktop Support'

    def test_each_row_names_its_own_queue(self):
        """The whole point on a board covering more than one."""
        b = builder()
        labels = [b.decorate(row(group=g))['pending_action_owner_label']
                  for g in ('Service Desk', 'Desktop Support', 'Network Operations')]
        assert labels == ['Service Desk', 'Desktop Support', 'Network Operations']

    @pytest.mark.parametrize('owner,expected', [
        ('CALLER', 'Caller'),
        ('VENDOR', 'Vendor'),
        ('CHANGE', 'Change'),
        ('PROBLEM', 'Problem'),
        ('APPROVAL', 'Approval'),
        ('NONE', '-'),
    ])
    def test_the_other_owners_are_untouched(self, owner, expected):
        """Only SERVICE_DESK means "the assigned team". A ticket waiting on
        the caller is not waiting on Desktop Support, and must not say so
        merely because that is the queue it sits in."""
        decorated = builder().decorate(row(owner=owner, group='Desktop Support'))
        assert decorated['pending_action_owner_label'] == expected

    @pytest.mark.parametrize('group', [None, '', '   '])
    def test_a_ticket_with_no_queue_falls_back_to_the_label(self, group):
        """There is no name to print, and a blank cell reads as a missing
        value rather than an unassigned one."""
        decorated = builder().decorate(row(group=group))
        assert decorated['pending_action_owner_label'] == DEFAULT_TEAM_LABEL

    def test_the_fallback_follows_the_configured_label(self):
        decorated = builder(**{'display.team_label': 'IT Support'}).decorate(
            row(group=''))
        assert decorated['pending_action_owner_label'] == 'IT Support'

    def test_the_stored_token_is_unchanged(self):
        """Only the rendering moved. pending_action_owner is written into
        ticket_signal and into the model's schema, and the board filters on
        it - changing it would rewrite history to fix a caption."""
        decorated = builder().decorate(row())
        assert decorated['pending_action_owner'] == 'SERVICE_DESK'

    def test_an_unknown_owner_is_shown_rather_than_blanked(self):
        decorated = builder().decorate(row(owner='SOMETHING_NEW'))
        assert decorated['pending_action_owner_label'] == 'SOMETHING_NEW'


@pytest.fixture(scope='module')
def board_template():
    return (ROOT / 'web' / 'templates' / 'board.html').read_text(encoding='utf-8')


class TestTheFilterStaysGeneric:
    """The dropdown filters on the stored token, so it asks about the
    category. Listing queue names there would filter on a different thing
    from what it appears to."""

    def test_the_dropdown_is_built_from_the_token_labels(self, board_template):
        block = board_template[board_template.index('id="ball"'):]
        block = block[:block.index('</select>')]
        assert 'PENDING_ACTION_OWNER_LABELS.items()' in block
        assert 'value="{{ key }}"' in block

    def test_the_row_uses_the_decorated_label(self, board_template):
        assert 'row.pending_action_owner_label' in board_template

    def test_the_ticket_page_uses_it_too(self):
        page = (ROOT / 'web' / 'templates' / 'ticket.html').read_text(encoding='utf-8')
        assert 'row.pending_action_owner_label' in page


# ---------------------------------------------------------------------------
# the durations
# ---------------------------------------------------------------------------

class TestWholeDaysForEmail:
    @pytest.mark.parametrize('minutes,expected', [
        (15150, '10 Days'),                 # 10 days 12 hrs - truncated
        (14400, '10 Days'),                 # exactly 10
        (MINUTES_PER_DAY, '1 Day'),         # singular
        (MINUTES_PER_DAY * 2, '2 Days'),
        (1439, 'Under a day'),              # 23h59
        (180, 'Under a day'),
        (0, 'Under a day'),
    ])
    def test_rendering(self, minutes, expected):
        assert format_days(minutes) == expected

    def test_it_truncates_rather_than_rounds(self):
        """Rounding up would have a ticket report a day it has not been
        open."""
        assert format_days(MINUTES_PER_DAY * 10 + MINUTES_PER_DAY - 1) == '10 Days'

    def test_under_a_day_is_named_not_zeroed(self):
        """'0 Days' on an aged-ticket report reads as a broken value rather
        than a small one, and idle time is under a day constantly."""
        assert '0' not in format_days(60)

    @pytest.mark.parametrize('value', [None, '', 'abc'])
    def test_rubbish_is_empty_not_an_exception(self, value):
        assert format_days(value) == ''

    def test_a_negative_is_clamped(self):
        """A clock skew between ServiceNow and this host can produce one."""
        assert format_days(-500) == 'Under a day'

    def test_the_board_keeps_the_precise_form(self):
        """Both are computed from the same minutes, so the two renderings
        can disagree about detail and never about the figure."""
        decorated = builder().decorate(row(age_minutes=15150, idle_minutes=7200))
        assert decorated['age_display'] == format_duration(15150)
        assert decorated['age_days_display'] == '10 Days'
        assert decorated['idle_display'] == format_duration(7200)
        assert decorated['idle_days_display'] == '5 Days'


class TestTheEmailsUseTheDayForm:
    @pytest.mark.parametrize('name', ['lead_digest.html', 'agent_digest.html'])
    def test_the_digest_shows_days_only(self, name):
        body = (ROOT / 'delivery' / 'templates' / name).read_text(encoding='utf-8')
        assert 'age_days_display' in body
        assert 'idle_days_display' in body

    @pytest.mark.parametrize('name', ['lead_digest.html', 'agent_digest.html'])
    def test_the_precise_form_is_gone_from_the_digest(self, name):
        body = (ROOT / 'delivery' / 'templates' / name).read_text(encoding='utf-8')
        assert 'row.age_display' not in body
        assert 'row.idle_display' not in body

    def test_the_board_still_uses_the_precise_form(self):
        board = (ROOT / 'web' / 'templates' / 'board.html').read_text(encoding='utf-8')
        assert 'row.age_display' in board
        assert 'row.age_days_display' not in board

    def test_age_and_idle_are_separated_in_the_lead_digest(self):
        """Run together on consecutive lines they read as one wrapped
        value, and the second number gets taken for part of the first."""
        body = (ROOT / 'delivery' / 'templates' / 'lead_digest.html').read_text(
            encoding='utf-8')
        cell = body[body.index('age_days_display'):body.index('idle_days_display')]
        assert 'height:9px' in cell, 'no spacer between the age and idle blocks'

    def test_both_blocks_are_labelled(self):
        """Two bare numbers in one cell do not say which is which."""
        body = (ROOT / 'delivery' / 'templates' / 'lead_digest.html').read_text(
            encoding='utf-8')
        cell = body[body.index('age_days_display'):
                    body.index('idle_days_display') + 400]
        assert '>open<' in cell
        assert '>idle<' in cell
