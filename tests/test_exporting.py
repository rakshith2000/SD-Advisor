"""Board exports.

These files leave the building. Nobody re-checks a spreadsheet against the
page it came from, so the properties that matter are the ones a reader
cannot verify for themselves:

  * it contains exactly what the board contained - same scope, same filters
  * it says what was filtered out, so nine rows cannot be mistaken for a
    clean desk
  * it says when it was truncated, because a silently short export looks
    complete
  * every column is distinguishable from every other column

That last one is not pedantry. The signal `sla_breached` and the risk flag
`SLA_BREACHED` are different values that are allowed to differ, and the
first version of this shipped two columns both headed "SLA breached" - a
filter applied to the wrong one, with nothing on screen to say so.

    python -m pytest tests/test_exporting.py -q
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from delivery.digest import FLAG_LABELS, DigestBuilder, owner_labels
from web import exporting

ROOT = Path(__file__).resolve().parent.parent

openpyxl = pytest.importorskip('openpyxl')


class FakeSettings:
    assignment_groups = ['Service Desk', 'Desktop Support']
    shadow_mode = False
    aged_after_days = 5

    def get(self, key, default=None):
        return default


def decorate(raw):
    builder = DigestBuilder.__new__(DigestBuilder)
    builder.settings = FakeSettings()
    builder.base_url = 'https://ata.example'
    builder.owner_labels = owner_labels(FakeSettings())
    return builder.decorate(raw)


def raw(number='INC0001', group='Desktop Support', owner='SERVICE_DESK',
        flags=('SLA_BREACHED', 'PROLONGED_INACTIVITY'), **extra):
    base = {'incident_number': number, 'attention_score': 82,
            'assignment_group': group, 'pending_action_owner': owner,
            'risk_flags': list(flags), 'age_minutes': 15150,
            'idle_minutes': 7200, 'age_days': 10.52, 'idle_days': 5.0,
            'short_description': 'Mapped drives drop off the VPN',
            'assigned_to': 'Asha Rao', 'caller_name': 'J Smith',
            'state': 'In Progress', 'priority': '2 - High',
            'category': 'Network', 'subcategory': 'VPN',
            'sla_breached': 1, 'sla_pct_consumed': 118.4,
            'caller_replied_unanswered': 1, 'dependency_resolved': 0,
            'auto_close_candidate': 0, 'duration_overrun': 1,
            'kb_available': 1, 'kb_attached': 0,
            'recommended_action': 'FOLLOW_UP_CALLER', 'confidence': 0.78,
            'rationale': 'Caller replied six days ago.',
            'lead_feedback': 'Chase this one.', 'snoozed': 0}
    base.update(extra)
    return decorate(base)


CONTEXT = {
    'generated_at': '02 Nov 2026 09:15', 'timezone': 'CST',
    'exported_by': 'Asha Rao',
    'filters_described': ['Queues: Desktop Support', 'Minimum score: 40',
                          'Snoozed tickets excluded'],
}


@pytest.fixture
def rows():
    return [raw('INC0001'), raw('INC0002', owner='CALLER', flags=()),
            raw('INC0003', group='')]


@pytest.fixture
def agents(rows):
    builder = DigestBuilder.__new__(DigestBuilder)
    return builder.by_agent(rows)


@pytest.fixture
def book(rows, agents):
    data = exporting.build_workbook(rows, agents, CONTEXT)
    return openpyxl.load_workbook(io.BytesIO(data))


def headings(sheet):
    return [cell.value for cell in sheet[1]]


def record(sheet, index=2):
    return dict(zip(headings(sheet), [c.value for c in sheet[index]]))


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------

class TestEveryColumnIsDistinguishable:
    def test_no_heading_appears_twice(self):
        """The bug this caught: `sla_breached` the signal and SLA_BREACHED
        the risk flag are different values, both headed "SLA breached".
        A filter applied to whichever Excel reached first."""
        names = exporting.ticket_headings()
        duplicates = sorted({n for n in names if names.count(n) > 1})
        assert duplicates == []

    def test_flags_are_prefixed(self):
        names = exporting.ticket_headings()
        for label in FLAG_LABELS.values():
            assert f'Flag: {label}' in names

    def test_every_known_flag_gets_its_own_column(self):
        """A comma-joined cell cannot be filtered without text matching,
        and "SLA breached" also matches "Vendor SLA breached"."""
        names = exporting.ticket_headings()
        assert len([n for n in names if n.startswith('Flag: ')]) == len(FLAG_LABELS)

    def test_the_agent_sheet_has_no_duplicates_either(self):
        names = [h for h, _ in exporting.AGENT_COLUMNS]
        assert len(names) == len(set(names))


class TestTheTicketSheet:
    def test_one_row_per_ticket(self, book, rows):
        assert book['Tickets'].max_row == len(rows) + 1

    def test_the_flag_column_reflects_the_row(self, book):
        first = record(book['Tickets'], 2)
        second = record(book['Tickets'], 3)
        assert first['Flag: SLA breached'] == 'Yes'
        assert second['Flag: SLA breached'] == 'No'

    def test_the_owning_team_is_named(self, book):
        assert record(book['Tickets'], 2)['Pending action owner'] == 'Desktop Support'

    def test_the_stored_token_is_exported_alongside_the_label(self, book):
        """So a pivot groups by a stable value rather than by a label that
        changes when somebody renames a queue."""
        assert record(book['Tickets'], 2)['Pending action owner (code)'] == 'SERVICE_DESK'

    def test_durations_are_exported_as_numbers_too(self, book):
        """"10 Days 12 Hrs" is text and sorts as text. A pivot needs the
        minutes."""
        first = record(book['Tickets'], 2)
        assert first['Age'] == '10 Days 12 Hrs 30 Mins'
        assert first['Age (minutes)'] == 15150
        assert isinstance(first['Age (minutes)'], int)

    def test_booleans_are_words_not_numbers(self, book):
        """A wall of 1s and 0s does not distinguish "no" from "not
        recorded"."""
        first = record(book['Tickets'], 2)
        assert first['Caller awaiting reply'] == 'Yes'
        assert first['Dependency resolved'] == 'No'

    def test_a_missing_value_is_marked_rather_than_guessed(self):
        assert exporting._yes_no(None) == '-'

    def test_a_missing_number_is_blank_not_zero(self):
        """A zero would be averaged in and pull every mean down."""
        assert exporting._num(None) is None
        assert exporting._num('') is None

    def test_the_sheet_is_filterable_and_frozen(self, book):
        """The reason to produce xlsx rather than csv."""
        sheet = book['Tickets']
        assert sheet.freeze_panes == 'A2'
        assert sheet.auto_filter.ref

    def test_rows_keep_the_board_order(self, book, rows):
        exported = [record(book['Tickets'], i + 2)['Incident']
                    for i in range(len(rows))]
        assert exported == [r['incident_number'] for r in rows]


