"""The insight pages.

Eight sections behind one tab strip. The properties worth pinning are the
ones a reader cannot check for themselves:

  * **Scope lives in the SQL.** Every ticket query carries the caller's
    queues. A chart that aggregates the whole estate and is captioned with
    one queue's name is a disclosure with nothing on screen to notice it by -
    an aggregate carries no incident numbers.
  * **An empty scope yields an empty page, never the estate.** The clause
    that does it matches nothing rather than being omitted.
  * **Every section renders.** They share one template, so a panel shape one
    section gets wrong takes out a page rather than a panel.
  * **A bad time range falls back; it does not raise.** The range is a view
    preference and lives in the URL, where people edit it by hand.

    python -m pytest tests/test_insights.py -q
"""

import datetime
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import charts
from web import insights
from web.insights import Insights, parse_range

ROOT = Path(__file__).resolve().parent.parent
TODAY = datetime.date(2026, 11, 4)


# ---------------------------------------------------------------------------
# time range
# ---------------------------------------------------------------------------

class TestTimeRange:
    @pytest.mark.parametrize('key,days', [('7d', 7), ('30d', 30),
                                          ('90d', 90), ('180d', 180)])
    def test_the_presets_span_what_they_say(self, key, days):
        window = parse_range(key, None, None, TODAY)
        assert window.key == key
        assert window.days == days
        assert window.end == TODAY

    def test_nothing_asked_for_gives_the_default(self):
        assert parse_range(None, None, None, TODAY).key == insights.DEFAULT_RANGE

    @pytest.mark.parametrize('rubbish', ['', '7', 'yesterday', 'all-time',
                                         '9999d', '../../etc/passwd'])
    def test_rubbish_falls_back_rather_than_raising(self, rubbish):
        """The range lives in the URL where people edit it by hand. Refusing
        to render the page is the wrong severity of response."""
        assert parse_range(rubbish, None, None,
                           TODAY).key == insights.DEFAULT_RANGE

    def test_a_custom_range_is_honoured(self):
        window = parse_range('custom', '2026-10-01', '2026-10-15', TODAY)
        assert window.is_custom
        assert window.start == datetime.date(2026, 10, 1)
        assert window.end == datetime.date(2026, 10, 15)

    def test_a_reversed_range_is_swapped_not_rejected(self):
        window = parse_range('custom', '2026-10-15', '2026-10-01', TODAY)
        assert window.start < window.end

    def test_an_end_in_the_future_is_clamped(self):
        """Future columns draw as empty bars, which read as an outage."""
        assert parse_range('custom', '2026-10-01', '2027-06-01',
                           TODAY).end == TODAY

    def test_a_start_before_retention_is_clamped(self):
        """ticket_signal is pruned at 180 days. A longer window draws a flat
        line across the missing part, which reads as a quiet period rather
        than as an absence of data."""
        window = parse_range('custom', '2020-01-01', '2026-11-01', TODAY)
        assert (TODAY - window.start).days <= insights.MAX_RANGE_DAYS

    @pytest.mark.parametrize('bad', ['not-a-date', '2026-13-45', '01/02/2026'])
    def test_an_unparseable_date_falls_back(self, bad):
        assert parse_range('custom', bad, '2026-10-15',
                           TODAY).key == insights.DEFAULT_RANGE

    def test_the_label_describes_the_window(self):
        assert parse_range('30d', None, None, TODAY).label == 'Last 30 days'
        assert '2026' in parse_range('custom', '2026-10-01', '2026-10-15',
                                     TODAY).label

    def test_until_covers_the_whole_final_day(self):
        """A midnight boundary silently drops everything recorded today,
        which is the day people ask about most."""
        window = parse_range('7d', None, None, TODAY)
        assert window.until.hour == 23 and window.until.minute == 59
        assert window.until.date() == window.end


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------

class RecordingDb:
    """Captures every statement so the scope clause can be asserted on."""

    def __init__(self, rows=None):
        self.statements = []
        self.rows = [] if rows is None else rows

    def query(self, sql, params=None):
        self.statements.append((' '.join(sql.split()), list(params or [])))
        return list(self.rows)

    def get_sync_watermark(self, name):
        return None


