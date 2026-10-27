"""Charts for the settings page's Monitor section: pure functions from numbers to inline SVG strings.

Every chart works under the page's Content-Security-Policy (no script, no image file, no url()): inline SVG, sized by the
page, with the hover value in an SVG <title> on each bar, point or cell, and a "Show the numbers" table beside it
(numbers()), so nothing is read from a colour alone or from a hover alone. An SVG here has no viewBox: x is a percentage
of the width and y is in pixels, so a chart fills a 375-pixel phone and a desktop card alike, and its text never scales.

The colours follow the dataviz method (checked with its validator on the page's white surface): one series wears the
page's blue; good, warning and bad are status colours and never a series; everything that is not the point is grey.
Marks are thin, bars have rounded tops, a 2-pixel gap separates stacked segments, gridlines are solid hairlines and only
a threshold or a forecast is dashed.
"""
import html
import math

ACCENT = "var(--c-accent)"  # one series (categorical slot 1, the page's blue)
SECOND = "var(--c-second)"  # a second series (slot 2)
GOOD, WARN, BAD = "var(--c-good)", "var(--c-warn)", "var(--c-bad)"  # status: good, warning, bad; each always comes with words
QUIET = "var(--c-quiet)"  # de-emphasis: what is not the point (below 3:1 on white, so every chart has its numbers table)
INK = "var(--c-ink)"  # thresholds, release markers
GRID, AXIS, TRACK, EMPTY = "var(--c-grid)", "var(--c-axis)", "var(--c-track)", "var(--c-empty)"

TOP, AXIS_BAND = 18, 20  # pixels above the plot (the top label) and below it (the time labels)


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def pct(fraction):
    """A fraction of the width as an SVG percentage, short: 0.125 -> 12.5%."""
    return f"{min(max(fraction, 0.0), 1.0) * 100:.3f}".rstrip("0").rstrip(".") + "%"


def nice(value):
    """A round top for an axis at or above `value`: 1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6 or 8 times a power of ten."""
    if not value or value <= 0:
        return 1
    base = 10 ** math.floor(math.log10(value))
    return next(step * base for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if value <= step * base * (1 + 1e-9))


def labelled(value, unit):
    """An axis value with its unit: 1.2K requests, 1 error, 300 ms, 80%."""
    number = compact(value)
    if unit == "%":
        return f"{number}%"
    if value == 1 and unit.endswith("s") and unit not in ("ms", "USD"):
        unit = unit[:-1]
    return f"{number} {unit}".strip()


def compact(value):
    """An axis or tile number: 950, 1.2K, 3.4M; fractions to two significant digits."""
    if value is None:
        return "–"
    magnitude = abs(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            return f"{value / limit:.1f}".rstrip("0").rstrip(".") + suffix
    if magnitude and magnitude < 10 and value != int(value):
        return f"{value:.2g}" if magnitude < 1 else f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:,.0f}"


def frame(ident, title, desc, height, body, cls="chart"):
    """The SVG itself: an image to assistive technology, named by its title and described by its description."""
    return (f'<svg class="{cls}" width="100%" height="{height}" role="img" aria-labelledby="{esc(ident)}-t {esc(ident)}-d">'
            f'<title id="{esc(ident)}-t">{esc(title)}</title><desc id="{esc(ident)}-d">{esc(desc)}</desc>{body}</svg>')


def text(x, y, words, anchor="start", cls="t", dx=0):
    shift = f' dx="{dx}"' if dx else ""
    return f'<text x="{x}" y="{y}"{shift} text-anchor="{anchor}" class="{cls}">{esc(words)}</text>'


def hline(y, color=GRID, dash=None, width=1):
    pattern = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="0" x2="100%" y1="{y}" y2="{y}" stroke="{color}" stroke-width="{width}"{pattern}/>'


def bar(x0, x1, y, height, color, rounded=True, extra=""):
    """One bar between two fractions of the width, its top rounded and its base square."""
    left, width = pct(x0), pct(x1 - x0)
    if not rounded or height < 4:
        return f'<rect x="{left}" width="{width}" y="{y:.1f}" height="{height:.1f}" fill="{color}"{extra}/>'
    return (f'<rect x="{left}" width="{width}" y="{y:.1f}" height="{height:.1f}" rx="2" fill="{color}"{extra}/>'
            f'<rect x="{left}" width="{width}" y="{y + height - 2:.1f}" height="2" fill="{color}"/>')


