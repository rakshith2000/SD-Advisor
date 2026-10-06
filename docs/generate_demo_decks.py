"""Builds the two demo decks for the Service Desk introduction.

    python docs/generate_demo_decks.py

Produces, next to this file:

    Aged_Ticket_Advisor_Overview.pptx   ~15 slides, for the call itself
    Aged_Ticket_Advisor_Detailed.pptx   ~32 slides, for the follow-up and
                                        for anyone who asks how it works

Both are generated rather than hand-built so the numbers in them stay tied to
the code. Every figure quoted - the seven score weights, the thresholds, the
cadences, the confidence floor - is imported or copied from the module that
owns it, and the self-check at the bottom fails the build if a weight drifts.
A deck that quietly disagrees with the running service is worse than no deck:
it gets presented once, believed, and quoted back months later.

Deliberately absent: any claim about hours saved or tickets avoided. Nobody
has measured those yet, and inventing them for a first demo is the fastest way
to lose a room that works these queues every day.
"""

import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.scoring import DEFAULT_WEIGHTS
from pipeline.signals import PENDING_ACTION_OWNERS
from delivery.digest import ACTION_LABELS, FLAG_LABELS

OUT = Path(__file__).resolve().parent

# Product palette, lifted from web/static/app.css so the deck and the screen
# people are about to be shown are recognisably the same thing.
INK = RGBColor(0x0B, 0x0B, 0x0B)
INK2 = RGBColor(0x52, 0x51, 0x4E)
MUTED = RGBColor(0x89, 0x87, 0x81)
SURFACE = RGBColor(0xFC, 0xFC, 0xFB)
PLANE = RGBColor(0xF9, 0xF9, 0xF7)
GRID = RGBColor(0xE1, 0xE0, 0xD9)
ACCENT = RGBColor(0x2A, 0x78, 0xD6)
ACCENT_SOFT = RGBColor(0xCD, 0xE2, 0xFB)
GOOD = RGBColor(0x0C, 0xA3, 0x0C)
WARN = RGBColor(0xFA, 0xB2, 0x19)
SERIOUS = RGBColor(0xEC, 0x83, 0x5A)
CRIT = RGBColor(0xD0, 0x3B, 0x3B)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

FONT = 'Segoe UI'
W, H = Inches(13.333), Inches(7.5)
MARGIN = Inches(0.78)
BODY_W = W - MARGIN * 2


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def _text(frame, blocks):
    """blocks: list of (text, size, bold, colour, space_before, bullet_level)."""
    frame.word_wrap = True
    first = True
    for text, size, bold, colour, space, level in blocks:
        p = frame.paragraphs[0] if first else frame.add_paragraph()
        first = False
        p.level = level
        p.space_before = Pt(space)
        p.space_after = Pt(0)
        p.line_spacing = 1.18
        run = p.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = colour
        run.font.name = FONT
    return frame


def _box(slide, left, top, width, height, fill=None, line=None,
         shape=MSO_SHAPE.ROUNDED_RECTANGLE, line_w=1.0):
    sh = slide.shapes.add_shape(shape, left, top, width, height)
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid()
        sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
        sh.line.width = Pt(line_w)
    sh.shadow.inherit = False
    sh.text_frame.word_wrap = True
    return sh


def _tb(slide, left, top, width, height):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tb.text_frame.word_wrap = True
    return tb


