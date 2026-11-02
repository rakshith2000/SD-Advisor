"""The design system's two silent failure modes.

Both of these produce a page that renders, returns 200, and is wrong in a way
no test and no exception reports.

**An undefined custom property.** `background: var(--surface-0)` where
`--surface-0` was never declared is not an error - CSS drops the declaration
and the element falls back to whatever it inherited. In light mode that often
looks fine; in dark mode it is white text on white. Two of these were shipped
before this test existed.

**A mail template with its own palette.** Email has no stylesheet to share -
Outlook has no flexbox or grid and clients strip <style> unpredictably - so
every colour is substituted inline. Five templates each declaring their own
hex values is five chances for one to drift, and the way you find out is that
one email in the set looks a year older than the others.

    python -m pytest tests/test_ui_tokens.py -q
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import admin_settings
from core.admin_settings import SECTIONS
from core.config import Settings
from delivery.dispatch import EMAIL_TOKENS

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / 'web' / 'static' / 'app.css'
WEB_TEMPLATES = sorted((ROOT / 'web' / 'templates').glob('*.html'))
MAIL_TEMPLATES = sorted((ROOT / 'delivery' / 'templates').glob('*.html'))


def read(path):
    return path.read_text(encoding='utf-8')


def markup(path):
    """The template with its Jinja comments removed.

    These templates explain in prose why they contain no `<style>` block and
    no flexbox, so a test searching the raw text finds the explanation and
    reports the thing it was written to prevent.
    """
    return re.sub(r'\{#.*?#\}', '', read(path), flags=re.S)


@pytest.fixture(scope='module')
def css():
    return read(CSS)


@pytest.fixture(scope='module')
def declared(css):
    return set(re.findall(r'^\s*(--[a-z0-9-]+)\s*:', css, re.M))


@pytest.fixture(scope='module')
def grid():
    """The card grid at the top of the settings page, rendered for real."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(ROOT / 'web' / 'templates')))
    env.filters['localdt'] = lambda v, f=None, with_zone=False: str(v)

    settings = Settings({'thresholds': {'aged_after_days': 5}}, Path('conf.json'))

    class Req:
        url = type('U', (), {'path': '/admin/settings'})()
        query_params = type('Q', (), {'get': staticmethod(lambda k: None)})()

    html = env.get_template('admin_settings.html').render(
        request=Req(), user={'role': 'ADMIN', 'full_name': 'A', 'username': 'a'},
        sections=admin_settings.section_summaries(settings, 'review'),
        section='review', section_title='Review policy', section_blurb='x',
        rows=admin_settings.rows_for(settings, 'review'),
        restart_pending=[], history=[], config_path='conf.json',
        saved='', error='', shadow_mode=False, customer='X',
        display_tz='UTC', display_tz_label='UTC', display_tz_abbrev='UTC',
        timezone_options=[], allow_user_timezone=False, now=None,
        nav_groups=[], nav_group=None, team_label='Assigned team',
        PENDING_ACTION_OWNER_LABELS={}, aged_after_days=5)
    return html[html.index('<div class="settings-grid">'):
                html.index('<div class="card">')]


# ---------------------------------------------------------------------------
# the stylesheet
# ---------------------------------------------------------------------------

class TestEveryTokenIsDeclared:
    def test_the_stylesheet_uses_nothing_it_does_not_declare(self, css, declared):
        used = set(re.findall(r'var\((--[a-z0-9-]+)', css))
        assert sorted(used - declared) == []

    def test_no_template_uses_a_token_the_stylesheet_lacks(self, declared):
        """Inline `style="..."` in a template is the easiest place for this
        to happen, because nothing links the two files."""
        offenders = {}
        for path in WEB_TEMPLATES:
            used = set(re.findall(r'var\((--[a-z0-9-]+)', read(path)))
            missing = sorted(used - declared)
            if missing:
                offenders[path.name] = missing
        assert offenders == {}

    def test_the_tokens_were_actually_found(self, declared):
        """A regex that silently matched nothing would pass both tests
        above."""
        assert len(declared) > 20


