"""Server-rendered SVG price-history chart.

Follows the dataviz reference: 2px step lines (prices change in steps), hairline solid
gridlines, clean rupee ticks, end/low/high markers with a 2px surface ring, text in ink
tokens (never the series colour), a legend only for 2+ series, colours from CSS variables
(first three validated categorical slots) and a hover crosshair driven by app.js.
"""

import json
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from html import escape

from markupsafe import Markup

from shopping_agent.services.formatting import format_inr

WIDTH, HEIGHT = 640, 260
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 78, 96, 16, 34
MAX_SERIES = 3  # validated all-pairs; more series should be faceted, not recoloured


@dataclass(frozen=True)
class Series:
    label: str
    points: list[tuple[date, Decimal]]


def _nice_step(span: float, target_ticks: int = 4) -> float:
    """A clean tick step (1, 2, 2.5 or 5 × 10^n) giving about `target_ticks` intervals."""
    raw = max(span / max(target_ticks, 1), 1.0)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10.0 * magnitude


def price_chart(series: list[Series], *, title: str) -> Markup:
    series = [s for s in series if s.points][:MAX_SERIES]
    if not series:
        return Markup('<p class="muted">No price observations yet.</p>')
    all_points = [p for s in series for p in s.points]
    dates = sorted({d for d, _ in all_points})
    first, last = dates[0], dates[-1]
    span_days = max((last - first).days, 1)
    low = min(float(v) for _, v in all_points)
    high = max(float(v) for _, v in all_points)
    pad = max((high - low) * 0.15, high * 0.02, 1.0)
    step = _nice_step(high - low + 2 * pad)
    y_min = (int((low - pad) / step)) * step
    y_max = (int((high + pad) / step) + 1) * step
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM

    def x(d: date) -> float:
        return PAD_LEFT + plot_w * ((d - first).days / span_days)

    def y(v: float) -> float:
        return PAD_TOP + plot_h * (1 - (v - y_min) / (y_max - y_min))

    parts: list[str] = []
    right = WIDTH - PAD_RIGHT
    # gridlines + y ticks
    tick = y_min
    while tick <= y_max + 1e-6:
        ty = y(tick)
        parts.append(
            f'<line class="grid" x1="{PAD_LEFT}" x2="{right}" y1="{ty:.1f}" y2="{ty:.1f}"/>'
        )
        parts.append(
            f'<text class="tick" x="{PAD_LEFT - 8}" y="{ty + 4:.1f}" text-anchor="end">'
            f"{escape(format_inr(Decimal(str(round(tick)))))}</text>"
        )
        tick += step
    # x labels: first, middle, last
    for d in sorted({first, first + (last - first) / 2, last}):
        anchor = "start" if d == first else "end" if d == last else "middle"
        parts.append(
            f'<text class="tick" x="{x(d):.1f}" y="{HEIGHT - 10}" '
            f'text-anchor="{anchor}">{d:%d %b}</text>'
        )
    parts.append(
        f'<line class="baseline" x1="{PAD_LEFT}" x2="{WIDTH - PAD_RIGHT}" '
        f'y1="{PAD_TOP + plot_h}" y2="{PAD_TOP + plot_h}"/>'
    )

    data: list[dict[str, object]] = []
    for i, s in enumerate(series, start=1):
        pts = sorted(s.points)
        path = [f"M{x(pts[0][0]):.1f},{y(float(pts[0][1])):.1f}"]
        for d, v in pts[1:]:
            path.append(f"H{x(d):.1f}V{y(float(v)):.1f}")  # step-after
        parts.append(f'<path class="line s{i}" d="{"".join(path)}"/>')
        end_d, end_v = pts[-1]
        parts.append(
            f'<circle class="marker s{i}" cx="{x(end_d):.1f}" cy="{y(float(end_v)):.1f}" r="4.5"/>'
        )
        label = (
            escape(format_inr(end_v))
            if len(series) == 1
            else escape(f"{s.label[:14]} {format_inr(end_v)}")
        )
        ex, ey = x(end_d) + 9, y(float(end_v)) + 4
        parts.append(f'<text class="value" x="{ex:.1f}" y="{ey:.1f}">{label}</text>')
        data.append(
            {
                "label": s.label,
                "slot": i,
                "points": [
                    {
                        "d": d.isoformat(),
                        "v": float(v),
                        "x": round(x(d), 1),
                        "y": round(y(float(v)), 1),
                    }
                    for d, v in pts
                ],
            }
        )
    if len(series) == 1:
        pts = series[0].points
        lo_d, lo_v = min(pts, key=lambda p: (p[1], p[0]))
        hi_d, hi_v = max(pts, key=lambda p: (p[1], -p[0].toordinal()))
        if hi_v != lo_v:
            for kind, (d, v), dy in (("Low", (lo_d, lo_v), 16), ("High", (hi_d, hi_v), -9)):
                parts.append(
                    f'<circle class="marker annot" cx="{x(d):.1f}" cy="{y(float(v)):.1f}" r="4"/>'
                )
                parts.append(
                    f'<text class="annot-label" x="{x(d):.1f}" y="{y(float(v)) + dy:.1f}" '
                    f'text-anchor="middle">{kind} {escape(format_inr(v))}</text>'
                )
    parts.append(
        f'<line class="crosshair" x1="0" x2="0" y1="{PAD_TOP}" y2="{PAD_TOP + plot_h}" '
        'visibility="hidden"/>'
    )
    parts.append(
        f'<rect class="hit" x="{PAD_LEFT}" y="{PAD_TOP}" width="{plot_w}" height="{plot_h}" '
        'fill="transparent"/>'
    )
    legend = ""
    if len(series) > 1:
        legend = (
            '<ul class="legend">'
            + "".join(
                f'<li><span class="key s{i}"></span>{escape(s.label)}</li>'
                for i, s in enumerate(series, start=1)
            )
            + "</ul>"
        )
    svg = (
        f'<svg class="price-chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f"aria-label=\"{escape(title)}\" data-series='{escape(json.dumps(data))}'>"
        f"<title>{escape(title)}</title>{''.join(parts)}</svg>"
    )
    by_date = [dict(s.points) for s in series]
    rows = "".join(
        f"<tr><td>{d:%d %b %Y}</td>"
        + "".join(
            f"<td>{escape(format_inr(lookup[d]))}</td>" if d in lookup else "<td>–</td>"
            for lookup in by_date
        )
        + "</tr>"
        for d in dates
    )
    head = "".join(f"<th>{escape(s.label)}</th>" for s in series)
    table = (
        '<details class="chart-table"><summary>Show as table</summary>'
        f"<table><thead><tr><th>Date</th>{head}</tr></thead>"
        f"<tbody>{rows}</tbody></table></details>"
    )
    # Every interpolated text value above is escaped; numbers and dates are formatted by us.
    return Markup(  # noqa: S704
        f'<figure class="viz-root chart">{legend}{svg}'
        f'<div class="tooltip" hidden></div>{table}</figure>'
    )