class Ctx:
    def __init__(self, db):
        self.db = db
        self.settings = type('S', (), {'aged_after_days': 5})()


def build(section, groups=('Service Desk',), rows=None):
    db = RecordingDb(rows)
    engine = Insights(Ctx(db), list(groups),
                      parse_range('30d', None, None, TODAY))
    return engine.build(section), db


def ticket_queries(db):
    return [(sql, params) for sql, params in db.statements
            if 'v_current_board' in sql or 'signal_daily' in sql]


class TestScopeIsEnforcedInSql:
    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_every_ticket_query_names_the_queue(self, section):
        """Not every query is queue-scoped - model latency and token spend
        are service-wide facts - but anything touching ticket data must pass
        the groups through."""
        _data, db = build(section, groups=['Service Desk'])
        for sql, params in ticket_queries(db):
            assert 'Service Desk' in params, sql[:140]

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_an_empty_scope_matches_nothing(self, section):
        """The inversion that matters. An omitted WHERE clause turns
        "entitled to nothing" into "entitled to the estate", which reads as
        success."""
        _data, db = build(section, groups=[])
        for sql, _params in ticket_queries(db):
            assert '1 = 0' in sql, sql[:140]

    def test_several_queues_are_all_passed(self):
        _data, db = build('operations',
                          groups=['Service Desk', 'Desktop Support'])
        board = [p for sql, p in db.statements if 'v_current_board' in sql]
        assert board and all('Desktop Support' in p for p in board)

    def test_the_clause_is_parameterised(self):
        """A queue name is user-supplied data reaching SQL. Inlined, a name
        with an apostrophe in it is an injection point."""
        _data, db = build('operations', groups=["O'Brien's Team"])
        for sql, params in ticket_queries(db):
            assert "O'Brien" not in sql
            assert "O'Brien's Team" in params

    def test_the_aliased_scope_is_also_parameterised(self):
        """The silent-breach query is aliased, so it builds its clause
        against b.assignment_group rather than patching the generated SQL."""
        _data, db = build('risk', groups=['Service Desk'])
        aliased = [sql for sql, _p in db.statements
                   if 'alert_log' in sql and 'NOT EXISTS' in sql]
        assert aliased
        assert 'b.assignment_group IN' in aliased[0]


# ---------------------------------------------------------------------------
# every section builds
# ---------------------------------------------------------------------------

class TestEverySectionBuilds:
    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_the_shape_is_what_the_template_expects(self, section):
        data, _db = build(section)
        assert isinstance(data.get('tiles', []), list)
        assert isinstance(data.get('panels', []), list)
        for item in data['panels']:
            assert item['kind'] in ('chart', 'table'), item['title']

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_it_survives_an_empty_database(self, section):
        """A new deployment has no rollup and no feedback. Every section must
        say so rather than divide by zero."""
        data, _db = build(section, rows=[])
        assert data['panels']

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_it_survives_a_broken_database(self, section):
        """One failing query degrades its panel, not the page."""
        class Broken(RecordingDb):
            def query(self, sql, params=None):
                raise RuntimeError('table does not exist')

        engine = Insights(Ctx(Broken()), ['Service Desk'],
                          parse_range('30d', None, None, TODAY))
        assert engine.build(section)['panels']

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_every_panel_declares_a_width_the_css_knows(self, section):
        data, _db = build(section)
        for item in data['panels']:
            assert item['width'] in ('full', 'half', 'third'), item['title']

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_every_toned_tile_also_has_words(self, section):
        """Status colour never carries meaning alone: the note is what
        survives a greyscale print."""
        data, _db = build(section)
        for item in data['tiles']:
            if item.get('tone'):
                assert item['note'], item['label']

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_every_table_says_what_empty_means(self, section):
        """"Nothing aged" and "the query failed" look identical as a blank
        area. The empty text is the only thing that tells them apart."""
        data, _db = build(section)
        for item in data['panels']:
            if item['kind'] == 'table':
                assert item['empty'], item['title']

    @pytest.mark.parametrize('section', insights.SECTION_KEYS)
    def test_a_linked_column_is_in_range(self, section):
        """The template turns column `link` into a ticket href. Off the end
        of the row it would render a link to nothing."""
        data, _db = build(section)
        for item in data['panels']:
            if item['kind'] == 'table' and item.get('link') is not None:
                assert 0 <= item['link'] < len(item['headings']), item['title']

    def test_the_registry_and_keys_agree(self):
        assert insights.SECTION_KEYS == [k for k, _l, _b in insights.SECTIONS]

    def test_there_are_eight_sections(self):
        assert len(insights.SECTIONS) == 8

    def test_rollup_absence_is_explained_not_drawn_as_zero(self):
        """A flat line at zero is indistinguishable from a quiet month."""
        data, _db = build('backlog', rows=[])
        text = ' '.join(p.get('footnote', '') + p['title']
                        for p in data['panels'])
        assert 'rollup' in text.lower()


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------