class TestTheAgentSheet:
    def test_it_exists_as_its_own_sheet(self, book):
        assert 'By agent' in book.sheetnames

    def test_it_is_not_the_first_sheet(self, book):
        """Tickets is what the file is for; By agent is the second look."""
        assert book.sheetnames[0] == 'Tickets'

    def test_one_row_per_agent(self, book, agents):
        assert book['By agent'].max_row == len(agents) + 1

    def test_the_compliance_column_survives_a_missing_link(self, book):
        """The audit database is optional. Its column must be empty, not
        absent - a disappearing column breaks a saved pivot."""
        assert 'Audit compliance (%)' in headings(book['By agent'])
        assert record(book['By agent'], 2)['Audit compliance (%)'] is None


class TestTheAboutSheet:
    """Six months later, "where did this come from and what was filtered
    out" is the only question anybody asks of a spreadsheet, and it is the
    one thing a spreadsheet never records."""

    def pairs(self, book):
        return [(r[0].value, r[1].value) for r in
                book['About'].iter_rows(min_col=1, max_col=2)]

    def test_it_names_who_exported_it(self, book):
        assert ('Exported by', 'Asha Rao') in self.pairs(book)

    def test_it_records_every_filter(self, book):
        applied = [v for k, v in self.pairs(book) if k == 'Filter applied']
        assert applied == CONTEXT['filters_described']

    def test_it_carries_the_handling_note(self, book):
        """The file contains caller names and leaves the building."""
        note = dict(self.pairs(book)).get('Handling') or ''
        assert 'caller names' in note

    def test_it_says_nothing_was_changed_in_servicenow(self, book):
        note = dict(self.pairs(book)).get('Note') or ''
        assert 'Nothing in ServiceNow has been changed' in note

    def test_an_untruncated_export_carries_no_warning(self, book):
        assert 'TRUNCATED' not in dict(self.pairs(book))