class TestDarkModeIsSelectedNotInverted:
    """Dark mode is its own set of steps chosen against the dark surface.
    An automatic filter-invert produces muddy hues and breaks the status
    colours, which are the one thing on the board that has to stay readable.
    """

    def test_there_is_an_explicit_dark_block(self, css):
        assert '[data-theme="dark"]' in css

    def test_the_os_preference_is_honoured_too(self, css):
        assert 'prefers-color-scheme: dark' in css

    def test_the_explicit_toggle_can_override_the_os(self, css):
        """Someone on a dark-mode laptop who wants the light theme has to be
        able to have it, which needs the :not() guard on the media query."""
        block = css[css.index('prefers-color-scheme: dark'):]
        assert ':not([data-theme="light"])' in block[:200]

    def test_no_invert_filter_anywhere(self, css):
        assert 'filter: invert' not in css.replace(' ', '')

    @pytest.mark.parametrize('token', [
        '--surface-1', '--text-primary', '--text-secondary', '--grid',
        '--accent',
    ])
    def test_each_core_token_is_restated_for_dark(self, css, token):
        dark = css[css.index('[data-theme="dark"]'):]
        dark = dark[:dark.index('}')]
        assert token in dark, f'{token} keeps its light value in dark mode'


class TestStatusIsNeverColourAlone:
    """Status colours are reserved, and every one of them ships beside a
    written label or a shape. A screenshot in a chat window, a greyscale
    print and a reader with a colour vision deficiency all have to work."""

    def test_the_four_status_roles_exist(self, declared):
        for role in ('--status-good', '--status-warning', '--status-serious',
                     '--status-critical'):
            assert role in declared

    def test_the_severity_cell_carries_a_written_label(self):
        """The board's score column. The dot is reinforcement."""
        board = read(ROOT / 'web' / 'templates' / 'board.html')
        assert 'sev-label' in board
        assert 'severity|capitalize' in board.replace(' ', '')

    def test_the_tiles_carry_a_note_not_only_a_colour(self):
        board = read(ROOT / 'web' / 'templates' / 'board.html')
        tile_macro = board[board.index('{% macro tile('):board.index('{% endmacro %}')]
        assert 'note' in tile_macro


# ---------------------------------------------------------------------------
# the emails
# ---------------------------------------------------------------------------

LOCAL_PALETTE = re.compile(
    r"\{%-?\s*set\s+(INK2?|SURFACE|SUNKEN|PLANE|RULE|BORDER|MUTED|ACCENT|"
    r"ACCENT_SOFT|CRITICAL|SERIOUS|WARNING|GOOD|FONT|RADIUS|TINT_\w+)\s*=",
    re.I)