class TestCharts:
    def test_a_line_chart_is_balanced_svg(self):
        svg = str(charts.line_chart(['Mon', 'Tue', 'Wed'],
                                    [{'name': 'Aged', 'values': [3, 5, 4]}]))
        assert svg.startswith('<svg') and svg.endswith('</svg>')
        assert svg.count('<svg') == svg.count('</svg>') == 1

    def test_charts_carry_an_accessible_name(self):
        """A screen reader should hear one chart with a summary, not a pile
        of shapes."""
        svg = str(charts.line_chart(['Mon', 'Tue'],
                                    [{'name': 'Aged', 'values': [1, 2]}]))
        assert 'role="img"' in svg and 'aria-label=' in svg
        assert '<desc>' in svg

    def test_marks_carry_a_hover_title(self):
        assert '<title>Caller: 9</title>' in str(
            charts.hbar_chart([{'label': 'Caller', 'value': 9}]))

    @pytest.mark.parametrize('maker', [
        lambda: charts.line_chart(['a', 'b'],
                                  [{'name': 'x', 'values': [1, 2]}]),
        lambda: charts.bar_chart(['a', 'b'],
                                 [{'name': 'x', 'values': [1, 2]}]),
        lambda: charts.hbar_chart([{'label': 'a', 'value': 1}]),
        lambda: charts.heatmap(['a'], ['b'], [[1]]),
        lambda: charts.gauge(60, 100, 'x'),
        lambda: charts.calibration_chart(['50-60%'], [55], [57]),
        lambda: charts.stacked_hbar([{'label': 'a', 'values': [1, 2]}],
                                    [{'name': 'x'}, {'name': 'y'}]),
    ])
    def test_colour_is_a_variable_never_a_literal(self, maker):
        """Dark mode is the same markup against different variables. A baked
        hex would be a second rendering path that drifts."""
        svg = str(maker())
        assert 'var(--' in svg
        assert not re.search(r'(?:fill|stroke)="#[0-9a-fA-F]{3,8}"', svg)

    def test_content_is_escaped(self):
        """Short descriptions and queue names reach these labels verbatim
        from ServiceNow."""
        svg = str(charts.hbar_chart(
            [{'label': '<script>alert(1)</script>', 'value': 1}]))
        assert '<script>' not in svg
        assert '&lt;script&gt;' in svg

    def test_an_empty_series_says_so_in_words(self):
        """An empty frame reads as a broken panel and a blank area reads as
        a missing one; neither says "there is genuinely nothing here"."""
        svg = str(charts.line_chart(['a'], [{'name': 'x', 'values': []}]))
        assert 'No data' in svg

    def test_a_stacked_segment_is_separated_by_a_gap_not_a_stroke(self):
        svg = str(charts.bar_chart(
            ['a'], [{'name': 'x', 'values': [5]},
                    {'name': 'y', 'values': [5]}], stacked=True))
        for rect in re.findall(r'<rect[^>]*>', svg):
            assert 'stroke=' not in rect

    def test_bars_are_capped_so_a_sparse_chart_is_not_slabs(self):
        svg = str(charts.bar_chart(['a', 'b'],
                                   [{'name': 'x', 'values': [1, 2]}]))
        widths = [float(w) for w in
                  re.findall(r'<rect[^>]*\swidth="([\d.]+)"', svg)]
        assert widths and max(widths) <= charts.BAR_MAX + 0.5

    def test_a_line_point_has_a_hit_target_bigger_than_the_line(self):
        """A 2px line is impossible to hover."""
        svg = str(charts.line_chart(['a', 'b'],
                                    [{'name': 'x', 'values': [1, 2]}]))
        assert 'r="9" fill="transparent"' in svg

    def test_a_sparkline_needs_two_points(self):
        assert str(charts.sparkline([1])) == ''
        assert str(charts.sparkline([1, 2, 3])).startswith('<svg')

    def test_a_gauge_reports_its_proportion(self):
        assert '62%' in str(charts.gauge(62, 100, 'Coverage'))

    def test_a_zero_maximum_does_not_divide_by_zero(self):
        assert '0%' in str(charts.gauge(0, 0, 'Nothing'))

    def test_the_calibration_chart_keeps_what_it_was_given(self):
        svg = str(charts.calibration_chart(['50-60%', '90-100%'],
                                           [55, 95], [57, 84]))
        assert '57%' in svg and '84%' in svg

    def test_formatting_separates_absent_from_zero(self):
        assert charts.fmt(None) == charts.DASH
        assert charts.fmt(0) == '0'
        assert charts.fmt(1284) == '1,284'
        assert charts.fmt(12.0) == '12'