def _lines(text, size, width_emu):
    """How many lines a run of prose will wrap to, give or take.

    PowerPoint textboxes grow downwards rather than clipping, so a paragraph
    that needs two lines in a box sized for one silently overlaps whatever was
    placed beneath it. Laying out against an estimate is ugly but it is the
    only way to put a table directly under a sentence of unknown length.
    Segoe UI averages close to half an em per character for mixed-case prose.
    """
    usable = (width_emu - Inches(0.2)) / Emu(12700)       # points
    per_line = max(8, int(usable / (size * 0.5)))
    return max(1, -(-len(text) // per_line))


def _lead_height(text, size, width_emu, pad=Inches(0.16)):
    return Emu(int(_lines(text, size, width_emu) * size * 1.2 * 12700)) + pad


class Deck:
    def __init__(self, subtitle):
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = W, H
        self.subtitle = subtitle
        self.n = 0

    # -- chrome ----------------------------------------------------------

    def _blank(self):
        return self.prs.slides.add_slide(self.prs.slide_layouts[6])

    def _bg(self, slide, colour=SURFACE):
        _box(slide, 0, 0, W, H, fill=colour, shape=MSO_SHAPE.RECTANGLE)

    def _footer(self, slide):
        self.n += 1
        tb = _tb(slide, MARGIN, H - Inches(0.46), BODY_W, Inches(0.3))
        p = tb.text_frame.paragraphs[0]
        r = p.add_run()
        r.text = f'Aged Ticket Advisor  ·  {self.subtitle}'
        r.font.size = Pt(9)
        r.font.color.rgb = MUTED
        r.font.name = FONT
        num = _tb(slide, W - MARGIN - Inches(0.6), H - Inches(0.46),
                  Inches(0.6), Inches(0.3))
        p = num.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.RIGHT
        r = p.add_run()
        r.text = str(self.n)
        r.font.size = Pt(9)
        r.font.color.rgb = MUTED
        r.font.name = FONT

    def _head(self, slide, title, kicker=None):
        top = Inches(0.52)
        if kicker:
            tb = _tb(slide, MARGIN, top, BODY_W, Inches(0.3))
            _text(tb.text_frame, [(kicker.upper(), 10.5, True, ACCENT, 0, 0)])
            top = top + Inches(0.34)
        tb = _tb(slide, MARGIN, top, BODY_W, Inches(0.62))
        _text(tb.text_frame, [(title, 27, True, INK, 0, 0)])
        rule = _box(slide, MARGIN, top + Inches(0.72), Inches(1.0), Pt(3),
                    fill=ACCENT, shape=MSO_SHAPE.RECTANGLE)
        return top + Inches(1.06)

    def notes(self, slide, text):
        slide.notes_slide.notes_text_frame.text = text

    # -- slide kinds -----------------------------------------------------

    def title(self, title, sub, tagline, notes=''):
        s = self._blank()
        self._bg(s, INK)
        _box(s, 0, H - Inches(2.3), W, Inches(2.3), fill=RGBColor(0x14, 0x14, 0x14),
             shape=MSO_SHAPE.RECTANGLE)
        _box(s, MARGIN, Inches(1.5), Inches(1.4), Pt(4), fill=ACCENT,
             shape=MSO_SHAPE.RECTANGLE)
        tb = _tb(s, MARGIN, Inches(1.9), BODY_W, Inches(2.4))
        _text(tb.text_frame, [
            (title, 46, True, WHITE, 0, 0),
            (sub, 19, False, RGBColor(0xC3, 0xC2, 0xB7), 12, 0),
        ])
        tb = _tb(s, MARGIN, H - Inches(1.95), BODY_W - Inches(1), Inches(1.5))
        _text(tb.text_frame, [(tagline, 14, False, RGBColor(0x89, 0x87, 0x81), 0, 0)])
        if notes:
            self.notes(s, notes)
        return s

    def section(self, number, title, blurb, notes=''):
        s = self._blank()
        self._bg(s, PLANE)
        _box(s, 0, 0, Inches(0.22), H, fill=ACCENT, shape=MSO_SHAPE.RECTANGLE)
        tb = _tb(s, MARGIN, Inches(2.5), BODY_W, Inches(2.2))
        _text(tb.text_frame, [
            (number, 13, True, ACCENT, 0, 0),
            (title, 36, True, INK, 10, 0),
            (blurb, 15, False, INK2, 14, 0),
        ])
        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def bullets(self, title, items, kicker=None, lead=None, notes=''):
        """items: str, or (text, sub) for a two-line bullet."""
        s = self._blank()
        self._bg(s)
        top = self._head(s, title, kicker)
        if lead:
            h = _lead_height(lead, 14.5, BODY_W)
            tb = _tb(s, MARGIN, top, BODY_W, h)
            _text(tb.text_frame, [(lead, 14.5, False, INK2, 0, 0)])
            top = top + h

        avail = H - top - Inches(0.75)
        tb = _tb(s, MARGIN, top, BODY_W, avail)
        blocks = []
        size = 16 if len(items) <= 6 else 14
        for i, item in enumerate(items):
            if isinstance(item, tuple):
                head, sub = item
                blocks.append((f'·  {head}', size, True, INK, 0 if not i else 13, 0))
                blocks.append((sub, size - 2.5, False, INK2, 2, 1))
            else:
                blocks.append((f'·  {item}', size, False, INK, 0 if not i else 10, 0))
        _text(tb.text_frame, blocks)
        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def split(self, title, left_title, left_items, right_title, right_items,
              kicker=None, left_colour=CRIT, right_colour=GOOD, notes=''):
        s = self._blank()
        self._bg(s)
        top = self._head(s, title, kicker)
        colw = (BODY_W - Inches(0.4)) / 2
        height = H - top - Inches(0.8)

        for idx, (head, items, colour) in enumerate(
                ((left_title, left_items, left_colour),
                 (right_title, right_items, right_colour))):
            left = MARGIN + idx * (colw + Inches(0.4))
            _box(s, left, top, colw, height, fill=PLANE, line=GRID)
            _box(s, left, top, Pt(4), height, fill=colour, shape=MSO_SHAPE.RECTANGLE)
            tb = _tb(s, left + Inches(0.3), top + Inches(0.22),
                     colw - Inches(0.55), height - Inches(0.4))
            blocks = [(head, 15, True, colour, 0, 0)]
            for i, item in enumerate(items):
                blocks.append((f'·  {item}', 13, False, INK2, 11 if i else 14, 0))
            _text(tb.text_frame, blocks)

        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def cards(self, title, items, kicker=None, lead=None, per_row=3, notes=''):
        """items: (heading, body) tiles."""
        s = self._blank()
        self._bg(s)
        top = self._head(s, title, kicker)
        if lead:
            h = _lead_height(lead, 14, BODY_W)
            tb = _tb(s, MARGIN, top, BODY_W, h)
            _text(tb.text_frame, [(lead, 14, False, INK2, 0, 0)])
            top = top + h

        gap = Inches(0.26)
        colw = (BODY_W - gap * (per_row - 1)) / per_row
        rows = (len(items) + per_row - 1) // per_row
        rowh = min(Inches(1.72), (H - top - Inches(0.8) - gap * (rows - 1)) / rows)

        for i, (head, body) in enumerate(items):
            r, c = divmod(i, per_row)
            left = MARGIN + c * (colw + gap)
            tp = top + r * (rowh + gap)
            _box(s, left, tp, colw, rowh, fill=PLANE, line=GRID)
            _box(s, left, tp, colw, Pt(3), fill=ACCENT, shape=MSO_SHAPE.RECTANGLE)
            tb = _tb(s, left + Inches(0.22), tp + Inches(0.18),
                     colw - Inches(0.44), rowh - Inches(0.3))
            _text(tb.text_frame, [
                (head, 13.5, True, INK, 0, 0),
                (body, 11.5, False, INK2, 6, 0),
            ])
        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def table(self, title, headers, rows, kicker=None, lead=None,
              widths=None, notes='', emphasise_last=False):
        s = self._blank()
        self._bg(s)
        top = self._head(s, title, kicker)
        if lead:
            h = _lead_height(lead, 13.5, BODY_W)
            tb = _tb(s, MARGIN, top, BODY_W, h)
            _text(tb.text_frame, [(lead, 13.5, False, INK2, 0, 0)])
            top = top + h

        n = len(rows) + 1
        rowh = min(Inches(0.44), (H - top - Inches(0.8)) / n)
        tbl = s.shapes.add_table(n, len(headers), MARGIN, top,
                                 BODY_W, rowh * n).table
        if widths:
            total = sum(widths)
            for i, wgt in enumerate(widths):
                tbl.columns[i].width = Emu(int(BODY_W * wgt / total))

        size = 12 if len(rows) <= 9 else 10.5

        for c, head in enumerate(headers):
            cell = tbl.cell(0, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = INK
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.12)
            _text(cell.text_frame, [(head, size - 0.5, True, WHITE, 0, 0)])

        for r, row in enumerate(rows, start=1):
            for c, val in enumerate(row):
                cell = tbl.cell(r, c)
                cell.fill.solid()
                cell.fill.fore_color.rgb = SURFACE if r % 2 else PLANE
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                cell.margin_left = Inches(0.12)
                bold = emphasise_last and r == len(rows)
                colour = INK if (c == 0 or bold) else INK2
                _text(cell.text_frame, [(str(val), size, bold, colour, 0, 0)])

        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def pipeline(self, title, stages, kicker=None, lead=None, footnote=None,
                 notes=''):
        """stages: (label, detail, cadence)."""
        s = self._blank()
        self._bg(s)
        top = self._head(s, title, kicker)
        if lead:
            h = _lead_height(lead, 14, BODY_W)
            tb = _tb(s, MARGIN, top, BODY_W, h)
            _text(tb.text_frame, [(lead, 14, False, INK2, 0, 0)])
            top = top + h

        n = len(stages)
        arrow = Inches(0.34)
        colw = (BODY_W - arrow * (n - 1)) / n
        boxh = Inches(2.5)

        for i, (label, detail, cadence) in enumerate(stages):
            left = MARGIN + i * (colw + arrow)
            _box(s, left, top, colw, boxh, fill=PLANE, line=GRID)
            _box(s, left, top, colw, Pt(4), fill=ACCENT, shape=MSO_SHAPE.RECTANGLE)
            tb = _tb(s, left + Inches(0.18), top + Inches(0.2),
                     colw - Inches(0.36), boxh - Inches(0.35))
            _text(tb.text_frame, [
                (cadence.upper(), 9, True, ACCENT, 0, 0),
                (label, 14, True, INK, 7, 0),
                (detail, 11, False, INK2, 7, 0),
            ])
            if i < n - 1:
                a = _box(s, left + colw + Inches(0.06), top + boxh / 2 - Inches(0.11),
                         Inches(0.22), Inches(0.22), fill=MUTED,
                         shape=MSO_SHAPE.ISOSCELES_TRIANGLE)
                a.rotation = 90

        if footnote:
            tb = _tb(s, MARGIN, top + boxh + Inches(0.3), BODY_W, Inches(0.8))
            _text(tb.text_frame, [(footnote, 13, False, INK2, 0, 0)])

        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def weights(self, title, pairs, kicker=None, lead=None, footnote=None, notes=''):
        """pairs: (component, weight, description) with weights summing to 100."""
        s = self._blank()
        self._bg(s)
        top = self._head(s, title, kicker)
        if lead:
            h = _lead_height(lead, 14, BODY_W)
            tb = _tb(s, MARGIN, top, BODY_W, h)
            _text(tb.text_frame, [(lead, 14, False, INK2, 0, 0)])
            top = top + h

        rowh = Inches(0.52)
        bar_left = MARGIN + Inches(2.55)
        bar_max = Inches(3.1)

        for i, (name, weight, desc) in enumerate(pairs):
            y = top + i * rowh
            tb = _tb(s, MARGIN, y, Inches(2.5), rowh)
            _text(tb.text_frame, [(name, 13, True, INK, 0, 0)])

            _box(s, bar_left, y + Inches(0.1), bar_max, Inches(0.17),
                 fill=ACCENT_SOFT, shape=MSO_SHAPE.RECTANGLE)
            _box(s, bar_left, y + Inches(0.1), Emu(int(bar_max * weight / 25)),
                 Inches(0.17), fill=ACCENT, shape=MSO_SHAPE.RECTANGLE)

            tb = _tb(s, bar_left + bar_max + Inches(0.14), y, Inches(0.6), rowh)
            _text(tb.text_frame, [(str(weight), 13, True, ACCENT, 0, 0)])

            tb = _tb(s, bar_left + bar_max + Inches(0.78), y,
                     W - MARGIN - bar_left - bar_max - Inches(0.78), rowh)
            _text(tb.text_frame, [(desc, 11.5, False, INK2, 0, 0)])

        y = top + len(pairs) * rowh + Inches(0.1)
        _box(s, MARGIN, y, BODY_W, Pt(1), fill=GRID, shape=MSO_SHAPE.RECTANGLE)
        tb = _tb(s, MARGIN, y + Inches(0.08), BODY_W, Inches(0.4))
        _text(tb.text_frame, [
            (f'Total  {sum(w for _, w, _ in pairs)}   ·   must sum to 100, or the '
             f'service reverts to these defaults and logs it', 12, True, INK, 0, 0)])

        if footnote:
            tb = _tb(s, MARGIN, y + Inches(0.52), BODY_W, Inches(0.7))
            _text(tb.text_frame, [(footnote, 12.5, False, INK2, 0, 0)])

        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def statement(self, kicker, big, sub, notes=''):
        s = self._blank()
        self._bg(s, PLANE)
        _box(s, MARGIN, Inches(1.9), Pt(4), Inches(3.3), fill=ACCENT,
             shape=MSO_SHAPE.RECTANGLE)
        tb = _tb(s, MARGIN + Inches(0.4), Inches(1.9), BODY_W - Inches(0.8), Inches(3.4))
        _text(tb.text_frame, [
            (kicker.upper(), 11, True, ACCENT, 0, 0),
            (big, 30, True, INK, 14, 0),
            (sub, 15, False, INK2, 16, 0),
        ])
        self._footer(s)
        if notes:
            self.notes(s, notes)
        return s

    def save(self, name):
        path = OUT / name
        self.prs.save(str(path))
        return path


# ---------------------------------------------------------------------------
# shared content, derived from the code so it cannot drift
# ---------------------------------------------------------------------------

WEIGHT_DESC = {
    'sla_risk': 'How close the resolution SLA is to breaching',
    'inactivity_duration': 'Time since the last real Service Desk action',
    'unactioned_delay': 'Caller replied and nobody answered; dependency already closed',
    'ticket_age': 'How far past the aged threshold the incident is',
    'business_priority': 'Priority and impact of the incident',
    'duration_overrun': 'Past the 90th percentile for comparable incidents',
    'reassignment_activity': 'How often it has been passed around or reopened',
}
WEIGHT_NAMES = {
    'sla_risk': 'SLA risk',
    'inactivity_duration': 'Inactivity duration',
    'unactioned_delay': 'Unactioned delay',
    'ticket_age': 'Ticket age',
    'business_priority': 'Business priority',
    'duration_overrun': 'Duration overrun',
    'reassignment_activity': 'Reassignment activity',
}
WEIGHT_ROWS = [(WEIGHT_NAMES[k], v, WEIGHT_DESC[k])
               for k, v in sorted(DEFAULT_WEIGHTS.items(), key=lambda kv: -kv[1])]

CADENCE = [
    ('Sync', 'Delta read of changed incidents, recompute every signal, '
             'fire risk alerts. No AI in this loop.', 'every 10 min'),
    ('Advise', 'Language model pass over tickets whose content actually '
               'changed. Everything else is served from cache.', 'hourly'),
    ('Digest', 'Ranked email to leads, then a short list to each agent.',
     'daily, per queue'),
    ('Maintain', 'Full sync, retire departed tickets, rebuild the evidence '
                 'index and resolution baselines.', 'nightly'),
]


def overview():
    d = Deck('Service Desk introduction')

    d.title(
        'Aged Ticket Advisor',
        'Finding the tickets that are genuinely stuck - and saying what to do next',
        'A read-only companion to ServiceNow for the Service Desk.  '
        'It never edits an incident.',
        notes='Frame the session: 20 minutes, then the live board. Two things '
              'to land - it is read-only, and it ranks rather than lists. '
              'Invite interruptions; this audience knows the queue better '
              'than the tool does.')

    d.statement(
        'The problem',
        'A long pending queue hides the handful of tickets that are actually stuck.',
        'Everything in an aged list looks equally old. Nothing in it tells you '
        'which ones are legitimately waiting on somebody else, and which ones '
        'have been waiting on us the whole time.',
        notes='Pause here. Ask the room how they pick what to review on a '
              'Monday. The honest answer is usually "oldest first, or whatever '
              'someone chased" - which is the gap this fills.')

    d.bullets(
        'What a manual review cannot see',
        [
            ('The caller already replied',
             'The incident still reads "Awaiting Caller", so it looks like '
             'their move. It has been ours since they answered.'),
            ('The dependency closed weeks ago',
             'A change or problem the ticket was held for is complete, and '
             'nothing told anybody.'),
            ('The idle clock was reset by a robot',
             'An SLA recalculation updates the record, so a ticket nobody has '
             'touched in nine days looks freshly worked.'),
            ('The breach is noticed afterwards',
             'A ticket crosses its SLA quietly and is found at the next review.'),
        ],
        kicker='Why this is hard',
        notes='These four are the whole pitch. Each is invisible in a list '
              'view and each is routine. If one of them lands with the room, '
              'the rest of the deck is easy.')

    d.pipeline(
        'What it actually does', CADENCE,
        lead='Four loops. Only one of them involves a language model.',
        footnote='Every read from ServiceNow is a read. There is no write '
                 'client in the codebase - it cannot resolve, reassign or '
                 'comment on an incident even if someone asked it to.',
        notes='Spell out the read-only point here rather than at the end. It '
              'is the first question a Service Desk team asks, and answering '
              'it early buys attention for everything after.')

    d.cards(
        'What it measures', [
            ('Age and idle, separately',
             'Age is how old. Idle is how long since a person last did '
             'something real. The second is the one that matters.'),
            ('Who owes the next action',
             'Service Desk, Caller, Vendor, Change, Problem or Approval - '
             'derived, not taken from the hold reason at face value.'),
            ('Caller replied, unanswered',
             'The caller spoke last and nobody has responded since.'),
            ('Dependency already cleared',
             'Held for a change or problem that has since closed.'),
            ('SLA position',
             'Breached, percentage consumed, and when it is projected to '
             'breach if nothing changes.'),
            ('Knowledge gap',
             'A matching article exists and is not attached to the ticket.'),
        ],
        kicker='Deterministic signals', per_row=3,
        lead='All of this is arithmetic over the ticket history. No model '
             'is involved, and every number can be explained.',
        notes='Stress "explained". A lead who asks why a ticket is top of the '
              'list gets an arithmetic answer, not "the AI decided".')

    d.weights(
        'How the ranking works', WEIGHT_ROWS,
        kicker='Attention score',
        lead='Seven components, each scored 0-100, combined by weights that '
             'sum to 100.',
        footnote='Leads can retune these in the web UI. The ticket page shows '
                 'the breakdown for every incident - "Why it ranks here".',
        notes='Open the ticket page later and show the breakdown bars. The '
              'point is not the numbers, it is that they are visible and '
              'editable by the team rather than fixed by us.')

    d.split(
        'What the AI does, and does not', 'It does',
        ['Suggest the next step, from a fixed list of actions',
         'Explain why, in one or two sentences',
         'Draft a work note and a message to the caller',
         'Cite evidence - similar resolved incidents, relevant articles'],
        'It does not',
        ['Decide the ranking - that is the arithmetic above',
         'Write anything to ServiceNow, ever',
         'Resolve, reassign, close or comment automatically',
         'See personal data - it is removed before the request is sent'],
        kicker='Scope of the model', left_colour=ACCENT, right_colour=CRIT,
        notes='The right-hand column is the one to read aloud. Low-confidence '
              'suggestions are held back entirely rather than shown with a '
              'caveat nobody reads.')

    d.cards(
        'How it reaches you', [
            ('The board',
             'Ranked worst-first, filtered to your queues. The top ten is '
             'normally the whole job.'),
            ('Daily lead digest',
             'What is new, what got worse, and what is waiting on us - in '
             'one email, before the shift.'),
            ('Per-agent digest',
             'A short list for each agent covering only their own tickets.'),
            ('Risk alerts',
             'Breach, inactivity and unanswered-caller events, once per '
             'ticket per type per day.'),
            ('Ticket page',
             'Signals, score breakdown, full activity, and drafts you can '
             'copy straight out.'),
            ('By agent view',
             'Per-person backlog, alongside the compliance score from the '
             'existing audit tool.'),
        ],
        kicker='Delivery', per_row=3,
        notes='Mention that the digest is the main surface for most people. '
              'The board is for leads working a queue down.')

    d.statement(
        'How we will know it is working',
        'Every suggestion gets a verdict from a lead, and the agreement rate '
        'is published.',
        'Agree, mostly right, wrong, not applicable, already handled. The '
        'Accuracy page tracks agreement over time, which action types are '
        'reliable, and - the uncomfortable one - breaches that happened with '
        'no warning.',
        notes='This is the slide that earns trust. We are measuring our own '
              'false quiet and showing it to the people being asked to use '
              'the tool. Ask them to use the verdict buttons in the pilot; '
              'without them there is no accuracy number.')

    d.cards(
        'Where it helps', [
            ('A shorter review',
             'Ranked, so the decision is where to stop reading rather than '
             'what to read first.'),
            ('Fewer silent stalls',
             'Cleared dependencies and unanswered callers surface the day '
             'they happen.'),
            ('Warning before the breach',
             'Projected breach time, not a post-mortem.'),
            ('The same standard daily',
             'Every queue reviewed by the same rules, including the days '
             'nobody has time.'),
            ('Coaching with evidence',
             'Per-agent backlog and ageing, next to audit compliance.'),
            ('A number to argue with',
             'Agreement rate makes the tool accountable to the team, not '
             'the other way round.'),
        ],
        kicker='Benefits', per_row=3,
        lead='Deliberately no percentages on this slide - nothing has been '
             'measured yet. The pilot is what produces those.',
        notes='If asked for a number, say so plainly: we will have an '
              'agreement rate and a review-time figure after the pilot, and '
              'we would rather bring real ones.')

    d.bullets(
        'Access and safety',
        [
            'Read-only ServiceNow integration. No write client exists.',
            'Single sign-on through Keycloak, with your existing account.',
            'Three roles: Admin, Lead, Viewer. New users start as Viewer.',
            'You see only the assignment groups you belong to - read from '
            'ServiceNow on first sign-in.',
            'Separate database and service from the existing audit tool.',
            'Credentials held in Vault; nothing in a config file.',
            'All times shown in US Central, switchable per person.',
        ],
        kicker='Governance',
        notes='Short slide, read it quickly unless somebody from security is '
              'in the room. The scope point matters to agents - they will not '
              'see other queues.')

    d.bullets(
        'What we are asking for',
        [
            ('Pilot on one queue, in shadow mode',
             'Everything computes and nothing is emailed, so we can compare '
             'against your own judgement first.'),
            ('Two or three leads using the verdict buttons',
             'Fifteen tickets a day is enough to produce a real agreement '
             'rate within a fortnight.'),
            ('Tell us where the ranking is wrong',
             'The weights are configurable. If SLA risk matters more here '
             'than age, we change the numbers, not the argument.'),
            ('Then turn the digest on',
             'Once the ranking looks right to the people who know the queue.'),
        ],
        kicker='Next steps',
        notes='Close by asking for names for the pilot and a date to turn '
              'shadow mode off. Leave the board open on screen for questions.')

    d.statement(
        'In one sentence',
        'It reads the aged queue every ten minutes, works out which tickets '
        'are genuinely stuck and who owes the next action, ranks them, '
        'explains the ranking, and drafts what to do.',
        'It never touches ServiceNow. Demo next.',
        notes='Hold here and switch to the live board.')

    return d.save('Aged_Ticket_Advisor_Overview.pptx')


def detailed():
    d = Deck('Detailed walkthrough')

    d.title(
        'Aged Ticket Advisor',
        'Detailed walkthrough - signals, scoring, delivery and measurement',
        'Companion to the overview deck. Written for anyone who has to '
        'operate, audit or argue with this service.',
        notes='Not a deck to present end to end. Use the sections that match '
              'the question being asked.')

    # -- 1. problem ------------------------------------------------------
    d.section('01', 'The problem, precisely',
              'Why an aged list is not a work list',
              notes='')

    d.table(
        'Four failure modes a list view cannot show',
        ['Situation', 'How it looks in the queue', 'What is actually true'],
        [['Caller answered a question',
          'Still "On Hold - Awaiting Caller"',
          'Ours, since the moment they replied'],
         ['Change or problem closed',
          'Still on hold for a dependency',
          'Nothing is blocking it'],
         ['SLA recalculation ran',
          'Updated minutes ago, looks active',
          'No person has touched it in nine days'],
         ['Three follow-ups, no reply',
          'Open and ageing',
          'A documented closure candidate'],
         ['Reassigned out of the Service Desk',
          'Still on our board',
          'Another team owns it now']],
        widths=[26, 36, 38],
        lead='Each of these is routine, and each needs the ticket history '
             'read to spot.',
        notes='The fifth row was a real defect found during build - the '
              'board kept showing tickets after they left our queues. Worth '
              'mentioning as evidence we test against reality.')

    d.statement(
        'The design decision underneath everything',
        'Rank, do not list.',
        'A list of 200 aged tickets is the problem restated. A ranked list '
        'where the top ten is the day\'s work is a different object - and it '
        'is only trustworthy if the ranking can be explained line by line.',
        notes='')

    # -- 2. signals ------------------------------------------------------
    d.section('02', 'What it measures',
              'Deterministic signals, computed from ticket history',
              notes='')

    d.bullets(
        'The idle clock, and why it is the hardest number',
        [
            ('Idle is not "time since last update"',
             'ServiceNow updates a record for all sorts of reasons. Taking '
             'sys_updated_on at face value makes every stale ticket look '
             'worked.'),
            ('System accounts are excluded',
             'Integration users, SLA engines and scheduled jobs do not reset '
             'the clock. Kohler\'s integration accounts end .rest, and that '
             'pattern is caught even when a new one is not yet configured.'),
            ('The caller\'s own updates are excluded',
             'A caller replying does not mean the Service Desk acted. It '
             'usually means the opposite.'),
            ('Field churn is excluded',
             'Business duration, SLA due, mod count - all ignored.'),
            ('Never touched? The clock runs from creation',
             'An untouched ticket is the worst case, not a missing value.'),
        ],
        kicker='Signal 1',
        notes='If one slide in this deck is worth reading, it is this one. '
              'Most tools of this kind get the idle clock wrong and report '
              'tickets that are in fact being progressed.')

    d.table(
        'Who owes the next action',
        ['Value', 'Means', 'Set when'],
        [['Service Desk', 'Ours to move', 'Default, and any time a blocker clears'],
         ['Caller', 'Waiting on the user', 'Hold reason says so AND they have not replied since'],
         ['Vendor', 'Waiting on a third party', 'Hold reason names a vendor or third party'],
         ['Change', 'Waiting on an RFC', 'Hold reason names a change, and it is still open'],
         ['Problem', 'Waiting on a problem record', 'Hold reason names a problem, still open'],
         ['Approval', 'Waiting on an approver', 'Hold reason names approval']],
        kicker='Signal 2', widths=[18, 30, 52],
        lead='Derived, not copied from the hold reason. Two rules override '
             'the stated reason, and both move the ticket back to us.',
        notes='The two overrides: a caller who has already replied, and a '
              'dependency that has already closed. Both are cases where the '
              'hold reason is stale and the ticket is quietly ours.')

    d.cards(
        'The rest of the signal set', [
            ('Caller replied, unanswered',
             'The last substantive entry came from the caller. Overrides '
             '"Awaiting Caller" outright.'),
            ('Dependency resolved',
             'The linked change or problem has reached a closed state while '
             'the incident is still held for it.'),
            ('Follow-ups and silence',
             'Counts documented, customer-visible chase attempts and the gap '
             'since the last one.'),
            ('Closure candidate',
             f'Three documented follow-ups and five days of silence, both '
             f'configurable.'),
            ('SLA position',
             'Breached, percent consumed, minutes left, projected breach '
             'time.'),
            ('Duration overrun',
             'Past the 90th percentile for comparable incidents, from 180 '
             'days of resolved history.'),
            ('Knowledge gap',
             'A relevant article exists and is not attached.'),
            ('Churn',
             'Reassignment and reopen counts.'),
            ('Activity stream',
             'Grouped the way ServiceNow groups it - one card per save, not '
             'one line per field.'),
        ],
        kicker='Signals 3-10', per_row=3,
        notes='')

    d.table(
        'Thresholds, and what each one changes',
        ['Setting', 'Default', 'What it controls'],
        [['aged_after_days', '5', 'When a ticket enters the board at all'],
         ['inactivity_days', '2', 'When "no recent activity" is flagged'],
         ['prolonged_inactivity_days', '4', 'Prolonged inactivity flag, and the inactivity score ceiling'],
         ['sla_risk_pct', '75', 'Percent consumed at which SLA risk is flagged'],
         ['closure_followup_count', '3', 'Documented chases needed for a closure candidate'],
         ['closure_silence_days', '5', 'Caller silence needed alongside them'],
         ['digest_max_tickets_per_agent', '25', 'Cap on an individual agent digest']],
        kicker='Tuning', widths=[32, 12, 56],
        lead='All configurable per deployment. These are the shipped defaults.',
        notes='Offer to change these for the pilot queue. They were chosen to '
              'be defensible, not to be right for every team.')

    # -- 3. scoring ------------------------------------------------------
    d.section('03', 'The attention score',
              'Why a ticket is where it is in the list',
              notes='')

    d.weights(
        'Seven components', WEIGHT_ROWS,
        lead='Each component is normalised to 0-100, multiplied by its '
             'weight, and summed. Nothing else contributes.',
        footnote='Editable in the UI by an administrator. If an edit leaves '
                 'the total at anything other than 100, the service reverts '
                 'to these defaults and writes a warning - an incomplete edit '
                 'cannot silently distort every score on the board.',
        notes='')

    d.bullets(
        'How each component is calculated',
        [
            'SLA risk - breached scores 100; otherwise percent consumed, '
            'falling back to a time-remaining band when no percentage exists.',
            'Inactivity - linear to the prolonged-inactivity threshold, then '
            'saturated at 100.',
            'Ticket age - zero until the aged threshold, then linear; one '
            'further threshold-width past it reaches 100.',
            'Business priority - P1 scores 100, P2 80, P3 50, P4 25, P5 10.',
            'Reassignment activity - 20 points per reassignment, 30 per '
            'reopen, capped at 100.',
            'Unactioned delay - 60 for an unanswered caller, plus points for '
            'a cleared dependency and a documented closure candidate.',
            'Duration overrun - set when past the p90 for comparable '
            'incidents.',
        ],
        kicker='Arithmetic',
        notes='Nobody will remember these. The point of showing them is that '
              'they exist in writing and are in the repository.')

    d.statement(
        'The property that matters',
        'Every point on the board is attributable to a named component.',
        'A lead who asks "why is this one top?" gets a breakdown - 11.3 of 25 '
        'for inactivity, 9.4 of 25 for SLA risk - rather than a model\'s '
        'opinion. That is the difference between a tool a team tunes and a '
        'tool a team works around.',
        notes='Show "Why it ranks here" on the live ticket page at this point.')

    # -- 4. ai -----------------------------------------------------------
    d.section('04', 'The language model',
              'A narrow job, tightly bounded',
              notes='')

    d.table(
        'What it is asked to produce',
        ['Output', 'Used for'],
        [['Recommended action', f'One of {len(ACTION_LABELS)} fixed values - resolve, follow up, '
                                'chase vendor, reassign, escalate, await dependency, close stale, '
                                'no action'],
         ['Rationale', 'One or two sentences shown under the recommendation'],
         ['Draft work note', 'Internal note, copied to clipboard by the agent'],
         ['Draft caller message', 'Customer-facing text, edited before use'],
         ['Suggested target group', 'Only when the action is reassign'],
         ['Evidence', 'Similar resolved incidents and knowledge articles it drew on'],
         ['Confidence', 'Below 0.55 the recommendation is not surfaced at all']],
        widths=[26, 74],
        lead='A fixed schema, validated on the way back. Free text is limited '
             'to the rationale and the two drafts.',
        notes='')

    d.split(
        'The boundaries', 'Enforced in code',
        ['No write client exists for ServiceNow',
         'Personal data removed before the request is sent',
         'Recommendations below the confidence floor are withheld',
         'Suggested groups are verified against real groups, or flagged',
         'Suspected prompt injection in ticket content is flagged for review'],
        'Enforced by design',
        ['Ranking is arithmetic, never model output',
         'Drafts are drafts - a person sends them',
         'Shadow mode computes everything and emails nothing',
         'Every suggestion is recorded and later scored by a lead',
         'Unchanged tickets are never re-analysed'],
        kicker='Guardrails', left_colour=ACCENT, right_colour=GOOD,
        notes='The last point on the right is cost control as well as '
              'stability: a ticket is only sent to the model when its content '
              'has actually changed.')

    d.bullets(
        'Evidence, and why it is retrieved rather than recalled',
        [
            ('Similar incidents come from your own history',
             'Resolved incidents from the last 180 days are indexed nightly '
             'and searched by similarity.'),
            ('Knowledge articles are matched the same way',
             'Which also produces the "article exists but is not attached" '
             'flag.'),
            ('Every citation is shown',
             'The ticket page lists what the recommendation was based on, so '
             'a lead can check it.'),
            ('Nothing is invented',
             'A suggestion with no supporting evidence is a low-confidence '
             'suggestion, and low-confidence suggestions are not shown.'),
        ],
        kicker='Retrieval',
        notes='')

    # -- 5. delivery -----------------------------------------------------
    d.section('05', 'Delivery',
              'Board, digests, alerts',
              notes='')

    d.pipeline('The four loops', CADENCE,
               footnote='Jobs do not stack. If a run overruns its interval '
                        'the next is skipped rather than queued, so a slow '
                        'period in ServiceNow cannot turn into a backlog of '
                        'concurrent syncs.',
               notes='')

    d.bullets(
        'The daily lead digest',
        [
            'Ranked worst-first, with the score and the reasons for it.',
            'Split into what is new, what got worse, and what is steady - a '
            'lead who saw the same forty rows yesterday needs the six that '
            'changed.',
            'Grouped by who owes the next action, so "nine of these are ours" '
            'is the first thing on the page.',
            'Sent per queue on its own schedule and timezone.',
            'All times in US Central, with the timezone named at the foot of '
            'the mail.',
        ],
        kicker='Email',
        notes='')

    d.table(
        'Risk alerts',
        ['Alert', 'Fires when'],
        [['SLA breached', 'The resolution SLA has been passed'],
         ['SLA at risk', 'Consumption crosses the configured percentage'],
         ['Caller awaiting reply', 'The caller spoke last and nobody has answered'],
         ['Dependency cleared', 'The change or problem it was held for has closed'],
         ['Prolonged inactivity', 'No real Service Desk action past the threshold']],
        widths=[30, 70],
        lead='Detected on the ten-minute loop. Deduplicated to once per '
             'ticket, per alert type, per day - a ticket that stays breached '
             'does not re-alert every ten minutes.',
        notes='The dedupe is what makes these survivable. Without it this '
              'becomes a mail filter within a week.')

    d.cards(
        'The web interface', [
            ('Board',
             'Ranked, with filters for queue, agent, who owes the action, '
             'risk flag and recommended action.'),
            ('Ticket',
             'Signals, score breakdown, activity grouped as ServiceNow '
             'groups it, drafts, and the verdict buttons.'),
            ('By agent',
             'Per-person aged backlog, worst score, average idle, SLA '
             'breaches, knowledge gaps.'),
            ('Accuracy',
             'Agreement rate, precision, coverage, and breaches that fired '
             'no warning.'),
            ('Weights',
             'The seven score components, editable, with the sum-to-100 '
             'guard.'),
            ('Access',
             'Role requests from users, approved or declined by an '
             'administrator.'),
        ],
        kicker='Screens', per_row=3,
        notes='')

    # -- 6. measurement --------------------------------------------------
    d.section('06', 'Measurement',
              'How we will know whether to keep it',
              notes='')

    d.table(
        'The verdict a lead records',
        ['Verdict', 'Meaning', 'Counts as'],
        [['Agree', 'This is the right next step', 'Agreement'],
         ['Mostly right', 'Right direction, I adjusted it', 'Agreement'],
         ['Wrong', 'Not the right step', 'Disagreement'],
         ['Not applicable', 'Should not have been flagged at all', 'Disagreement'],
         ['Already handled', 'Dealt with before I got here', 'Excluded, and snoozed']],
        widths=[22, 48, 30],
        lead='One click on the ticket page. This is the only thing that '
             'makes accuracy measurable, and it is the one thing the tool '
             'cannot do for itself.',
        notes='Ask explicitly for this during the pilot. Without verdicts '
              'there is no number, and without a number this is a matter of '
              'opinion in three months.')

    d.cards(
        'What the Accuracy page reports', [
            ('Agreement rate',
             'Of the suggestions a lead judged, how many they agreed with, '
             'exactly or with edits.'),
            ('Actionable precision',
             'Of the tickets flagged, how many turned out to matter.'),
            ('Coverage',
             'How many recommended tickets were actually reviewed - a high '
             'agreement rate on three tickets means nothing.'),
            ('By action type',
             'Which recommendations are reliable and which are not, with '
             'average confidence for each.'),
            ('False quiet',
             'Incidents that breached with no prior warning. The number a '
             'vendor would leave out.'),
            ('Volume over time',
             'Board size and feedback volume, counted on UTC day '
             'boundaries.'),
        ],
        kicker='Metrics', per_row=3,
        notes='False quiet is deliberately on this page. It is the metric '
              'that tells you the tool is missing things, and it is the one '
              'that would be easiest to omit.')

    # -- 7. operations ---------------------------------------------------
    d.section('07', 'Running it',
              'Deployment, access, failure behaviour',
              notes='')

    d.bullets(
        'Deployment shape',
        [
            'Runs beside the existing ticket audit tool, not inside it - its '
            'own service, own database, own port, own systemd unit.',
            'The only contact with the audit tool is one optional read-only '
            'query for agent compliance scores, off by default.',
            'ServiceNow access is read-only: incidents, history, SLAs, '
            'knowledge, group membership.',
            'Credentials in Vault. Nothing sensitive in a configuration file.',
            'Behind nginx with TLS; the service itself listens locally.',
        ],
        kicker='Architecture',
        notes='Useful if anyone from platform or security joins. Otherwise '
              'skip to access.')

    d.table(
        'Roles',
        ['Role', 'Who', 'Can'],
        [['Viewer', 'Agents, managers',
          'See the board and tickets for their own queues'],
         ['Lead', 'Service Desk leads',
          'All of the above, plus record verdicts, snooze, request re-analysis, '
          'receive the digest'],
         ['Admin', 'Application team',
          'All of the above, plus tune weights and approve role requests']],
        widths=[14, 24, 62],
        lead='Sign-in is through Keycloak with your existing account. A new '
             'user is a Viewer until somebody says otherwise.',
        notes='')

    d.bullets(
        'How access is granted',
        [
            ('First sign-in creates the account as a Viewer',
             'No ticket, no waiting, no administrator involved.'),
            ('Queues come from ServiceNow',
             'The groups you belong to are read on first sign-in, and you '
             'see those queues and no others.'),
            ('Elevation is a request, not an email to somebody',
             'A Viewer asks for Lead from their profile page, with a reason. '
             'Every administrator is notified and the first to decide wins.'),
            ('Leavers stop receiving mail',
             'Accounts are reconciled against Keycloak daily, with guards '
             'that refuse to deactivate everybody at once.'),
        ],
        kicker='Joiners and leavers',
        notes='The last point exists because the user table is also the mail '
              'recipient list. A leaver who is not deactivated keeps '
              'receiving incident descriptions and caller names indefinitely.')

    d.split(
        'When something goes wrong', 'It fails quietly in the safe direction',
        ['No queues assigned shows nothing, never everything',
         'A ticket outside your scope is indistinguishable from one that '
         'does not exist',
         'ServiceNow unreachable leaves the board as it was',
         'A model failure withholds the recommendation; signals still show',
         'A mass "everyone has left" result changes nothing'],
        'And says so',
        ['A built-in health check covers every dependency',
         'Sync state, watermark and last error are recorded each run',
         'Shadow mode computes everything and sends nothing',
         'Every retirement from the board is logged with its reason',
         'Alert history is kept, so a missed warning is auditable'],
        kicker='Failure behaviour', left_colour=WARN, right_colour=ACCENT,
        notes='The left column is a design principle rather than a feature '
              'list - an empty scope meaning "everything" is the kind of bug '
              'that only shows up in an audit.')

    # -- 8. pilot --------------------------------------------------------
    d.section('08', 'The pilot',
              'What we are proposing, and what we need',
              notes='')

    d.table(
        'Proposed sequence',
        ['Stage', 'Duration', 'What happens', 'What we learn'],
        [['Shadow', '1 week',
          'Everything computes, nothing is emailed',
          'Whether the ranking matches your judgement'],
         ['Tune', 'A few days',
          'Adjust weights and thresholds for this queue',
          'What this team actually prioritises'],
         ['Live, one queue', '2 weeks',
          'Digest and alerts on for the pilot queue',
          'Agreement rate, false quiet, review time'],
         ['Decide', '-',
          'Keep, change or stop, on the numbers',
          'Whether to extend to the other queues']],
        widths=[18, 14, 34, 34],
        notes='Be explicit that "stop" is on the list. A pilot that can only '
              'succeed is not a pilot.')

    d.bullets(
        'What we need from the Service Desk',
        [
            ('Two or three leads recording verdicts',
             'Roughly fifteen tickets a day between them is enough for a '
             'meaningful agreement rate inside two weeks.'),
            ('One queue to start with',
             'Whichever has the most aged pending tickets - the tool is '
             'least useful and least interesting on a clean queue.'),
            ('Honest disagreement about the ranking',
             'If age matters less here than SLA risk, we change the weights. '
             'That conversation is the point of the pilot.'),
            ('Thirty minutes at the end of week one',
             'To look at where it was wrong before anything goes live.'),
        ],
        kicker='The ask',
        notes='')

    d.statement(
        'The commitment',
        'If the agreement rate is poor, we will say so on this slide in six '
        'weeks.',
        'The measurement exists so this tool can be judged rather than '
        'believed. Questions, then the live board.',
        notes='Close here and go to the demo.')

    return d.save('Aged_Ticket_Advisor_Detailed.pptx')


# ---------------------------------------------------------------------------
# self-check
# ---------------------------------------------------------------------------

def verify():
    """Fail the build rather than ship a deck that misstates the service."""
    assert sum(DEFAULT_WEIGHTS.values()) == 100, DEFAULT_WEIGHTS
    assert set(WEIGHT_DESC) == set(DEFAULT_WEIGHTS), 'weight description drift'
    assert set(WEIGHT_NAMES) == set(DEFAULT_WEIGHTS), 'weight label drift'
    assert len(WEIGHT_ROWS) == 7
    # Quoted on the "who owes the next action" slide.
    assert len(PENDING_ACTION_OWNERS) == 6, PENDING_ACTION_OWNERS
    # Quoted on the alerts slide.
    for flag in ('SLA_BREACHED', 'SLA_AT_RISK', 'CALLER_AWAITING_REPLY',
                 'DEPENDENCY_CLEARED', 'PROLONGED_INACTIVITY'):
        assert flag in FLAG_LABELS, flag


if __name__ == '__main__':
    verify()
    for path in (overview(), detailed()):
        print(f'{path.name:<44} {path.stat().st_size / 1024:6.0f} KB  {path}')
