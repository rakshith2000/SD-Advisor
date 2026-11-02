"""Board exports: a workbook for analysis, a PDF for circulation.

The two formats are not the same report, and trying to make them the same
report is how both end up bad.

**Excel gets everything.** One column per risk flag, one per signal, one per
recommendation field - thirty-odd columns, which is unreadable on a page and
exactly right in a spreadsheet, where the first thing anybody does is filter
and pivot. This is the "extract the detailed list" case.

**PDF gets what fits.** Landscape A4 holds about eight columns at a legible
size. A PDF carrying all thirty-odd would need six-point type or a scroll of
pages nobody reads, so it carries the ranked list a lead actually works from
and says, in the header, that the full detail is in the workbook. A report
that cannot be read is not a smaller version of one that can.

Both carry the same rows, in the same order, under the same scope and the
same filters as the board that produced them - a download that quietly
contains more than the screen did is the thing to avoid here, because these
files leave the building. Both are generated from already-decorated rows, so
a figure can never differ between the board and the file.

reportlab is imported lazily. PDF is the optional half of this module and a
deployment without it should lose the PDF button, not the whole page.
"""

import datetime
import io
from typing import Any, Dict, List, Optional, Sequence

from core.logging_setup import get_logger
from core.timeutil import utc_now
from delivery.digest import FLAG_LABELS

log = get_logger('web.exporting')

# Beyond this the workbook stops being something a person opens and starts
# being something that times out the worker. The cap is applied with a row
# in the file saying so - a silently truncated export is worse than a
# refused one, because it looks complete.
MAX_ROWS = 5000


class ExportUnavailable(RuntimeError):
    """A format this deployment cannot produce. Message is for the screen."""


# ---------------------------------------------------------------------------
# column definitions
# ---------------------------------------------------------------------------

def _yes_no(value: Any) -> str:
    """Booleans as words.

    TRUE/FALSE and 1/0 both sort and filter oddly in Excel once a column is
    mixed, and a reader scanning a wall of zeroes cannot tell "no" from "not
    recorded". Yes/No/- says which is which.
    """
    if value is None or value == '':
        return '-'
    return 'Yes' if value else 'No'


def _pct(value: Any) -> Any:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> Any:
    """Kept numeric so Excel can sum and sort it.

    Returning '' rather than 0 for a missing value: a zero would be averaged
    in and quietly pull every mean down.
    """
    if value is None or value == '':
        return None
    try:
        return float(value) if isinstance(value, float) else int(value)
    except (TypeError, ValueError):
        return None