# ---------------------------------------------------------------------------
# the route and the nav entry
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def app_source():
    return (ROOT / 'web' / 'app.py').read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def route_body(app_source):
    start = app_source.index('def insights_section(')
    return app_source[start:app_source.index('# ----', start)]


class TestTheRoute:
    def test_it_is_gated_to_admins_and_leads(self, app_source):
        start = app_source.index('def insights_section(')
        assert 'require_lead' in app_source[start:app_source.index('):', start)]

    def test_an_unknown_section_is_404_not_a_silent_default(self, route_body):
        assert 'SECTION_KEYS' in route_body and '404' in route_body

    def test_a_failing_section_does_not_take_the_page_down(self, route_body):
        assert 'except Exception' in route_body

    def test_the_range_travels_between_tabs(self, route_body):
        """Changing section must not silently snap the window back."""
        assert 'carried' in route_body and 'range=' in route_body

    def test_it_is_scoped_from_the_account(self, route_body):
        assert 'scope_of(user)' in route_body

    def test_the_nav_entry_is_role_gated(self):
        base = (ROOT / 'web' / 'templates' / 'base.html'
                ).read_text(encoding='utf-8')
        nav = base[base.index('<nav class="nav">'):base.index('</nav>')]
        assert '/insights' in nav
        assert "user.role in ('ADMIN', 'LEAD')" in nav[:nav.index('/insights')]

    def test_the_rollup_runs_before_the_prune(self):
        """The prune drops ticket_signal rows past retention and the rollup
        is built from them. The other order loses the oldest day of every
        trend, once, every night."""
        jobs = (ROOT / 'scheduler' / 'jobs.py').read_text(encoding='utf-8')
        assert jobs.index('DailyRollup') < jobs.index("result['pruned']")


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