class TestTheEmailsShareOnePalette:
    def test_no_template_declares_its_own_colours(self):
        offenders = {}
        for path in MAIL_TEMPLATES:
            names = sorted(set(LOCAL_PALETTE.findall(read(path))))
            if names:
                offenders[path.name] = names
        assert offenders == {}, (
            'these templates would drift from the rest: ' + repr(offenders))

    def test_the_templates_were_actually_found(self):
        assert len(MAIL_TEMPLATES) >= 5

    def test_every_token_a_template_uses_is_supplied(self):
        """A missing token renders as the empty string, so `color:;` - which
        most clients ignore, leaving black text on a dark band."""
        supplied = set(EMAIL_TOKENS) | {
            'display_tz', 'display_tz_label', 'display_tz_abbrev',
            'team_label', 'PENDING_ACTION_OWNER_LABELS',
        }
        # Names that look like tokens: SHOUTY_CASE inside {{ }}.
        offenders = {}
        for path in MAIL_TEMPLATES:
            used = set(re.findall(r'\{\{\s*([A-Z][A-Z0-9_]{2,})\s*\}\}',
                                  read(path)))
            missing = sorted(used - supplied)
            if missing:
                offenders[path.name] = missing
        assert offenders == {}

    def test_the_palette_is_light_surfaced_on_purpose(self):
        """Automatic dark-mode inversion in email is unreliable across
        clients and tends to produce a dark card with black text on it.
        Naming the surface is the reliable outcome."""
        assert EMAIL_TOKENS['SURFACE'].lower() in ('#ffffff', '#fff')

    def test_status_colours_clear_contrast_on_that_surface(self):
        """Email has no hover, no tooltip and no table view to fall back on,
        so a status colour that is merely decorative on screen has to be
        readable here."""
        def luminance(hex_colour):
            hex_colour = hex_colour.lstrip('#')
            parts = [int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            parts = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                     for c in parts]
            return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

        surface = luminance(EMAIL_TOKENS['SURFACE'])
        for role in ('CRITICAL', 'SERIOUS', 'WARNING', 'GOOD'):
            ink = luminance(EMAIL_TOKENS[role])
            ratio = (max(surface, ink) + 0.05) / (min(surface, ink) + 0.05)
            # 3:1 is the floor for large text and for a UI component's own
            # colour; these are used at 21-28px in the stat tiles.
            assert ratio >= 3.0, f'{role} is {ratio:.2f}:1 on the card surface'


class TestTheSettingsGridIsPopulated:
    """Every area of configuration, visible at once as separate controls.

    The failure this catches is specific and quiet. The template reads
    `entry.key`, `entry.label`, `entry.icon` off each section; Jinja's
    default Undefined renders as the empty string rather than raising, so
    feeding it the wrong shape - a list of tuples instead of a list of
    dicts - produces seven blank cards, a 200, and no error anywhere. A
    smoke test that only checks "did it render" passes.
    """

    def test_every_section_gets_a_card(self, grid):
        for _key, label, _blurb, _icon in SECTIONS:
            assert label in grid, f'{label} has no card'

    def test_the_cards_carry_their_section_link(self, grid):
        for key, _label, _blurb, _icon in SECTIONS:
            assert f'?section={key}' in grid, key

    def test_no_card_is_blank(self, grid):
        """The symptom of the wrong shape: the markup is there, the text is
        not."""
        headings = re.findall(r'<h3>\s*(.*?)\s*(?:<span|</h3>)', grid, re.S)
        assert len(headings) >= len(SECTIONS)
        assert all(h.strip() for h in headings), headings

    def test_each_card_states_how_many_settings_it_holds(self, grid):
        """So the reader can tell a section they have not opened from one
        that is empty."""
        assert re.search(r'\d+ settings?', grid)

    def test_the_current_section_is_marked(self, grid):
        assert 'settings-card active' in grid
        assert 'aria-current' in grid

    def test_the_neighbouring_pages_are_in_the_grid_too(self, grid):
        """Weights, users and the export are configuration as far as the
        reader is concerned, even though they are not conf.json keys."""
        for href in ('/settings/weights', '/admin/users', '/admin/settings/export'):
            assert href in grid


class TestTheEmailsStayTableBased:
    """Outlook renders through Word, which has no flexbox and no grid. A
    layout that works in every webmail client and collapses in Outlook is
    the single most common email regression."""

    @pytest.mark.parametrize('path', MAIL_TEMPLATES, ids=lambda p: p.name)
    def test_no_flexbox_or_grid_layout(self, path):
        body = markup(path).replace(' ', '')
        assert 'display:flex' not in body
        assert 'display:grid' not in body

    @pytest.mark.parametrize('path', MAIL_TEMPLATES, ids=lambda p: p.name)
    def test_no_external_or_embedded_stylesheet(self, path):
        body = markup(path).lower()
        assert '<style' not in body
        assert '<link' not in body

    @pytest.mark.parametrize('path', MAIL_TEMPLATES, ids=lambda p: p.name)
    def test_the_outer_layout_is_a_table(self, path):
        assert '<table' in markup(path)
