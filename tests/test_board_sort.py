"""Board sorting.

Two things are easy to get wrong here and neither announces itself.

The first is sorting the rendered value. age_display reads '10 Days 12 Hrs'
and '2 Days', so ordering the text puts 2 Days after 10 Days - a table that
looks sorted, is not, and is read as authoritative. The minute columns exist
for this, and the tests below sort a set whose text order and numeric order
disagree.

The second is losing a filter. A sort link has to carry every active filter
forward; one that drops the queue hands the reader a different board while
appearing to have only changed the order.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.app import (BOARD_SORTS, DEFAULT_DIRECTION, DEFAULT_SORT,
                     direction_phrase, normalise_sort, sort_board,
                     sort_links)


def row(number, score=0, agent='', group='Service Desk', age=0, idle=0,
        owner='Service Desk', action='Resolve', confidence=None):
    """A decorated board row, as DigestBuilder.decorate leaves it."""
    return {
        'incident_number': number,
        'attention_score': score,
        'assigned_to': agent,
        'assignment_group': group,
        'age_minutes': age,
        'idle_minutes': idle,
        # The rendered forms, deliberately disagreeing with the numbers above
        # in text order so a test cannot pass by sorting the wrong field.
        'age_display': f'{age // 1440} Days',
        'idle_display': f'{idle // 1440} Days',
        'pending_action_owner_label': owner,
        'action_label': action,
        'confidence': confidence,
    }


DAY = 1440


@pytest.fixture
def board():
    """Text order and numeric order disagree for both duration columns:
    '10 Days' sorts before '2 Days' as text, after it as a number."""
    return [
        row('INC0003', score=70, agent='asha rao',  age=10 * DAY, idle=10 * DAY),
        row('INC0001', score=9,  agent='Zoe Clark', age=2 * DAY,  idle=2 * DAY),
        row('INC0002', score=45, agent='',          age=21 * DAY, idle=1 * DAY),
    ]


def numbers(rows):
    return [r['incident_number'] for r in rows]


# ---------------------------------------------------------------------------
# the sort itself
# ---------------------------------------------------------------------------

class TestSorting:
    def test_score_descending_is_the_default_ranking(self, board):
        assert numbers(sort_board(board, 'score', 'desc')) == ['INC0003', 'INC0002', 'INC0001']

    def test_score_ascending(self, board):
        assert numbers(sort_board(board, 'score', 'asc')) == ['INC0001', 'INC0002', 'INC0003']

    def test_age_sorts_numerically_not_as_displayed_text(self, board):
        """'10 Days' before '2 Days' as text; 21 > 10 > 2 as minutes."""
        assert numbers(sort_board(board, 'age', 'desc')) == ['INC0002', 'INC0003', 'INC0001']

    def test_idle_sorts_numerically_too(self, board):
        assert numbers(sort_board(board, 'idle', 'asc')) == ['INC0002', 'INC0001', 'INC0003']

    def test_text_sorts_ignore_case(self, board):
        """Otherwise every lower-case name lands after every capitalised one,
        which reads as a broken sort rather than as ASCII order."""
        assert numbers(sort_board(board, 'agent', 'asc')) == ['INC0002', 'INC0003', 'INC0001']

    def test_ticket_number_sorts(self, board):
        assert numbers(sort_board(board, 'ticket', 'asc')) == ['INC0001', 'INC0002', 'INC0003']

    def test_an_unassigned_row_sorts_without_raising(self, board):
        assert len(sort_board(board, 'agent', 'desc')) == 3

    def test_a_null_confidence_does_not_break_the_page(self, board):
        """Not yet analysed is the normal state for a fresh ticket, and it is
        a NULL in the view."""
        assert len(sort_board(board, 'confidence', 'desc')) == 3

    def test_a_non_numeric_value_sorts_as_zero(self):
        rows = [row('INC1', score=5), dict(row('INC2'), attention_score='n/a')]
        assert numbers(sort_board(rows, 'score', 'desc')) == ['INC1', 'INC2']

    def test_the_sort_is_stable_so_a_reader_does_not_lose_their_place(self):
        """Rows the column cannot separate keep the order the query gave them -
        score then age. An unstable sort reshuffles them between page loads."""
        rows = [row(f'INC{i:04d}', score=90 - i, agent='Same Person')
                for i in range(6)]
        assert numbers(sort_board(rows, 'agent', 'asc')) == numbers(rows)
        assert numbers(sort_board(rows, 'agent', 'desc')) == numbers(rows)

    def test_sorting_does_not_mutate_the_input(self, board):
        before = numbers(board)
        sort_board(board, 'age', 'asc')
        assert numbers(board) == before

    def test_an_empty_board_sorts_to_nothing(self):
        assert sort_board([], 'score', 'desc') == []

    @pytest.mark.parametrize('key', sorted(BOARD_SORTS))
    def test_every_offered_column_actually_sorts(self, key, board):
        """A column in the header with no usable field behind it would render
        as a link that does nothing."""
        assert len(sort_board(board, key, 'desc')) == 3
        assert len(sort_board(board, key, 'asc')) == 3

    @pytest.mark.parametrize('key,spec', sorted(BOARD_SORTS.items()))
    def test_no_column_sorts_on_a_rendered_value(self, key, spec):
        """The guard against the whole class of bug: _display fields are
        strings built for reading and must never be a sort key."""
        assert not spec['field'].endswith('_display')
        assert not spec['field'].endswith('_label') or spec['kind'] == 'text'


# ---------------------------------------------------------------------------
# the query string
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_defaults(self):
        assert normalise_sort(None, None) == (DEFAULT_SORT, DEFAULT_DIRECTION)

    @pytest.mark.parametrize('bad', ['', 'nonsense', 'attention_score',
                                     'score; DROP TABLE', None])
    def test_an_unknown_column_falls_back_to_the_ranking(self, bad):
        """A stale bookmark must not error the first page most people open."""
        assert normalise_sort(bad, 'desc')[0] == DEFAULT_SORT

    @pytest.mark.parametrize('bad', ['', 'sideways', 'DESC;', None])
    def test_an_unknown_direction_falls_back(self, bad):
        assert normalise_sort('age', bad)[1] == DEFAULT_DIRECTION

    def test_case_and_padding_are_tolerated(self):
        assert normalise_sort('  AGE  ', '  ASC ') == ('age', 'asc')


class TestHeaderLinks:
    FILTERS = {'group': 'Service Desk', 'agent': 'Asha Rao', 'ball': 'CALLER',
               'flag': 'SLA_BREACHED', 'action': 'RESOLVE',
               'min_score': 40, 'show_snoozed': True}

    def params(self, url):
        return parse_qs(urlparse(url).query)

    def test_every_active_filter_is_carried_forward(self):
        """The bug this prevents: a sort link that drops the queue filter
        shows a different board while looking like a reorder."""
        link = sort_links(self.FILTERS, 'score', 'desc')['age']
        got = self.params(link['url'])
        for name, value in self.FILTERS.items():
            assert name in got, f'{name} was dropped from the sort link'
        assert got['min_score'] == ['40']
        assert got['show_snoozed'] == ['true']

    def test_defaults_are_not_carried_as_noise(self):
        """min_score=0 and show_snoozed=False are the defaults; putting them
        in every link makes a shareable URL unreadable."""
        got = self.params(sort_links({'min_score': 0, 'show_snoozed': False,
                                      'group': None}, 'score', 'desc')['age']['url'])
        assert set(got) == {'sort', 'dir'}

    def test_a_filter_value_with_a_space_is_encoded(self):
        got = self.params(sort_links({'group': 'Service Desk APAC'},
                                     'score', 'desc')['age']['url'])
        assert got['group'] == ['Service Desk APAC']

    def test_the_first_click_on_a_column_sorts_descending(self):
        """Score, age and idle are all read from the worst end. Ascending on
        the first click opens a page about the worst tickets on the calmest."""
        assert sort_links({}, 'score', 'desc')['age']['next'] == 'desc'

    def test_clicking_the_active_column_reverses_it(self):
        assert sort_links({}, 'age', 'desc')['age']['next'] == 'asc'
        assert sort_links({}, 'age', 'asc')['age']['next'] == 'desc'

    def test_only_the_active_column_is_marked(self):
        links = sort_links({}, 'age', 'asc')
        assert links['age']['active'] is True
        assert links['age']['indicator']
        assert links['score']['active'] is False
        assert links['score']['indicator'] == ''

    def test_the_indicator_follows_the_direction(self):
        assert sort_links({}, 'age', 'desc')['age']['indicator'] == '▼'
        assert sort_links({}, 'age', 'asc')['age']['indicator'] == '▲'

    def test_there_is_a_way_back_to_the_ranking_with_filters_intact(self):
        got = self.params(sort_links(self.FILTERS, 'agent', 'asc')['_default']['url'])
        assert got['sort'] == [DEFAULT_SORT]
        assert got['dir'] == [DEFAULT_DIRECTION]
        assert got['group'] == ['Service Desk']

    def test_a_link_exists_for_every_column(self):
        links = sort_links({}, 'score', 'desc')
        assert set(links) == set(BOARD_SORTS) | {'_default'}


class TestWording:
    @pytest.mark.parametrize('sort,direction,expected', [
        ('score', 'desc', 'highest first'),
        ('score', 'asc', 'lowest first'),
        ('age', 'desc', 'highest first'),
        ('agent', 'asc', 'A to Z'),
        ('agent', 'desc', 'Z to A'),
        ('owner', 'asc', 'A to Z'),
    ])
    def test_the_phrase_suits_the_column(self, sort, direction, expected):
        """'Lowest first' is right for a score and wrong for a name, and a
        label that reads oddly is a label people stop reading."""
        assert direction_phrase(sort, direction) == expected

    def test_an_unknown_column_still_describes_itself(self):
        assert direction_phrase('nonsense', 'desc') == 'highest first'

    @pytest.mark.parametrize('key', sorted(BOARD_SORTS))
    def test_every_header_offers_a_phrase_for_its_next_click(self, key):
        link = sort_links({}, 'score', 'desc')[key]
        assert link['next_phrase']