def hits(count, top, bottom, tips):
    """A transparent column per step, as tall as the plot, carrying that step's values: the hover finds the time, not the mark."""
    if not tips:
        return ""
    width = 1 / count
    return "".join(f'<rect class="hit" x="{pct(index * width)}" width="{pct(width)}" y="{top}" height="{bottom - top}"><title>{esc(tip)}</title></rect>'
                   for index, tip in enumerate(tips) if tip)


def time_axis(height, labels):
    """Time labels under the plot: (fraction, words); the first anchors left, the last right, the rest in the middle."""
    parts = []
    for fraction, words in labels:
        anchor = "start" if fraction <= 0.02 else "end" if fraction >= 0.98 else "middle"
        parts.append(text(pct(fraction), height - 5, words, anchor))
    return "".join(parts)


def release_marks(markers, bottom):
    """Releases as thin vertical lines with a small flag on top; the flag and a wide invisible line carry the words."""
    parts = []
    for item in markers:
        x = pct(item["at"])
        colour = item.get("color") or INK
        if item.get("line", True):  # a day's flag (the activity chart) needs no line through its bar
            parts.append(f'<line x1="{x}" x2="{x}" y1="{TOP - 6}" y2="{bottom}" stroke="{colour}" stroke-width="1" opacity=".7"/>')
        parts.append(f'<svg x="{x}" y="{TOP - 9}" overflow="visible" class="flag"><path d="M-4.5 0h9l-4.5 6z" fill="{colour}"/></svg>'
                     f'<line class="mark-hit" x1="{x}" x2="{x}" y1="0" y2="{bottom}" stroke="transparent" stroke-width="12">'
                     f'<title>{esc(item["tip"])}</title></line>')
    return "".join(parts)


def plot_frame(height, top_words, axis):
    """The furniture under every vertical chart: a gridline with its value at the top, the baseline, the time labels.
    Returns the baseline's y and the markup."""
    bottom = height - AXIS_BAND
    return bottom, hline(TOP) + (text(0, TOP - 6, top_words) if top_words else "") + hline(bottom, AXIS) + time_axis(height, axis)


def scale(values, thresholds=(), floor_top=None):
    """The top of a line chart's axis, the thresholds drawn on it and those left out: a threshold more than twice the
    highest value is named in words instead of flattening the line against the floor."""
    known = [value for value in values if value is not None]
    highest = max(known) if known else 0
    shown, hidden = [], []
    for level, words in thresholds:
        if level is None:
            continue
        (shown if not highest or level <= 2 * highest or level <= (floor_top or 0) else hidden).append((level, words))
    top = nice(max([highest * 1.1, floor_top or 0, *(level for level, _ in shown)] or [1]))
    return top, shown, hidden


# ---- the charts

def sparkline(ident, title, desc, values, tips=None, height=36):
    """A small line without axes: the trend in grey, the newest value as a blue dot. Empty steps (None) leave a gap."""
    count = len(values)
    known = [value for value in values if value is not None]
    top = nice(max(known)) if known and max(known) > 0 else 1
    low, high = 4, height - 4

    def y(value):
        return high - (high - low) * value / top
    parts = [hline(high, AXIS)]
    for index in range(count - 1):
        if values[index] is not None and values[index + 1] is not None:
            parts.append(f'<line x1="{pct((index + .5) / count)}" x2="{pct((index + 1.5) / count)}" y1="{y(values[index]):.1f}" '
                         f'y2="{y(values[index + 1]):.1f}" stroke="{QUIET}" stroke-width="2" stroke-linecap="round"/>')
    last = next((index for index in range(count - 1, -1, -1) if values[index] is not None), None)
    if last is not None:
        parts.append(f'<circle cx="{pct((last + .5) / count)}" cy="{y(values[last]):.1f}" r="3.5" fill="{ACCENT}" stroke="var(--halo)" stroke-width="2"/>')
    parts.append(hits(count, 0, height, tips))
    return frame(ident, title, desc, height, "".join(parts), "chart spark")


def minibars(ident, title, desc, values, tips=None, colors=None, color=ACCENT, height=34):
    """Small columns without axes, for a tile: each step's value, `colors` giving a step its own colour."""
    count = max(len(values), 1)
    top = nice(max([value or 0 for value in values] or [0]))
    parts = [hline(height - 1, AXIS)]
    for index, value in enumerate(values):
        if value:
            tall = max((height - 3) * value / top, 2)
            parts.append(bar((index + .15) / count, (index + .85) / count, height - 1 - tall, tall, (colors or {}).get(index, color)))
    parts.append(hits(count, 0, height, tips))
    return frame(ident, title, desc, height, "".join(parts), "chart spark")