class TestNothingIsDrawnOutsideItsFrame:
    """The layout class of bug, caught without a rasteriser.

    Charts are generated geometry, and the failures are a label that runs
    off the card, a bar wider than its track, a negative dimension that the
    browser silently drops. None of those raise and none show up in a
    structural test - but every one of them is a coordinate outside the
    viewBox, which is checkable arithmetic.
    """

    CHARTS = {
        'line': lambda: charts.line_chart(
            ['12w ago', '6w', 'now'],
            [{'name': 'Aged', 'values': [34, 51, 47], 'area': True},
             {'name': 'Breached', 'values': [4, 9, 6]}],
            target=40, target_label='target'),
        'bar': lambda: charts.bar_chart(
            ['a', 'b', 'c'], [{'name': 'x', 'values': [3, 9, 5]},
                              {'name': 'y', 'values': [2, 4, 8]}]),
        'stacked': lambda: charts.bar_chart(
            ['a', 'b'], [{'name': 'x', 'values': [3, 9]},
                         {'name': 'y', 'values': [2, 4]},
                         {'name': 'z', 'values': [1, 2]},
                         {'name': 'w', 'values': [5, 1]}], stacked=True),
        'hbar': lambda: charts.hbar_chart(
            [{'label': 'Prolonged inactivity', 'value': 11},
             {'label': 'Caller awaiting reply', 'value': 9},
             {'label': 'Nothing', 'value': 0}]),
        'stacked_hbar': lambda: charts.stacked_hbar(
            [{'label': 'Service Desk', 'values': [2.8, 3.2, 4.4]}],
            [{'name': 'In Progress'}, {'name': 'On Hold'},
             {'name': 'Awaiting caller'}]),
        'heatmap': lambda: charts.heatmap(
            ['Asha Rao', '(unassigned)'], ['5-10d', '10-20d', '30+d'],
            [[6, 3, 1], [1, 1, 2]]),
        'gauge': lambda: charts.gauge(62, 100, 'Review coverage'),
        'calibration': lambda: charts.calibration_chart(
            ['50-60%', '60-70%', '90-100%'], [55, 65, 95], [57, 66, 84]),
        'empty': lambda: charts.empty_chart('Nothing to show'),
    }

    @staticmethod
    def _box(svg):
        return [float(v) for v in
                re.search(r'viewBox="([^"]+)"', svg).group(1).split()]

    @pytest.mark.parametrize('name', sorted(CHARTS))
    def test_no_negative_dimension(self, name):
        """A negative width is dropped silently by the browser, so the mark
        simply is not there and nothing says why."""
        svg = str(self.CHARTS[name]())
        for attr in ('width', 'height', 'r'):
            for value in re.findall(rf'\s{attr}="(-?[\d.]+)"', svg):
                assert float(value) >= 0, f'{name}: negative {attr}'

    @pytest.mark.parametrize('name', sorted(CHARTS))
    def test_every_rect_is_inside_the_viewbox(self, name):
        svg = str(self.CHARTS[name]())
        _x, _y, vw, vh = self._box(svg)
        for rect in re.findall(r'<rect[^>]*>', svg):
            def at(attr, default=0.0):
                found = re.search(rf'\s{attr}="(-?[\d.]+)"', rect)
                return float(found.group(1)) if found else default
            assert at('x') >= -0.5, f'{name}: rect starts left of the frame'
            assert at('x') + at('width') <= vw + 0.5,                 f'{name}: rect runs off the right edge'
            assert at('y') + at('height') <= vh + 0.5,                 f'{name}: rect runs off the bottom'

    @pytest.mark.parametrize('name', sorted(CHARTS))
    def test_every_label_is_inside_the_viewbox(self, name):
        """The bug this catches: a y-axis label rendered at a negative x,
        which clips against the panel edge and reads as a truncated word."""
        svg = str(self.CHARTS[name]())
        _x, _y, vw, vh = self._box(svg)
        for text in re.findall(r'<text[^>]*x="(-?[\d.]+)"[^>]*y="(-?[\d.]+)"',
                               svg):
            tx, ty = float(text[0]), float(text[1])
            assert -1 <= tx <= vw + 1, f'{name}: label at x={tx}, frame {vw}'
            assert -1 <= ty <= vh + 1, f'{name}: label at y={ty}, frame {vh}'

    @pytest.mark.parametrize('name', sorted(CHARTS))
    def test_every_path_point_is_inside_the_viewbox(self, name):
        svg = str(self.CHARTS[name]())
        _x, _y, vw, vh = self._box(svg)
        for path in re.findall(r'<path d="([^"]+)"', svg):
            for px, py in re.findall(r'[ML](-?[\d.]+),(-?[\d.]+)', path):
                assert -1 <= float(px) <= vw + 1, f'{name}: path x off frame'
                assert -1 <= float(py) <= vh + 1, f'{name}: path y off frame'

    def test_a_zero_value_still_draws_a_visible_stub(self):
        """Zero and missing are different facts. A zero-width bar reads as
        an absent row rather than a row whose value is nought."""
        svg = str(charts.hbar_chart([{'label': 'Nothing', 'value': 0}]))
        widths = [float(w) for w in
                  re.findall(r'<rect[^>]*\swidth="([\d.]+)"', svg)]
        assert widths and min(widths) > 0

