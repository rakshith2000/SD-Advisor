"""The assignment group selector in the navigation bar.

One parameter - `group` - chosen in the navigation bar and applied to every
page that lists tickets. Three things have to hold, and each of them fails
quietly if it does not:

  * Only the queues the account is entitled to are listed. The dropdown is
    also the answer to "what can I see", so an extra entry is both a wrong
    filter and a disclosure of which queues exist.

  * The selection travels with the links. A navigation bar that drops it
    would widen the next page back to every queue in scope - and a board
    showing more than you expected reads as a busy morning, not as a bug.

  * There is exactly one control for it. The board filter form used to carry
    its own Group dropdown; two controls for one parameter disagree the
    moment either is changed, and the one that loses is whichever the reader
    was not looking at.

    python -m pytest tests/test_nav_scope.py -q
"""

import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

SD = 'Service Desk'
DS = 'Desktop Support'
NW = 'Network Operations'


class Req:
    """The two attributes base.html reads off the request."""

    def __init__(self, path='/board', params=None):
        self.url = type('U', (), {'path': path})()
        self.query_params = type('Q', (), {
            'get': staticmethod(lambda key, _p=(params or {}): _p.get(key))})()


@pytest.fixture(scope='module')
def env():
    e = Environment(loader=FileSystemLoader(str(ROOT / 'web' / 'templates')))
    e.filters['localdt'] = lambda value, fmt=None, with_zone=False: str(value)
    return e


def navbar(env, groups, selected=None, role='LEAD', path='/board'):
    return env.get_template('base.html').render(
        request=Req(path), user={'role': role, 'full_name': 'A Lead',
                                 'username': 'lead'},
        nav_groups=groups, nav_group=selected,
        shadow_mode=False, customer='Example',
        display_tz='UTC', display_tz_label='UTC', display_tz_abbrev='UTC',
        timezone_options=[], allow_user_timezone=False, now=None)


def options(html):
    """The values offered by the group selector, in order."""
    block = re.search(r'<select id="group-select".*?</select>', html, re.S)
    if not block:
        return None
    return re.findall(r'<option value="([^"]*)"', block.group(0))


# ---------------------------------------------------------------------------
# what is offered
# ---------------------------------------------------------------------------

class TestTheSelector:
    def test_it_lists_the_accounts_own_queues(self, env):
        assert options(navbar(env, [SD, DS])) == ['', SD, DS]

    def test_it_offers_nothing_outside_the_scope(self, env):
        """The list is per account, so a queue absent from it is a queue this
        reader cannot be told exists."""
        assert NW not in (options(navbar(env, [SD, DS])) or [])

    def test_the_blank_option_means_every_queue_in_scope(self, env):
        html = navbar(env, [SD, DS])
        assert 'All my groups (2)' in html

    def test_the_current_choice_is_marked_selected(self, env):
        html = navbar(env, [SD, DS], selected=DS)
        block = re.search(rf'<option value="{DS}"[^>]*>', html).group(0)
        assert 'selected' in block

    def test_a_single_queue_is_text_rather_than_a_dropdown(self, env):
        """A select with one option looks like a choice and is not one."""
        html = navbar(env, [SD])
        assert options(html) is None
        assert 'group-single' in html
        assert SD in html

    def test_an_account_with_no_queues_gets_no_control(self, env):
        html = navbar(env, [])
        assert options(html) is None
        assert 'group-single' not in html

    def test_an_administrator_is_offered_every_queue(self, env):
        """Their scope is the whole estate, so the same template produces the
        full list without a special case."""
        assert options(navbar(env, [SD, DS, NW], role='ADMIN')) == ['', SD, DS, NW]


# ---------------------------------------------------------------------------
# carrying the choice
# ---------------------------------------------------------------------------

class TestTheChoiceTravels:
    def test_the_ticket_listing_links_carry_it(self, env):
        html = navbar(env, [SD, DS], selected=DS)
        assert 'href="/board?group=Desktop%20Support"' in html
        assert 'href="/agents?group=Desktop%20Support"' in html

    def test_the_digest_preview_link_carries_it(self, env):
        """The preview shows one queue's digest, so which queue is the whole
        content of the page."""
        html = navbar(env, [SD, DS], selected=DS, role='ADMIN')
        assert 'href="/digest/preview?group=Desktop%20Support"' in html

    def test_nothing_is_appended_when_no_queue_is_chosen(self, env):
        html = navbar(env, [SD, DS])
        assert 'href="/board"' in html
        assert 'group=' not in html.split('<nav')[1].split('</nav>')[0]

    def test_the_value_is_url_encoded(self, env):
        """Every real group name has a space in it."""
        html = navbar(env, [SD, DS], selected=SD)
        assert 'group=Service%20Desk' in html
        assert 'href="/board?group=Service Desk"' not in html

    def test_the_script_clears_the_agent_filter(self, env):
        """An agent chosen in one queue usually matches nothing in another,
        and an empty board reads as a clean queue rather than a stale
        filter."""
        html = navbar(env, [SD, DS])
        handler = html[html.index('function selectGroup'):]
        assert "searchParams.delete('agent')" in handler


# ---------------------------------------------------------------------------
# one control, not two
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def board():
    source = (ROOT / 'web' / 'templates' / 'board.html').read_text(encoding='utf-8')
    return source[source.index('<form class="filters"'):source.index('</form>')]


@pytest.fixture(scope='module')
def source():
    return (ROOT / 'web' / 'app.py').read_text(encoding='utf-8')


class TestTheBoardFormDefersToIt:
    def test_the_filter_form_has_no_group_dropdown(self, board):
        assert '<select id="group"' not in board

    def test_it_carries_the_selection_as_a_hidden_field(self, board):
        """Without this, applying any other filter would quietly widen the
        board back to every queue in scope."""
        assert 'type="hidden" name="group"' in board

    def test_reset_keeps_the_queue(self, board):
        """Reset clears the filters. The queue is chosen in the navigation
        bar and is not one of them."""
        reset = re.search(r'<a class="btn" href="/board[^"]*">Reset</a>', board)
        assert reset and 'group=' in reset.group(0)


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------

class TestTheRouteSupplies:
    """The template degrades quietly.

    An absent nav_groups renders no control at all rather than raising, which
    is the right behaviour for a half-built context and the wrong thing to
    discover in production - the page would look finished and simply have no
    queue selector on it. These check the other side of the seam.
    """

    def test_render_populates_the_selector(self, source):
        body = source[source.index('def render(request'):
                      source.index('def scope_of(')]
        assert "context['nav_groups']" in body
        assert "'nav_group'" in body

    def test_it_is_built_from_the_accounts_scope(self, source):
        """Not from the configured queue list. The control is per account."""
        body = source[source.index('def render(request'):
                      source.index('def scope_of(')]
        assert 'scope_of(' in body
        assert 'known_assignment_groups' not in body

    def test_an_out_of_scope_value_selects_nothing(self, source):
        """An unvalidated `group` in the URL would otherwise echo back into
        the dropdown, confirming the queue exists."""
        body = source[source.index('def render(request'):
                      source.index('def scope_of(')]
        assert 'in visible' in body

    def test_the_listing_routes_accept_the_parameter(self, source):
        """A navigation bar that offers a queue the page ignores is a control
        that does nothing."""
        for route in ('def board(', 'def agents_view(', 'def digest_preview('):
            start = source.index(route)
            signature = source[start:source.index('):', start)]
            assert 'group' in signature, route