def bars(ident, title, desc, values, unit="", color=ACCENT, tips=None, axis=(), markers=(), height=150, colors=None):
    """Columns from one baseline, one per step; `colors` gives a step its own colour (the newest week, say)."""
    count = max(len(values), 1)
    top = nice(max([value or 0 for value in values] or [0]))
    bottom, furniture = plot_frame(height, labelled(top, unit), axis)
    parts = [furniture]
    for index, value in enumerate(values):
        if value:
            tall = max((bottom - TOP) * value / top, 2)
            parts.append(bar((index + .18) / count, (index + .82) / count, bottom - tall, tall, (colors or {}).get(index, color)))
    parts.append(release_marks(markers, bottom) if markers else "")
    parts.append(hits(count, TOP, bottom, tips))
    return frame(ident, title, desc, height, "".join(parts))


def stacked_bars(ident, title, desc, steps, series, unit="", tips=None, axis=(), markers=(), height=170):
    """Columns split by series, bottom first, with a 2-pixel gap between segments. `steps` holds one {series key: value}
    per step; `series` is [(key, label, colour)]. A segment that holds anything is at least 2 pixels tall, so one error shows."""
    count = max(len(steps), 1)
    totals = [sum(value or 0 for value in step.values()) for step in steps]
    top = nice(max(totals or [0]))
    bottom, furniture = plot_frame(height, labelled(top, unit), axis)
    parts = [furniture]
    for index, step in enumerate(steps):
        cursor, filled = bottom, [key for key, _, _ in series if step.get(key)]
        for key, _, colour in series:
            value = step.get(key) or 0
            if not value:
                continue
            gap = 2 if cursor < bottom else 0
            tall = max((bottom - TOP) * value / top, 2 + gap)
            parts.append(bar((index + .18) / count, (index + .82) / count, cursor - tall, tall - gap, colour, rounded=key == filled[-1]))
            cursor -= tall
    parts.append(release_marks(markers, bottom) if markers else "")
    parts.append(hits(count, TOP, bottom, tips))
    return frame(ident, title, desc, height, "".join(parts))


def line(ident, title, desc, values, unit="", color=ACCENT, tips=None, axis=(), markers=(), thresholds=(), height=170, floor_top=None):
    """A 2-pixel line through each step's value, a dot on the newest, dashed threshold lines with their words, and the
    releases as markers. A threshold far above the data is left out (scale() says which; the page names it in words)."""
    count = max(len(values), 1)
    top, shown, _ = scale(values, thresholds, floor_top)
    bottom, furniture = plot_frame(height, labelled(top, unit), axis)

    def y(value):
        return bottom - (bottom - TOP) * value / top
    parts = [furniture]
    for level, words in shown:
        parts.append(hline(f"{y(level):.1f}", INK, "4 4") + text("100%", f"{y(level) - 5:.1f}", words, "end", "t th"))
    for index in range(count - 1):
        if values[index] is not None and values[index + 1] is not None:
            parts.append(f'<line x1="{pct((index + .5) / count)}" x2="{pct((index + 1.5) / count)}" y1="{y(values[index]):.1f}" '
                         f'y2="{y(values[index + 1]):.1f}" stroke="{color}" stroke-width="2" stroke-linecap="round"/>')
    for index, value in enumerate(values):  # a value with no neighbour would draw nothing: a dot shows it
        alone = value is not None and (index == 0 or values[index - 1] is None) and (index == count - 1 or values[index + 1] is None)
        if alone:
            parts.append(f'<circle cx="{pct((index + .5) / count)}" cy="{y(value):.1f}" r="2.5" fill="{color}"/>')
    last = next((index for index in range(count - 1, -1, -1) if values[index] is not None), None)
    if last is not None:
        parts.append(f'<circle cx="{pct((last + .5) / count)}" cy="{y(values[last]):.1f}" r="4" fill="{color}" stroke="var(--halo)" stroke-width="2"/>')
    parts.append(release_marks(markers, bottom) if markers else "")
    parts.append(hits(count, TOP, bottom, tips))
    return frame(ident, title, desc, height, "".join(parts))