# (heading, extractor). Order is the column order in the sheet.
TICKET_COLUMNS = [
    ('Incident', lambda r: r.get('incident_number')),
    ('Short description', lambda r: r.get('short_description')),
    ('Assignment group', lambda r: r.get('assignment_group')),
    ('Assigned to', lambda r: r.get('assigned_to') or 'Unassigned'),
    ('Caller', lambda r: r.get('caller_name')),

    ('Attention score', lambda r: _num(r.get('attention_score'))),
    ('Severity', lambda r: (r.get('severity') or '').title()),

    ('State', lambda r: r.get('state')),
    ('On-hold reason', lambda r: r.get('hold_reason')),
    ('Priority', lambda r: r.get('priority')),
    ('Category', lambda r: r.get('category')),
    ('Subcategory', lambda r: r.get('subcategory')),

    # Both forms. Days is what a person reads; minutes is what a pivot
    # table needs, because "10 Days 12 Hrs" is text and sorts as text.
    ('Age', lambda r: r.get('age_display')),
    ('Age (days)', lambda r: _num(r.get('age_days'))),
    ('Age (minutes)', lambda r: _num(r.get('age_minutes'))),
    ('Idle', lambda r: r.get('idle_display')),
    ('Idle (days)', lambda r: _num(r.get('idle_days'))),
    ('Idle (minutes)', lambda r: _num(r.get('idle_minutes'))),

    ('Pending action owner', lambda r: r.get('pending_action_owner_label')),
    ('Pending action owner (code)', lambda r: r.get('pending_action_owner')),

    ('SLA breached', lambda r: _yes_no(r.get('sla_breached'))),
    ('SLA consumed (%)', lambda r: _pct(r.get('sla_pct_consumed'))),
    ('Projected breach at', lambda r: r.get('projected_breach_at')),

    ('Caller awaiting reply', lambda r: _yes_no(r.get('caller_replied_unanswered'))),
    ('Dependency resolved', lambda r: _yes_no(r.get('dependency_resolved'))),
    ('Closure candidate', lambda r: _yes_no(r.get('auto_close_candidate'))),
    ('Took longer than usual', lambda r: _yes_no(r.get('duration_overrun'))),
    ('Knowledge article available', lambda r: _yes_no(r.get('kb_available'))),
    ('Knowledge article attached', lambda r: _yes_no(r.get('kb_attached'))),

    ('Suggested next step', lambda r: r.get('action_label')),
    ('Suggested next step (code)', lambda r: r.get('recommended_action')),
    ('Suggested target group', lambda r: r.get('suggested_target_group')),
    ('Confidence (%)', lambda r: _num(r.get('confidence_pct'))),
    ('Needs review', lambda r: _yes_no(r.get('low_confidence'))),
    ('Reasoning', lambda r: r.get('rationale')),
    ('Coaching note', lambda r: r.get('lead_feedback')),

    ('Snoozed', lambda r: _yes_no(r.get('snoozed'))),
    ('Signals computed at', lambda r: r.get('computed_at')),
    ('Link', lambda r: r.get('url')),
]

# One column per risk flag, as asked. A single comma-joined cell cannot be
# filtered on in Excel without text matching, which is the thing people get
# wrong - "SLA breached" also matches "Vendor SLA breached".
#
# Prefixed, because several flags share a name with the signal column they
# are derived from. Two columns both headed "SLA breached" is not merely
# untidy: the signal is the computed boolean on ticket_signal and the flag
# is what the scorer raised from it, they are allowed to differ, and an
# unprefixed sheet gives the reader no way to tell which one they are
# filtering on. The prefix also groups them together in the filter dropdown.
FLAG_COLUMN_PREFIX = 'Flag: '

FLAG_COLUMNS = [(FLAG_COLUMN_PREFIX + label, key)
                for key, label in FLAG_LABELS.items()]

AGENT_COLUMNS = [
    ('Agent', lambda a: a.get('agent')),
    ('Aged tickets', lambda a: _num(a.get('ticket_count'))),
    ('Highest score', lambda a: _num(a.get('max_score'))),
    ('Average score', lambda a: _num(a.get('avg_score'))),
    ('Average idle (days)', lambda a: _num(a.get('avg_idle_days'))),
    ('SLA breached', lambda a: _num(a.get('sla_breached'))),
    ('Prolonged inactivity', lambda a: _num(a.get('prolonged_inactivity'))),
    ('Caller awaiting reply', lambda a: _num(a.get('caller_awaiting'))),
    ('Knowledge gaps', lambda a: _num(a.get('kb_gaps'))),
    ('Audit compliance (%)',
     lambda a: _num((a.get('compliance') or {}).get('avg_score'))),
    ('Incidents audited',
     lambda a: _num((a.get('compliance') or {}).get('audited'))),
]


def describe_filters(filters: Dict[str, Any], scope: Sequence[str]) -> List[str]:
    """What was applied, in words, for the header of each file.

    A spreadsheet outlives the screen it came from. Without this, a file
    showing nine tickets is indistinguishable from a desk with nine aged
    tickets and one with a filter left on.
    """
    parts = [f"Queues: {', '.join(scope) if scope else 'none in scope'}"]

    labels = {
        'group': 'Queue', 'agent': 'Agent', 'ball': 'Pending action owner',
        'flag': 'Risk flag', 'action': 'Suggested step',
    }
    for key, label in labels.items():
        value = filters.get(key)
        if value:
            parts.append(f'{label}: {value}')

    if filters.get('min_score'):
        parts.append(f"Minimum score: {filters['min_score']}")
    parts.append('Snoozed tickets included' if filters.get('show_snoozed')
                 else 'Snoozed tickets excluded')
    return parts