class TestTruncation:
    def test_under_the_cap_nothing_is_dropped(self, rows):
        kept, dropped = exporting.truncate(rows)
        assert len(kept) == len(rows) and dropped == 0

    def test_over_the_cap_is_capped(self):
        kept, dropped = exporting.truncate([{'n': i}
                                            for i in range(exporting.MAX_ROWS + 25)])
        assert len(kept) == exporting.MAX_ROWS
        assert dropped == 25

    def test_the_file_says_so(self, agents):
        """A silently short export looks complete, which is worse than a
        refused one."""
        many = [raw(f'INC{i:05d}') for i in range(5)] * 1100
        data = exporting.build_workbook(many, agents, CONTEXT)
        book = openpyxl.load_workbook(io.BytesIO(data))
        warning = dict((r[0].value, r[1].value) for r in
                       book['About'].iter_rows(min_col=1, max_col=2))
        assert 'TRUNCATED' in warning
        assert str(len(many) - exporting.MAX_ROWS) in warning['TRUNCATED']


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

class TestPdf:
    @pytest.fixture(autouse=True)
    def _needs_reportlab(self):
        pytest.importorskip('reportlab')

    def test_it_produces_a_pdf(self, rows, agents):
        data = exporting.build_pdf(rows, agents, CONTEXT)
        assert data[:4] == b'%PDF'

    def test_by_agent_is_on_its_own_page(self, rows, agents):
        data = exporting.build_pdf(rows, agents, CONTEXT)
        pages = data.count(b'/Type /Page') - data.count(b'/Type /Pages')
        assert pages >= 2

    def test_an_empty_board_still_renders(self, agents):
        """Exporting a clean desk must produce a file saying so, not a
        stack trace."""
        assert exporting.build_pdf([], [], CONTEXT)[:4] == b'%PDF'

    def test_markup_in_a_description_cannot_break_the_layout(self, agents):
        """ServiceNow content reaches these cells verbatim, and reportlab
        parses a mini-HTML in paragraphs - an unescaped < ends the run."""
        row = raw('INC0009', **{'short_description': 'Error <b>500</b> & "quotes"'})
        assert exporting.build_pdf([row], agents, CONTEXT)[:4] == b'%PDF'

    def test_it_carries_fewer_columns_than_the_workbook(self):
        """Deliberate. Thirty columns on A4 is six-point type, and a report
        that cannot be read is not a smaller version of one that can."""
        assert len(exporting.PDF_COLUMNS) < len(exporting.ticket_headings())

    def test_it_points_at_the_workbook_for_the_rest(self):
        source = (ROOT / 'web' / 'exporting.py').read_text(encoding='utf-8')
        assert 'in the Excel export' in source


# ---------------------------------------------------------------------------
# provenance and naming
# ---------------------------------------------------------------------------

