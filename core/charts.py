"""Server-rendered SVG charts.

No charting library and no CDN. The service runs on a locked-down host where
fetching a script at page load is not a thing that happens, and vendoring a
megabyte of JavaScript to draw a bar chart is a poor trade. These render to
inline SVG in the response, which means:

  * they work with scripting off, and in a printed page
  * they theme themselves - every colour is a CSS custom property, so dark
    mode is the same markup against different variables rather than a second
    rendering path that can drift out of step
  * a screen reader hears one chart with a summary rather than a pile of
    shapes: every chart carries role, title and desc, and every mark carries
    a <title>, which browsers also surface as a native hover tooltip

The mark specs are fixed and deliberate. Bars cap at 26px with a rounded
data-end and a 2px gap in the surface colour between neighbours; lines are
2px with round joins; markers are at least 8px across with a 2px surface
ring; gridlines are hairline and one step off the surface. The data is the
only thing allowed to be loud.

Colour is assigned by the job it does, never by position in a loop:

    SERIES   identity    up to three slots, validated as a set for colour
                         vision deficiency across every pair
    RAMP     magnitude   one hue light to dark, for ordered bands
    STATUS   state       good / warning / serious / critical, reserved, and
                         never borrowed for a series

Everything here is pure - data in, markup out. No database, no request, no
settings, which is what makes it testable without a server.
"""

import html
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from markupsafe import Markup

# The values live in app.css so the board and the charts cannot disagree
# about what "critical" looks like.
SERIES = ('var(--series-1)', 'var(--series-2)', 'var(--series-3)')
RAMP = ('var(--ramp-1)', 'var(--ramp-2)', 'var(--ramp-3)', 'var(--ramp-4)')
STATUS = {
    'good': 'var(--status-good)',
    'warning': 'var(--status-warning)',
    'serious': 'var(--status-serious)',
    'critical': 'var(--status-critical)',
    'neutral': 'var(--series-1)',
}

SURFACE = 'var(--surface-1)'
GRID = 'var(--grid)'
AXIS = 'var(--axis)'
INK = 'var(--text-primary)'
INK2 = 'var(--text-secondary)'
MUTED = 'var(--text-muted)'
INVERSE = 'var(--text-inverse)'

BAR_MAX = 26          # px; capped so a sparse chart does not grow slabs
GAP = 2               # px of surface between touching marks
LINE_W = 2
MARKER_R = 4.5        # >= 8px diameter
RING = 2

DASH = '—'
DOT = '·'


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt(value: Any, suffix: str = '') -> str:
    """Readable numbers. 1284 -> 1,284 and 12.0 -> 12; None is an em dash."""
    if value is None:
        return DASH
    if isinstance(value, float):
        value = int(value) if float(value).is_integer() else round(value, 1)
    if isinstance(value, int):
        return f'{value:,}{suffix}'
    return f'{value}{suffix}'


def _nice_ceiling(value: float) -> float:
    """Round an axis maximum up to a number a person would have chosen."""
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        candidate = step * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _chrome(width: int, height: int, title: str, description: str,
            body: str, classes: str = '') -> Markup:
    return Markup(
        f'<svg class="chart {classes}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{esc(title)}. {esc(description)}">'
        f'<title>{esc(title)}</title><desc>{esc(description)}</desc>'
        f'{body}</svg>')


def _text(x: float, y: float, value: Any, size: float = 11,
          colour: str = MUTED, anchor: str = 'start',
          weight: str = '400') -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
            f'fill="{colour}" text-anchor="{anchor}" font-weight="{weight}" '
            f'dominant-baseline="middle">{esc(value)}</text>')


def _gridlines(x0: float, x1: float, ticks: Sequence[Tuple[float, Any]],
               label_x: float) -> str:
    out = []
    for y, label in ticks:
        out.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" '
                   f'y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(_text(label_x, y, label, 10, MUTED, 'end'))
    return ''.join(out)


def _y_ticks(top: float, plot_top: float, plot_bottom: float,
             count: int = 4, suffix: str = '') -> List[Tuple[float, str]]:
    return [(plot_bottom - (plot_bottom - plot_top) * (i / count),
             fmt(top * i / count, suffix)) for i in range(count + 1)]


# ---------------------------------------------------------------------------
# line / area
# ---------------------------------------------------------------------------