def progress(ident, title, desc, points, forecast=None, thresholds=(), axis=(), unit="", height=180):
    """A running total across a period (the month's spend): `points` are (fraction of the period, total, hover words), drawn
    as a solid line; `forecast` (fraction, total, words) continues it as a dashed line to the period's end; thresholds (the
    budget, the cap) are dashed with their words. The hover band of each point reaches halfway to its neighbours."""
    values = [total for _, total, _ in points] + ([forecast[1]] if forecast else [])
    top, _, _ = scale(values)
    levels = [level for level, _ in thresholds if level is not None]
    top = nice(max([top, *levels]))  # the budget and the cap always show
    # A threshold at the top already says the top's value: its words take the place of the axis label.
    bottom, furniture = plot_frame(height, "" if any(level >= top * 0.97 for level in levels) else labelled(top, unit), axis)

    def y(value):
        return bottom - (bottom - TOP) * value / top
    parts = [furniture]
    for level, words in thresholds:
        if level is not None:
            parts.append(hline(f"{y(level):.1f}", INK, "4 4") + text("0", f"{y(level) - 5:.1f}", words, "start", "t th"))
    for (x0, v0, _), (x1, v1, _) in zip(points, points[1:], strict=False):
        parts.append(f'<line x1="{pct(x0)}" x2="{pct(x1)}" y1="{y(v0):.1f}" y2="{y(v1):.1f}" stroke="{ACCENT}" stroke-width="2" stroke-linecap="round"/>')
    if points and forecast:
        x0, v0, _ = points[-1]
        parts.append(f'<line x1="{pct(x0)}" x2="{pct(forecast[0])}" y1="{y(v0):.1f}" y2="{y(forecast[1]):.1f}" stroke="{ACCENT}" stroke-width="2" '
                     f'stroke-dasharray="5 4" stroke-linecap="round"><title>{esc(forecast[2])}</title></line>'
                     f'<circle cx="{pct(forecast[0])}" cy="{y(forecast[1]):.1f}" r="3.5" fill="#fff" stroke="{ACCENT}" stroke-width="2"><title>{esc(forecast[2])}</title></circle>')
    if points:
        x, value, _ = points[-1]
        parts.append(f'<circle cx="{pct(x)}" cy="{y(value):.1f}" r="4" fill="{ACCENT}" stroke="var(--halo)" stroke-width="2"/>')
    edges = [x for x, _, _ in points]
    for index, (x, _, words) in enumerate(points):
        left = (edges[index - 1] + x) / 2 if index else max(x - 0.02, 0)
        right = (x + edges[index + 1]) / 2 if index + 1 < len(edges) else min(x + 0.02, 1)
        parts.append(f'<rect class="hit" x="{pct(left)}" width="{pct(max(right - left, 0.004))}" y="{TOP}" height="{bottom - TOP}"><title>{esc(words)}</title></rect>')
    return frame(ident, title, desc, height, "".join(parts))


def hbars(ident, title, desc, rows, color=ACCENT):
    """Horizontal bars, one row each: its name on the left and its amount on the right above a bar from zero. `rows` is
    [(name, value, amount words, hover words)]; a negative amount (a credit) draws no bar."""
    row_height = 38
    top = max([value for _, value, _, _ in rows if value and value > 0] or [1])
    parts = []
    for index, (name, value, words, tip) in enumerate(rows):
        y = index * row_height
        parts.append(f'<g class="row"><title>{esc(tip)}</title>' + text(0, y + 13, name, cls="t tl") + text("100%", y + 13, words, "end", "t tv")
                     + f'<rect x="0" width="100%" y="{y + 19}" height="8" rx="2" fill="{TRACK}"/>')
        if value and value > 0:
            parts.append(f'<rect x="0" width="{pct(value / top)}" y="{y + 19}" height="8" rx="2" fill="{color}"/>')
        parts.append(f'<rect x="0" width="100%" y="{y}" height="{row_height - 4}" fill="transparent"/></g>')
    return frame(ident, title, desc, max(len(rows), 1) * row_height, "".join(parts), "chart hbars")