def ticket_row(row: Dict[str, Any]) -> List[Any]:
    values = [extract(row) for _heading, extract in TICKET_COLUMNS]
    flags = set(row.get('risk_flags') or [])
    values.extend(_yes_no(key in flags) for _label, key in FLAG_COLUMNS)
    return values


def ticket_headings() -> List[str]:
    return ([heading for heading, _ in TICKET_COLUMNS]
            + [label for label, _ in FLAG_COLUMNS])


def agent_row(entry: Dict[str, Any]) -> List[Any]:
    return [extract(entry) for _heading, extract in AGENT_COLUMNS]


def truncate(rows: Sequence[Dict[str, Any]]) -> tuple:
    """(rows to write, how many were dropped)."""
    if len(rows) <= MAX_ROWS:
        return list(rows), 0
    log.warning('Export capped at %d rows; %d omitted', MAX_ROWS, len(rows) - MAX_ROWS)
    return list(rows[:MAX_ROWS]), len(rows) - MAX_ROWS


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def build_workbook(rows: Sequence[Dict[str, Any]],
                   agents: Sequence[Dict[str, Any]],
                   context: Dict[str, Any]) -> bytes:
    """Three sheets: Tickets, By agent, About.

    About exists because the other two are numbers without provenance. Six
    months later, "where did this come from and what was filtered out" is
    the only question anybody asks of a spreadsheet, and it is the one thing
    a spreadsheet never records.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:                                  # pragma: no cover
        raise ExportUnavailable(
            'Excel export needs the openpyxl package, which is not installed '
            'on this server.') from exc

    kept, dropped = truncate(rows)

    book = Workbook()
    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill('solid', fgColor='2F4F6F')

    def write_sheet(sheet, headings, data_rows, widths=None):
        sheet.append(list(headings))
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical='center', wrap_text=True)
        for values in data_rows:
            sheet.append(values)

        sheet.freeze_panes = 'A2'
        # An auto-filter on the header row is the reason to export to Excel
        # rather than CSV, so it is on by default.
        sheet.auto_filter.ref = sheet.dimensions

        for index, heading in enumerate(headings, start=1):
            letter = get_column_letter(index)
            if widths and heading in widths:
                sheet.column_dimensions[letter].width = widths[heading]
            else:
                sheet.column_dimensions[letter].width = min(
                    max(12, len(str(heading)) + 3), 28)

    tickets = book.active
    tickets.title = 'Tickets'
    write_sheet(tickets, ticket_headings(), [ticket_row(r) for r in kept],
                widths={'Short description': 52, 'Reasoning': 60,
                        'Coaching note': 44, 'Link': 42, 'Caller': 22})

    by_agent = book.create_sheet('By agent')
    write_sheet(by_agent, [h for h, _ in AGENT_COLUMNS],
                [agent_row(a) for a in agents], widths={'Agent': 26})

    about = book.create_sheet('About')
    about.column_dimensions['A'].width = 30
    about.column_dimensions['B'].width = 90
    for label, value in _provenance(context, len(kept), len(agents), dropped):
        about.append([label, value])
    for row_cells in about.iter_rows(min_col=1, max_col=1):
        for cell in row_cells:
            cell.font = Font(bold=True)
    for row_cells in about.iter_rows(min_col=2, max_col=2):
        for cell in row_cells:
            cell.alignment = Alignment(wrap_text=True, vertical='top')

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _provenance(context: Dict[str, Any], ticket_count: int,
                agent_count: int, dropped: int) -> List[tuple]:
    entries = [
        ('Report', 'Aged pending ticket review - board export'),
        ('Generated at', context.get('generated_at')),
        ('Timezone', context.get('timezone')),
        ('Exported by', context.get('exported_by')),
        ('Tickets', ticket_count),
        ('Agents', agent_count),
    ]
    for line in context.get('filters_described') or []:
        entries.append(('Filter applied', line))

    if dropped:
        entries.append((
            'TRUNCATED',
            f'{dropped} further ticket(s) matched and are NOT in this file. '
            f'The export is capped at {MAX_ROWS} rows. Narrow the filters on '
            f'the board and export again.'))

    entries.append((
        'Note',
        'Suggested next steps are advisory and were generated from ticket '
        'history, SLA state, comparable resolved incidents and knowledge '
        'articles. Nothing in ServiceNow has been changed by this tool.'))
    entries.append((
        'Handling',
        'Contains incident descriptions and caller names. Share only with '
        'people entitled to the queues listed above.'))
    return entries


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

# What fits on landscape A4 at a size somebody can read. Deliberately not
# every column: see the module docstring.
PDF_COLUMNS = [
    ('Score', 42, lambda r: str(r.get('attention_score') or 0)),
    ('Incident', 74, lambda r: r.get('incident_number') or ''),
    ('Summary', 232, lambda r: (r.get('short_description') or '')[:150]),
    ('Assigned to', 92, lambda r: r.get('assigned_to') or 'Unassigned'),
    ('Age', 58, lambda r: r.get('age_days_display') or r.get('age_display') or ''),
    ('Idle', 58, lambda r: r.get('idle_days_display') or r.get('idle_display') or ''),
    ('Pending on', 96, lambda r: r.get('pending_action_owner_label') or ''),
    ('Suggested step', 118, lambda r: r.get('action_label') or ''),
    ('Flags', 150, lambda r: ', '.join(r.get('flag_labels') or []) or '-'),
]

PDF_AGENT_COLUMNS = [
    ('Agent', 150, lambda a: a.get('agent') or ''),
    ('Aged', 48, lambda a: str(a.get('ticket_count') or 0)),
    ('Highest', 56, lambda a: str(a.get('max_score') or 0)),
    ('Average', 56, lambda a: str(a.get('avg_score') or 0)),
    ('Avg idle', 70, lambda a: a.get('avg_idle_display') or '-'),
    ('SLA breached', 78, lambda a: str(a.get('sla_breached') or 0)),
    ('Stale 4d+', 64, lambda a: str(a.get('prolonged_inactivity') or 0)),
    ('Caller waiting', 82, lambda a: str(a.get('caller_awaiting') or 0)),
    ('KB gaps', 58, lambda a: str(a.get('kb_gaps') or 0)),
    ('Audit avg', 64,
     lambda a: (f"{(a.get('compliance') or {}).get('avg_score')}%"
                if (a.get('compliance') or {}).get('avg_score') is not None else '-')),
]


def build_pdf(rows: Sequence[Dict[str, Any]],
              agents: Sequence[Dict[str, Any]],
              context: Dict[str, Any]) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as exc:
        raise ExportUnavailable(
            'PDF export needs the reportlab package, which is not installed '
            'on this server. Excel export is unaffected.') from exc

    kept, dropped = truncate(rows)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=14 * mm,
        title='Aged pending ticket review',
        author='Aged Ticket Advisor')

    styles = getSampleStyleSheet()
    cell = ParagraphStyle('cell', parent=styles['BodyText'], fontSize=7.2,
                          leading=9, alignment=TA_LEFT, spaceAfter=0)
    head = ParagraphStyle('head', parent=styles['BodyText'], fontSize=7.2,
                          leading=9, textColor=colors.white,
                          fontName='Helvetica-Bold', spaceAfter=0)
    title = ParagraphStyle('title', parent=styles['Title'], fontSize=15,
                           leading=18, alignment=TA_LEFT, spaceAfter=2)
    sub = ParagraphStyle('sub', parent=styles['BodyText'], fontSize=8,
                         leading=11, textColor=colors.HexColor('#56554f'))
    note = ParagraphStyle('note', parent=sub, fontSize=7.5,
                          textColor=colors.HexColor('#8a8880'))

    def escape(value: str) -> str:
        return (str(value).replace('&', '&amp;')
                          .replace('<', '&lt;').replace('>', '&gt;'))

    def grid(columns, data_rows, zebra_on=None):
        header = [Paragraph(escape(label), head) for label, _w, _f in columns]
        body = [header]
        for item in data_rows:
            body.append([Paragraph(escape(extract(item)), cell)
                         for _label, _w, extract in columns])

        table = Table(body, colWidths=[w for _l, w, _f in columns],
                      repeatRows=1, hAlign='LEFT')
        style = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2f4f6f')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#d8d7cf')),
        ]
        # Banded rows rather than a full grid: vertical rules on a nine
        # column table are more ink than the data.
        for index in range(1, len(body)):
            if index % 2 == 0:
                style.append(('BACKGROUND', (0, index), (-1, index),
                              colors.HexColor('#f6f6f3')))
            if zebra_on and zebra_on(data_rows[index - 1]):
                style.append(('LINEBEFORE', (0, index), (0, index), 2.2,
                              colors.HexColor('#c62f2f')))
        table.setStyle(TableStyle(style))
        return table

    story = [Paragraph('Aged pending ticket review', title)]
    for line in context.get('filters_described') or []:
        story.append(Paragraph(escape(line), sub))
    story.append(Paragraph(
        escape(f"Generated {context.get('generated_at')} "
               f"{context.get('timezone') or ''} by {context.get('exported_by')} "
               f"- {len(kept)} ticket(s), ranked by attention score"), sub))
    story.append(Paragraph(
        'This is the working list. Every signal, flag and recommendation '
        'field in its own column is in the Excel export - a page cannot hold '
        'thirty columns legibly.', note))

    if dropped:
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            escape(f'TRUNCATED: {dropped} further ticket(s) matched and are not '
                   f'shown. The export is capped at {MAX_ROWS} rows.'),
            ParagraphStyle('warn', parent=sub,
                           textColor=colors.HexColor('#c62f2f'))))

    story.append(Spacer(1, 7))
    if kept:
        story.append(grid(PDF_COLUMNS, kept,
                          zebra_on=lambda r: bool(r.get('sla_breached'))))
    else:
        story.append(Paragraph('Nothing aged in this view.', sub))

    story.append(PageBreak())
    story.append(Paragraph('By agent', title))
    story.append(Paragraph(
        'Worst first. The audit column is the 30-day average from the ticket '
        'audit tool, where that link is configured.', sub))
    story.append(Spacer(1, 7))
    if agents:
        story.append(grid(PDF_AGENT_COLUMNS, agents))
    else:
        story.append(Paragraph('No agents to report.', sub))

    def furniture(canvas, document):
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(colors.HexColor('#8a8880'))
        width, _height = landscape(A4)
        canvas.drawString(
            12 * mm, 8 * mm,
            'Contains incident descriptions and caller names - share only '
            'with people entitled to these queues.')
        canvas.drawRightString(width - 12 * mm, 8 * mm,
                               f'Page {canvas.getPageNumber()}')
        canvas.restoreState()

    doc.build(story, onFirstPage=furniture, onLaterPages=furniture)
    return buffer.getvalue()


# ---------------------------------------------------------------------------

def filename(prefix: str, extension: str,
             group: Optional[str] = None,
             now: Optional[datetime.datetime] = None) -> str:
    """A name that sorts chronologically and says what it holds.

    Dated, because these end up in a folder together and "board (3).xlsx"
    tells nobody which morning it was.
    """
    stamp = (now or utc_now()).strftime('%Y-%m-%d')
    parts = [prefix, stamp]
    if group:
        safe = ''.join(c if c.isalnum() else '-' for c in group).strip('-')
        if safe:
            parts.insert(1, safe.lower())
    return '-'.join(parts) + '.' + extension