class TestDescribeFilters:
    def test_the_scope_is_always_stated(self):
        lines = exporting.describe_filters({}, ['Service Desk'])
        assert lines[0] == 'Queues: Service Desk'

    def test_an_empty_scope_says_so(self):
        """Otherwise an empty file reads as a clean desk."""
        assert 'none in scope' in exporting.describe_filters({}, [])[0]

    def test_each_applied_filter_is_named(self):
        lines = exporting.describe_filters(
            {'agent': 'Asha Rao', 'flag': 'SLA_BREACHED', 'min_score': 40},
            ['Service Desk'])
        joined = ' | '.join(lines)
        assert 'Agent: Asha Rao' in joined
        assert 'Risk flag: SLA_BREACHED' in joined
        assert 'Minimum score: 40' in joined

    def test_unapplied_filters_are_not_listed(self):
        lines = exporting.describe_filters({'agent': None, 'min_score': 0},
                                           ['Service Desk'])
        assert not any('Agent' in line for line in lines)
        assert not any('Minimum score' in line for line in lines)

    def test_the_snooze_state_is_always_stated_either_way(self):
        """It changes the row count and is off by default, so leaving it
        unsaid is how a short export gets misread."""
        assert 'Snoozed tickets excluded' in exporting.describe_filters(
            {}, ['x'])
        assert 'Snoozed tickets included' in exporting.describe_filters(
            {'show_snoozed': True}, ['x'])


class TestFilenames:
    def test_it_is_dated(self):
        import datetime
        name = exporting.filename('aged-tickets', 'xlsx',
                                  now=datetime.datetime(2026, 11, 2))
        assert name == 'aged-tickets-2026-11-02.xlsx'

    def test_the_queue_is_included_when_one_is_selected(self):
        import datetime
        name = exporting.filename('aged-tickets', 'pdf', 'Desktop Support',
                                  now=datetime.datetime(2026, 11, 2))
        assert name == 'aged-tickets-desktop-support-2026-11-02.pdf'

    def test_a_queue_name_cannot_escape_the_filename(self):
        """It reaches a Content-Disposition header."""
        name = exporting.filename('x', 'pdf', 'A/B"; drop\\table')
        assert '"' not in name and '/' not in name and '\\' not in name


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def source():
    return (ROOT / 'web' / 'app.py').read_text(encoding='utf-8')


class TestTheRoutes:
    def test_both_formats_are_exposed(self, source):
        assert "@app.get('/board/export.xlsx')" in source
        assert "@app.get('/board/export.pdf')" in source

    def test_export_is_lead_only(self, source):
        for route in ('def export_board_excel(', 'def export_board_pdf('):
            start = source.index(route)
            signature = source[start:source.index('):', start)]
            assert 'require_lead' in signature, route

    def test_the_board_and_the_export_share_one_selection(self, source):
        """Two copies of the scope-and-filter logic would eventually
        disagree, and the way anybody would find out is a spreadsheet
        containing rows the screen did not."""
        assert source.count('def _board_selection(') == 1
        assert source.count('_board_selection(user, group, agent') >= 2

    def test_the_selection_respects_scope(self, source):
        body = source[source.index('def _board_selection('):]
        body = body[:body.index('def _export_payload(')]
        assert 'narrow_scope(user, group)' in body
        assert 'if not groups' in body

    def test_a_missing_library_is_a_message_not_a_stack_trace(self, source):
        assert source.count('except exporting.ExportUnavailable') == 2
        assert source.count('status_code=503') >= 2

    def test_the_extraction_is_logged(self, source):
        """A bulk download of incident descriptions and caller names is
        worth a line naming who took it and how much."""
        for route in ('export_board_excel', 'export_board_pdf'):
            body = source[source.index(f'def {route}('):]
            body = body[:body.index('return Response')]
            assert 'log.warning' in body, route

    def test_the_board_offers_both_buttons(self):
        board = (ROOT / 'web' / 'templates' / 'board.html').read_text(encoding='utf-8')
        assert '/board/export.xlsx' in board
        assert '/board/export.pdf' in board

    def test_the_buttons_carry_the_current_filters(self):
        """Otherwise the file is the unfiltered board and nobody notices."""
        board = (ROOT / 'web' / 'templates' / 'board.html').read_text(encoding='utf-8')
        assert 'request.url.query' in board