def ranges(ident, title, desc, rows):
    """Horizontal ranges, one row each: a bar from its low to its high end and a tick at its limit, on one scale. `rows` is
    [(name, low, high, limit or None, words, hover words)]."""
    row_height = 40
    top = max([max(high or 0, limit or 0) for _, _, high, limit, _, _ in rows] or [1]) * 1.08 or 1
    parts = []
    for index, (name, low, high, limit, words, tip) in enumerate(rows):
        y = index * row_height
        parts.append(f'<g class="row"><title>{esc(tip)}</title>' + text(0, y + 13, name, cls="t tl") + text("100%", y + 13, words, "end", "t tv")
                     + f'<rect x="0" width="100%" y="{y + 20}" height="8" rx="2" fill="{TRACK}"/>')
        left, right = (low or 0) / top, (high or 0) / top
        parts.append(f'<rect x="{pct(left)}" width="{pct(max(right - left, 0.006))}" y="{y + 20}" height="8" rx="2" fill="{ACCENT}"/>')
        if limit:
            x = pct(limit / top)
            parts.append(f'<line x1="{x}" x2="{x}" y1="{y + 16}" y2="{y + 32}" stroke="{INK}" stroke-width="2"/>')
        parts.append(f'<rect x="0" width="100%" y="{y}" height="{row_height - 4}" fill="transparent"/></g>')
    return frame(ident, title, desc, max(len(rows), 1) * row_height, "".join(parts), "chart hbars")


def bullet(ident, title, desc, value, forecast=None, budget=None, cap=None, height=50, money=lambda amount: f"${amount:,.2f}"):
    """A bullet graph: the amount so far as a solid bar, its forecast as a pale bar behind it, the budget as a solid tick and
    the cap as a dashed one, each with its words below."""
    marks = [amount for amount in (value, forecast, budget, cap) if amount is not None]
    top = max(marks or [1]) * 1.06 or 1
    parts = [f'<rect x="0" width="100%" y="8" height="14" rx="3" fill="{TRACK}"/>']
    if forecast is not None and forecast > 0:
        parts.append(f'<rect x="0" width="{pct(forecast / top)}" y="8" height="14" rx="3" fill="{ACCENT}" opacity=".22">'
                     f'<title>{esc(f"On course for {money(forecast)} this month")}</title></rect>')
    if value:
        parts.append(f'<rect x="0" width="{pct(max(value, 0) / top)}" y="8" height="14" rx="3" fill="{ACCENT}"><title>{esc(f"{money(value)} so far")}</title></rect>')
    labels = []
    for amount, words, dash in ((budget, "budget", None), (cap, "cap", "3 2")):
        if amount is None:
            continue
        x = pct(amount / top)
        pattern = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(f'<line x1="{x}" x2="{x}" y1="3" y2="27" stroke="{INK}" stroke-width="2"{pattern}><title>{esc(f"{words} {money(amount)}")}</title></line>')
        labels.append((amount / top, f"{words} {money(amount)}"))
    if len(labels) == 2 and abs(labels[0][0] - labels[1][0]) < 0.3:  # too close to sit apart: one label for both
        labels = [(max(labels[0][0], labels[1][0]), f"{labels[0][1]} · {labels[1][1]}")]
    for fraction, words in labels:
        anchor = "end" if fraction > 0.75 else "start" if fraction < 0.25 else "middle"
        parts.append(text(pct(fraction), height - 6, words, anchor))
    return frame(ident, title, desc, height, "".join(parts), "chart bullet")


def meter(ident, title, desc, share, colour, height=18):
    """A share of a whole (0 to 1) as a filled track: the error budget left. A share below zero fills nothing."""
    filled = min(max(share or 0, 0), 1)
    body = f'<rect x="0" width="100%" y="2" height="12" rx="3" fill="{TRACK}"/>'
    if filled:
        body += f'<rect x="0" width="{pct(filled)}" y="2" height="12" rx="3" fill="{colour}"><title>{esc(title)}</title></rect>'
    return frame(ident, title, desc, height, body, "chart meter")


def uptime_level(passed):
    """good, warning or bad for a day's share of passed uptime checks; None when nothing was measured."""
    if passed is None:
        return None
    return "good" if passed >= 0.999 else "warning" if passed >= 0.99 else "bad"


LEVEL_COLORS = {"good": GOOD, "warning": WARN, "bad": BAD, None: EMPTY}