def line_chart(labels: Sequence[str], series: Sequence[Dict[str, Any]],
               height: int = 220, width: int = 640, suffix: str = '',
               target: Optional[float] = None, target_label: str = '',
               annotations: Sequence[Dict[str, Any]] = ()) -> Markup:
    """One or more lines over a shared x.

    series: [{'name': str, 'values': [...], 'colour': css, 'area': bool}]

    Only the final point is labelled. A value beside every point is chaos
    and goes unread; the axis, the hover tooltip and the end label between
    them carry the rest.
    """
    pad_l, pad_r, pad_t, pad_b = 46, 20, 16, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    plot_bottom = pad_t + plot_h

    flat = [v for s in series for v in s['values'] if v is not None]
    if not flat:
        return empty_chart('No data in this range', width, height)

    top = _nice_ceiling(
        max(flat + ([target] if target is not None else [])) * 1.08)

    def px(index: int) -> float:
        return pad_l + plot_w * index / max(len(labels) - 1, 1)

    def py(value: float) -> float:
        return plot_bottom - plot_h * (value / top if top else 0)

    parts = [_gridlines(pad_l, pad_l + plot_w,
                        _y_ticks(top, pad_t, plot_bottom, 4, suffix),
                        pad_l - 8)]

    if target is not None:
        ty = py(target)
        parts.append(f'<line x1="{pad_l}" y1="{ty:.1f}" '
                     f'x2="{pad_l + plot_w}" y2="{ty:.1f}" '
                     f'stroke="{STATUS["good"]}" stroke-width="1.5"/>')
        if target_label:
            parts.append(_text(pad_l + 5, ty - 9, target_label, 10,
                               STATUS['good']))

    for note in annotations:
        ax = px(note['index'])
        parts.append(f'<line x1="{ax:.1f}" y1="{pad_t}" x2="{ax:.1f}" '
                     f'y2="{plot_bottom:.1f}" stroke="{AXIS}" '
                     f'stroke-width="1"/>')
        parts.append(_text(ax + 5, pad_t + 7, note['text'], 10, MUTED))

    for index, entry in enumerate(series):
        colour = entry.get('colour') or SERIES[index % len(SERIES)]
        points = [(px(i), py(v)) for i, v in enumerate(entry['values'])
                  if v is not None]
        if not points:
            continue
        path = ' '.join(f'{"M" if i == 0 else "L"}{x:.1f},{y:.1f}'
                        for i, (x, y) in enumerate(points))

        if entry.get('area'):
            # A wash, never a saturated block.
            parts.append(
                f'<path d="{path} L{points[-1][0]:.1f},{plot_bottom:.1f} '
                f'L{points[0][0]:.1f},{plot_bottom:.1f} Z" fill="{colour}" '
                f'opacity="0.10"/>')

        parts.append(f'<path d="{path}" fill="none" stroke="{colour}" '
                     f'stroke-width="{LINE_W}" stroke-linejoin="round" '
                     f'stroke-linecap="round"/>')

        # A 2px line is impossible to hover, so each point gets an invisible
        # target big enough to actually hit.
        for i, (x, y) in enumerate(points):
            label = labels[i] if i < len(labels) else ''
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="transparent">'
                f'<title>{esc(label)}: '
                f'{esc(fmt(entry["values"][i], suffix))}</title></circle>')

        x, y = points[-1]
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{MARKER_R}" '
                     f'fill="{colour}" stroke="{SURFACE}" '
                     f'stroke-width="{RING}"/>')
        parts.append(_text(x - 9, y - 14, fmt(entry['values'][-1], suffix),
                           12, INK, 'end', '700'))

    step = max(1, (len(labels) - 1) // 4 or 1)
    shown = list(range(0, len(labels), step))
    if shown and shown[-1] != len(labels) - 1:
        shown.append(len(labels) - 1)
    for index in shown:
        parts.append(_text(px(index), plot_bottom + 14, labels[index], 10,
                           MUTED, 'middle'))

    names = ', '.join(s['name'] for s in series)
    latest = '; '.join(f'{s["name"]} {fmt(s["values"][-1], suffix)}'
                       for s in series if s['values'])
    return _chrome(width, height, names,
                   f'{labels[0]} to {labels[-1]}. Latest: {latest}',
                   ''.join(parts))


# ---------------------------------------------------------------------------
# horizontal bars
# ---------------------------------------------------------------------------

def hbar_chart(rows: Sequence[Dict[str, Any]], width: int = 420,
               suffix: str = '', label_w: int = 150,
               max_value: Optional[float] = None) -> Markup:
    """Ranked categories, value labelled at the tip.

    No x axis: the number is the point, and an axis makes the reader measure
    where a direct label simply says it.
    """
    if not rows:
        return empty_chart('Nothing to show', width, 120)

    bar_h, row_h = 22, 36
    height = len(rows) * row_h + 10
    track = width - label_w - 58
    top = max_value or max([r['value'] or 0 for r in rows] + [1])

    parts = []
    for index, row in enumerate(rows):
        y = 6 + index * row_h
        value = row['value'] or 0
        colour = row.get('colour') or SERIES[0]
        w = max(track * (value / top) if top else 0, 2)
        note = f' - {row["note"]}' if row.get('note') else ''

        parts.append(_text(label_w - 10, y + bar_h / 2, row['label'], 11,
                           INK2, 'end'))
        parts.append(
            f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" '
            f'rx="4" fill="{colour}"><title>{esc(row["label"])}: '
            f'{esc(fmt(value, suffix))}{esc(note)}</title></rect>')
        parts.append(_text(label_w + w + 8, y + bar_h / 2, fmt(value, suffix),
                           11.5, INK, 'start', '600'))

    return _chrome(width, height, 'Ranked categories',
                   '; '.join(f'{r["label"]} {fmt(r["value"], suffix)}'
                             for r in rows),
                   ''.join(parts), 'chart-hbar')


# ---------------------------------------------------------------------------
# vertical bars
# ---------------------------------------------------------------------------

def bar_chart(labels: Sequence[str], series: Sequence[Dict[str, Any]],
              stacked: bool = False, height: int = 220, width: int = 640,
              suffix: str = '') -> Markup:
    pad_l, pad_r, pad_t, pad_b = 44, 14, 14, 28
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    plot_bottom = pad_t + plot_h

    if not labels or not series:
        return empty_chart('No data in this range', width, height)

    if stacked:
        top = _nice_ceiling(max(
            [sum(s['values'][i] or 0 for s in series)
             for i in range(len(labels))] + [1]) * 1.06)
    else:
        top = _nice_ceiling(max(
            [v or 0 for s in series for v in s['values']] + [1]) * 1.06)

    slot = plot_w / max(len(labels), 1)
    count = 1 if stacked else len(series)
    bar_w = max(min(BAR_MAX, (slot - 10) / count - (0 if stacked else GAP)), 3)

    parts = [_gridlines(pad_l, pad_l + plot_w,
                        _y_ticks(top, pad_t, plot_bottom, 4, suffix),
                        pad_l - 8)]

    for index, label in enumerate(labels):
        centre = pad_l + slot * (index + 0.5)
        if stacked:
            cursor = plot_bottom
            for depth, entry in enumerate(series):
                value = entry['values'][index] or 0
                if value <= 0:
                    continue
                h = plot_h * value / top
                colour = entry.get('colour') or RAMP[depth % len(RAMP)]
                # The gap in the surface colour separates segments. Never a
                # stroke around the mark - that is ink which is not data.
                parts.append(
                    f'<rect x="{centre - bar_w / 2:.1f}" '
                    f'y="{cursor - h:.1f}" width="{bar_w:.1f}" '
                    f'height="{max(h - GAP, 1):.1f}" rx="2" fill="{colour}">'
                    f'<title>{esc(label)} {DOT} {esc(entry["name"])}: '
                    f'{esc(fmt(value, suffix))}</title></rect>')
                cursor -= h
        else:
            group_w = count * bar_w + (count - 1) * GAP
            for depth, entry in enumerate(series):
                value = entry['values'][index] or 0
                h = plot_h * value / top
                colour = entry.get('colour') or SERIES[depth % len(SERIES)]
                x = centre - group_w / 2 + depth * (bar_w + GAP)
                parts.append(
                    f'<rect x="{x:.1f}" y="{plot_bottom - h:.1f}" '
                    f'width="{bar_w:.1f}" height="{max(h, 1):.1f}" rx="3" '
                    f'fill="{colour}">'
                    f'<title>{esc(label)} {DOT} {esc(entry["name"])}: '
                    f'{esc(fmt(value, suffix))}</title></rect>')

    step = max(1, len(labels) // 6 or 1)
    for index in range(0, len(labels), step):
        parts.append(_text(pad_l + slot * (index + 0.5), plot_bottom + 14,
                           labels[index], 10, MUTED, 'middle'))

    names = ', '.join(s['name'] for s in series)
    return _chrome(width, height, names,
                   f'{names} across {len(labels)} periods', ''.join(parts))


def stacked_hbar(rows: Sequence[Dict[str, Any]],
                 segments: Sequence[Dict[str, Any]],
                 width: int = 520, label_w: int = 140,
                 suffix: str = '') -> Markup:
    """Composition per row, each segment labelled where it fits."""
    if not rows:
        return empty_chart('Nothing to show', width, 120)

    bar_h, row_h = 26, 42
    height = len(rows) * row_h + 10
    track = width - label_w - 24
    top = max((sum(v or 0 for v in r['values']) for r in rows), default=1) or 1

    parts = []
    for index, row in enumerate(rows):
        y = 6 + index * row_h
        parts.append(_text(label_w - 10, y + bar_h / 2, row['label'], 11,
                           INK2, 'end'))
        cursor = float(label_w)
        for depth, value in enumerate(row['values']):
            if not value or value <= 0:
                continue
            w = track * value / top
            colour = (segments[depth].get('colour')
                      or SERIES[depth % len(SERIES)])
            parts.append(
                f'<rect x="{cursor:.1f}" y="{y}" '
                f'width="{max(w - GAP, 1):.1f}" height="{bar_h}" rx="3" '
                f'fill="{colour}"><title>{esc(row["label"])} {DOT} '
                f'{esc(segments[depth]["name"])}: {esc(fmt(value, suffix))}'
                f'</title></rect>')
            if w > 34:
                parts.append(_text(cursor + w / 2, y + bar_h / 2, fmt(value),
                                   10.5, INVERSE, 'middle', '600'))
            cursor += w

    return _chrome(width, height, 'Composition',
                   '; '.join(r['label'] for r in rows), ''.join(parts),
                   'chart-hbar')


# ---------------------------------------------------------------------------
# small forms
# ---------------------------------------------------------------------------

def sparkline(values: Sequence[float], width: int = 120, height: int = 30,
              colour: str = SERIES[0]) -> Markup:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return Markup('')
    low, high = min(clean), max(clean)
    span = max(high - low, 1e-9)
    step = width / (len(values) - 1)
    points = [(i * step, height - 3 - (height - 6) * ((v - low) / span))
              for i, v in enumerate(values) if v is not None]
    path = ' '.join(f'{"M" if i == 0 else "L"}{x:.1f},{y:.1f}'
                    for i, (x, y) in enumerate(points))
    return Markup(
        f'<svg class="sparkline" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="1.6" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'<circle cx="{points[-1][0]:.1f}" cy="{points[-1][1]:.1f}" r="2.5" '
        f'fill="{colour}"/></svg>')


def gauge(value: float, maximum: float = 100, label: str = '',
          tone: str = 'neutral', size: int = 140) -> Markup:
    """A single proportion as an arc.

    An arc rather than a bar when it is the only number on the panel: the
    shape says "out of a whole" without needing a second mark to compare to.
    """
    pct = 0.0 if not maximum else max(0.0, min(1.0, value / maximum))
    radius = size / 2 - 14
    circumference = math.pi * radius
    cx, cy = size / 2, size / 2 + 10
    arc = f'M {cx - radius} {cy} A {radius} {radius} 0 0 1 {cx + radius} {cy}'
    colour = STATUS.get(tone, STATUS['neutral'])

    return _chrome(
        size, int(size * 0.74), label or 'Proportion',
        f'{fmt(round(value, 1))} of {fmt(maximum)}',
        f'<path d="{arc}" fill="none" stroke="{GRID}" stroke-width="10" '
        f'stroke-linecap="round"/>'
        f'<path d="{arc}" fill="none" stroke="{colour}" stroke-width="10" '
        f'stroke-linecap="round" '
        f'stroke-dasharray="{circumference * pct:.1f} {circumference:.1f}"/>'
        + _text(cx, cy - 18, f'{round(pct * 100)}%', 25, INK, 'middle', '700')
        + (_text(cx, cy + 12, label, 10, MUTED, 'middle') if label else ''),
        'chart-gauge')


def heatmap(row_labels: Sequence[str], col_labels: Sequence[str],
            matrix: Sequence[Sequence[Any]], width: int = 560,
            label_w: int = 140, suffix: str = '') -> Markup:
    """Magnitude across two categories. One hue, stepped light to dark."""
    if not matrix or not row_labels:
        return empty_chart('Nothing to show', width, 120)

    cell_h, gap = 28, 3
    height = len(row_labels) * (cell_h + gap) + 28
    cell_w = (width - label_w - 10) / max(len(col_labels), 1)
    peak = max((v for row in matrix for v in row if v), default=1) or 1

    parts = [_text(label_w + cell_w * (i + 0.5), 10, label, 9.5, MUTED,
                   'middle')
             for i, label in enumerate(col_labels)]

    for r, label in enumerate(row_labels):
        y = 22 + r * (cell_h + gap)
        parts.append(_text(label_w - 10, y + cell_h / 2, label, 11, INK2,
                           'end'))
        for c in range(len(col_labels)):
            value = matrix[r][c] if c < len(matrix[r]) else None
            x = label_w + c * cell_w
            if not value:
                parts.append(
                    f'<rect x="{x:.1f}" y="{y}" width="{cell_w - gap:.1f}" '
                    f'height="{cell_h}" rx="3" fill="{GRID}" '
                    f'opacity="0.45"/>')
                continue
            # Ordinal steps, not a continuous alpha: the same hue at 20% and
            # at 28% is not two distinguishable values.
            step = min(3, int(4 * value / peak)) if peak else 0
            parts.append(
                f'<rect x="{x:.1f}" y="{y}" width="{cell_w - gap:.1f}" '
                f'height="{cell_h}" rx="3" fill="{RAMP[step]}">'
                f'<title>{esc(label)} {DOT} {esc(col_labels[c])}: '
                f'{esc(fmt(value, suffix))}</title></rect>')
            parts.append(_text(
                x + (cell_w - gap) / 2, y + cell_h / 2, fmt(value), 10.5,
                INVERSE if step >= 2 else INK, 'middle', '600'))

    return _chrome(width, height, 'Heatmap',
                   f'{len(row_labels)} rows by {len(col_labels)} columns',
                   ''.join(parts), 'chart-heatmap')


def calibration_chart(bins: Sequence[str], stated: Sequence[float],
                      actual: Sequence[float], width: int = 560,
                      height: int = 240) -> Markup:
    """Stated confidence against what actually happened.

    Its own form because the comparison is the whole point: the diagonal is
    where an honest model sits, the bar is where it actually is, and the
    connector between them is the error. Three separate charts would not say
    that.
    """
    pad_l, pad_r, pad_t, pad_b = 44, 16, 22, 30
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    plot_bottom = pad_t + plot_h

    if not bins:
        return empty_chart('Nothing judged in this range', width, height)

    slot = plot_w / len(bins)
    bar_w = min(BAR_MAX + 8, slot - 16)

    parts = [_gridlines(pad_l, pad_l + plot_w,
                        _y_ticks(100, pad_t, plot_bottom, 4, '%'), pad_l - 8)]

    ideal = [(pad_l + slot * (i + 0.5), plot_bottom - plot_h * (s or 0) / 100)
             for i, s in enumerate(stated)]
    parts.append('<path d="' + ' '.join(
        f'{"M" if i == 0 else "L"}{x:.1f},{y:.1f}'
        for i, (x, y) in enumerate(ideal)) +
        f'" fill="none" stroke="{AXIS}" stroke-width="1.6"/>')
    parts.append(_text(ideal[-1][0] + 6, ideal[-1][1] - 4, 'honest', 10,
                       MUTED))

    for index, label in enumerate(bins):
        centre = pad_l + slot * (index + 0.5)
        got, said = actual[index] or 0, stated[index] or 0
        h = plot_h * got / 100
        overstated = said - got
        tone = (STATUS['critical'] if overstated >= 8 else
                STATUS['serious'] if overstated >= 4 else SERIES[0])
        parts.append(
            f'<rect x="{centre - bar_w / 2:.1f}" y="{plot_bottom - h:.1f}" '
            f'width="{bar_w:.1f}" height="{max(h, 1):.1f}" rx="3" '
            f'fill="{tone}"><title>Said {esc(label)} {DOT} actually agreed '
            f'{esc(round(got))}%</title></rect>')
        parts.append(_text(centre, plot_bottom - h - 11, f'{round(got)}%', 11,
                           INK, 'middle', '700'))
        if overstated >= 4:
            parts.append(
                f'<line x1="{centre:.1f}" '
                f'y1="{plot_bottom - plot_h * said / 100:.1f}" '
                f'x2="{centre:.1f}" y2="{plot_bottom - h:.1f}" '
                f'stroke="{STATUS["critical"]}" stroke-width="1.6"/>')
        parts.append(_text(centre, plot_bottom + 14, label, 10, MUTED,
                           'middle'))

    worst = max((s - a for s, a in zip(stated, actual)), default=0)
    return _chrome(width, height, 'Confidence calibration',
                   f'Largest overstatement {round(worst)} points',
                   ''.join(parts))


def empty_chart(message: str, width: int = 420, height: int = 120) -> Markup:
    """Said in words, in the space the chart would have taken.

    An empty frame reads as a broken panel and a blank area reads as a
    missing one. Neither says "there is genuinely nothing here", which is
    usually the truth and is sometimes the good news.
    """
    return _chrome(width, height, 'No data', message,
                   _text(width / 2, height / 2, message, 12, MUTED, 'middle'),
                   'chart-empty')
