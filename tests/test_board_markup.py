"""The contract between board.html and static/table-sort.js.

Sorting happens in the browser against the data-* attributes the template
emits. That splits one feature across two files, and the seam fails quietly:
a header whose key has no matching row attribute renders as something that
looks pressable and does nothing.

The script reports that in the console. These tests catch it before it ships,
and pin the part that has to be right in the markup - that the sort keys carry
the underlying value and never the rendered one.

    python -m pytest tests/test_board_markup.py -q
"""

import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, pass_context
from markupsafe import Markup, escape

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.timeutil import format_dt
from delivery.digest import (ACTION_LABELS, FLAG_LABELS,
                             PENDING_ACTION_OWNER_LABELS)

ROOT = Path(__file__).resolve().parent.parent
DAY = 1440


@pytest.fixture(scope='module')
def html():
    env = Environment(loader=FileSystemLoader(str(ROOT / 'web' / 'templates')))

    @pass_context
    def localdt(ctx, value, fmt=None, with_zone=False):
        return format_dt(value, 'America/Chicago', fmt or '%d %b %Y %H:%M',
                         with_zone=with_zone)

    env.filters['localdt'] = localdt
    env.filters['activity_html'] = lambda v: Markup(escape(str(v or '')))
    env.globals.update({
        'ACTION_LABELS': ACTION_LABELS, 'FLAG_LABELS': FLAG_LABELS,
        'PENDING_ACTION_OWNER_LABELS': PENDING_ACTION_OWNER_LABELS,
        'shadow_mode': False, 'customer': 'Kohler', 'aged_after_days': 5,
    })

    def row(number, score, agent, age, idle):
        return {
            'incident_number': number, 'attention_score': score,
            'severity': 'high', 'assigned_to': agent,
            'assignment_group': 'Service Desk',
            'age_minutes': age, 'idle_minutes': idle,
            # The rendered durations, which order differently as text.
            'age_display': f'{age // DAY} Days', 'idle_display': f'{idle // DAY} Days',
            'short_description': 'A thing broke', 'flag_labels': [],
            'pending_action_owner_label': 'Service Desk',
            'action_label': 'Resolve', 'recommended_action': 'RESOLVE',
            'confidence': 0.8, 'confidence_pct': 80, 'low_confidence': False,
            'rationale': 'because', 'snoozed': 0, 'suggested_target_group': None,
        }

    class Req:
        class url:
            path = '/board'

        class query_params:
            @staticmethod
            def get(key):
                return None

    return env.get_template('board.html').render(
        request=Req(), user={'role': 'LEAD', 'full_name': 'L', 'username': 'l'},
        rows=[row('INC0003', 70, 'asha rao', 10 * DAY, 10 * DAY),
              row('INC0001', 9, 'Zoe Clark', 2 * DAY, 2 * DAY),
              row('INC0002', 45, '', 21 * DAY, 1 * DAY)],
        summary={'total': 3, 'service_desk_owned': 3, 'sla_breached': 0,
                 'sla_at_risk': 0, 'prolonged_inactivity': 1,
                 'caller_awaiting': 0, 'dependency_cleared': 0,
                 'closure_candidates': 0, 'needs_review': 0},
        movement={'new': [], 'worsening': [], 'steady': []},
        all_groups=['Service Desk'], visible_groups=['Service Desk'],
        no_scope=False, agents=['asha rao', 'Zoe Clark'],
        filters={'group': None, 'agent': None, 'ball': None, 'flag': None,
                 'action': None, 'min_score': 0, 'show_snoozed': False},
        display_tz='America/Chicago', display_tz_label='x',
        display_tz_abbrev='CDT', timezone_options=[],
        allow_user_timezone=False, now=None)


def header_keys(html):
    return re.findall(r'<th[^>]*\bdata-sort="([^"]+)"', html)


def row_attrs(html):
    body = html[html.index('<tbody>'):html.index('</tbody>')]
    first = re.search(r'<tr([^>]*)>', body).group(1)
    return set(re.findall(r'data-([a-z-]+)=', first))


class TestTheSeam:
    def test_the_table_opts_in(self, html):
        assert 'data-sortable="board"' in html

    def test_the_script_is_loaded(self, html):
        assert '/static/table-sort.js' in html

    def test_every_header_has_a_matching_row_attribute(self, html):
        """The failure this catches: a header that looks pressable, is, and
        reorders nothing, because the rows carry no value for it."""
        missing = [k for k in header_keys(html) if k not in row_attrs(html)]
        assert missing == [], f'headers with no data on the rows: {missing}'

    def test_every_header_declares_its_type(self, html):
        """An undeclared type falls back to text, which would sort the score
        column as strings - 9 after 70."""
        for block in re.findall(r'<th[^>]*\bdata-sort="[^"]+"[^>]*>', html):
            assert 'data-type="num"' in block or 'data-type="text"' in block, block

    def test_the_duration_columns_are_numeric(self, html):
        for key in ('score', 'age', 'idle'):
            block = re.search(rf'<th[^>]*\bdata-sort="{key}"[^>]*>', html).group(0)
            assert 'data-type="num"' in block, f'{key} would sort as text'


class TestTheValues:
    def test_age_carries_minutes_not_the_rendered_duration(self, html):
        """'10 Days' sorts before '2 Days' as text. The whole reason the
        minute columns exist."""
        assert 'data-age="14400"' in html
        assert 'data-age="10 Days"' not in html

    def test_idle_carries_minutes_too(self, html):
        assert 'data-idle="1440"' in html

    def test_the_assignee_key_is_the_stored_name_not_the_placeholder(self, html):
        """The cell reads 'Unassigned'; sorting on that would file every
        unassigned ticket under U."""
        assert 'data-agent=""' in html
        assert 'data-agent="Unassigned"' not in html

    def test_every_row_carries_a_key_for_every_header(self, html):
        body = html[html.index('<tbody>'):html.index('</tbody>')]
        keys = header_keys(html)
        for attrs in re.findall(r'<tr([^>]*)>', body):
            present = set(re.findall(r'data-([a-z-]+)=', attrs))
            assert not [k for k in keys if k not in present], attrs


class TestTheCaption:
    def test_the_default_text_is_rendered_as_well_as_stored(self, html):
        """So the caption reads correctly before the script runs, and the
        script has something to restore on reset."""
        assert 'data-default="Highest attention score first"' in html
        assert '>Highest attention score first<' in html

    def test_the_reset_link_starts_hidden(self, html):
        block = re.search(r'<a[^>]*id="board-sort-reset"[^>]*>', html).group(0)
        assert 'hidden' in block

    def test_the_table_points_at_both_elements(self, html):
        assert 'data-sort-note="board-sort-note"' in html
        assert 'data-sort-reset="board-sort-reset"' in html


class TestNoServerSideSortRemains:
    def test_the_filter_form_carries_no_sort_fields(self, html):
        """Removed with the backend sort. A hidden field naming a parameter
        the route no longer accepts is a trap for the next reader."""
        assert 'name="sort"' not in html
        assert 'name="dir"' not in html

    def test_no_header_is_a_link(self, html):
        """A link would navigate, which is the thing this replaced."""
        for block in re.findall(r'<th[^>]*\bdata-sort=.*?</th>', html, re.S):
            assert '<a ' not in block, block
            assert '<button' in block

    def test_the_route_takes_no_sort_parameter(self):
        source = (ROOT / 'web' / 'app.py').read_text(encoding='utf-8')
        for name in ('BOARD_SORTS', 'sort_board', 'sort_links', 'normalise_sort'):
            assert name not in source, f'{name} survived the move to the client'