def uptime_strip(ident, title, desc, days, start_words, end_words, height=54):
    """One square per day, like a status page: green at 99.9 percent of checks or more, amber from 99, red below, grey with
    nothing measured. A day that was not green also gets a caret underneath, so the strip reads without colour."""
    count = max(len(days), 1)
    parts = []
    for index, day in enumerate(days):
        level = uptime_level(day.get("passed"))
        x0 = (index + .1) / count
        parts.append(f'<rect class="cell" x="{pct(x0)}" width="{pct(.8 / count)}" y="0" height="26" rx="2" fill="{LEVEL_COLORS[level]}">'
                     f'<title>{esc(day["tip"])}</title></rect>')
        if level in ("warning", "bad"):
            parts.append(f'<svg x="{pct((index + .5) / count)}" y="30" overflow="visible"><path d="M0 0l4 5h-8z" fill="{INK}"/></svg>')
    parts.append(text(0, height - 4, start_words) + text("100%", height - 4, end_words, "end"))
    return frame(ident, title, desc, height, "".join(parts), "chart strip")


SHAPES = {
    "circle": '<circle r="5"/>',
    "ring": '<circle r="4.5" fill="var(--halo)" stroke-width="2"/>',
    "diamond": '<path d="M0-6.5 6.5 0 0 6.5-6.5 0z"/>',
    "square": '<rect x="-5" y="-5" width="10" height="10" rx="1"/>',
    "triangle": '<path d="M0-6.5 6.5 5h-13z"/>',
    "cross": '<path d="M-5-5 5 5M5-5-5 5" fill="none" stroke-width="2.6" stroke-linecap="round"/>',
}


def shape(name, colour):
    inner = SHAPES[name]
    return f'<g fill="{colour}" stroke="{colour}">{inner}</g>' if name in ("ring", "cross") else f'<g fill="{colour}">{inner}</g>'


def timeline(ident, title, desc, rows, marks, axis, height_per_row=36):
    """Rows of events along time: one row per name, each event a shape at its moment. `marks` are dicts with row (index),
    at (fraction of the width), shape, colour and tip; `axis` the time labels."""
    parts = []
    for index, name in enumerate(rows):
        y = index * height_per_row
        parts.append(text(0, y + 12, name, cls="t tl") + f'<line x1="0" x2="100%" y1="{y + 25}" y2="{y + 25}" stroke="{GRID}"/>')
    for item in marks:
        y = item["row"] * height_per_row + 25
        parts.append(f'<svg x="{pct(item["at"])}" y="{y}" overflow="visible" class="mk"><title>{esc(item["tip"])}</title>'
                     f'<circle r="12" fill="transparent"/>{shape(item["shape"], item["color"])}</svg>')
    height = max(len(rows), 1) * height_per_row + AXIS_BAND
    return frame(ident, title, desc, height, "".join(parts) + time_axis(height, axis), "chart timeline")


# ---- around a chart: its legend and its numbers

KEYS = {
    "box": lambda colour: f'<rect x="2" y="1" width="10" height="10" rx="2" fill="{colour}"/>',
    "line": lambda colour: f'<line x1="1" x2="13" y1="6" y2="6" stroke="{colour}" stroke-width="2" stroke-linecap="round"/>',
    "dash": lambda colour: f'<line x1="1" x2="14" y1="6" y2="6" stroke="{colour}" stroke-width="2" stroke-dasharray="3 2"/>',
    "flag": lambda colour: f'<path d="M3 2h8l-4 6z" fill="{colour}"/>',
}


def legend(items):
    """[(words, colour, key)] as a row of keys, each with its words: the identity of a series never rests on its colour."""
    rendered = []
    for words, colour, key in items:
        mark = KEYS[key](colour) if key in KEYS else f'<g transform="translate(7 6) scale(.85)">{shape(key, colour)}</g>'
        rendered.append(f'<li><svg width="15" height="12" aria-hidden="true">{mark}</svg>{esc(words)}</li>')
    return f'<ul class="legend">{"".join(rendered)}</ul>' if rendered else ""


def numbers(headers, rows, numeric=(), caption=""):
    """The chart's numbers as a table, folded under "Show the numbers": the version that needs neither colour nor hover."""
    head = "".join(f'<th scope="col"{" class=\"r\"" if index in numeric else ""}>{esc(name)}</th>' for index, name in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td{" class=\"r num\"" if index in numeric else ""}>{esc(cell)}</td>' for index, cell in enumerate(row)) + "</tr>"
                   for row in rows)
    caption = f'<caption class="sr">{esc(caption)}</caption>' if caption else ""
    return (f'<details class="numbers"><summary>Show the numbers</summary><div class="table-scroll"><table class="list compact">{caption}'
            f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></details>")
