"""Step-by-step SVG side views rendered from the same machine-coordinate
section segments used by the JSON process card and the collision checks."""
from __future__ import annotations

import math
from typing import Iterable, Sequence, Tuple

from .models import Die, PressBrake, Punch

Seg = Tuple[float, float, float, float, int]

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd",
           "#8c564b", "#e377c2", "#17becf", "#bcbd22"]


def render_step(section: Sequence[Seg], die: Die, punch: Punch,
                machine: PressBrake, *, title: str = "",
                gauge_distance: float | None = None,
                width: int = 860, height: int = 520) -> str:
    # machine obstacle outlines (mirrors machine._tool_polygons layout)
    v_half = die.v_width / 2
    floor = -v_half / math.tan(math.radians(die.v_angle_deg) / 2)
    dw = max(die.v_width * 3, die.die_height * 1.2)
    die_poly = [(-dw, -die.die_height - 30), (dw, -die.die_height - 30),
                (dw, 0.0), (v_half, 0.0), (0.0, floor),
                (-v_half, 0.0), (-dw, 0.0)]
    nose = punch.nose_width / 2
    body = max(nose, punch.body_width / 2 if punch.body_width else nose * 2)
    rh = punch.relief_height or punch.punch_height * 0.35
    ph = punch.punch_height
    punch_poly = [(-nose, 0), (-nose, rh), (-body, rh), (-body, ph),
                  (body, ph), (body, rh), (nose, rh), (nose, 0)]
    td = machine.throat_depth_mm
    th = machine.frame_clearance_height_mm
    frame_poly = [(td, -50.0), (td + 80, -50.0),
                  (td + 80, th + 100), (td, th + 100)]

    allpts: list[tuple[float, float]] = []
    for poly in (die_poly, punch_poly, frame_poly):
        allpts.extend(poly)
    for s1, z1, s2, z2, _ in section:
        allpts.extend([(s1, z1), (s2, z2)])
    if gauge_distance is not None:
        allpts.append((gauge_distance, 0))
    xs = [p[0] for p in allpts]
    ys = [p[1] for p in allpts]
    xmin, xmax = min(xs) - 20, max(xs) + 20
    ymin, ymax = min(ys) - 20, max(ys) + 40
    # keep aspect ratio
    span_x, span_y = xmax - xmin, ymax - ymin
    pad = 40
    scale = min((width - 2 * pad) / span_x, (height - 2 * pad) / span_y)

    def X(s):
        return pad + (s - xmin) * scale

    def Y(z):
        return height - pad - (z - ymin) * scale

    def pts(poly):
        return " ".join(f"{X(s):.1f},{Y(z):.1f}" for s, z in poly)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
           f'height="{height}" viewBox="0 0 {width} {height}" '
           'font-family="sans-serif">']
    if title:
        out.append(f'<text x="{pad}" y="22" font-size="15" '
                   f'font-weight="bold">{_esc(title)}</text>')
    # machine
    out.append(f'<polygon points="{pts(die_poly)}" fill="#9aa0a6" '
               'stroke="#5f6368" stroke-width="1"/>')
    out.append(f'<polygon points="{pts(punch_poly)}" fill="#c5cad1" '
               'stroke="#5f6368" stroke-width="1"/>')
    out.append(f'<polygon points="{pts(frame_poly)}" fill="#f3d2d2" '
               'stroke="#a33" stroke-width="1" '
               'stroke-dasharray="5,3"/>')
    out.append(f'<text x="{X(td+82):.1f}" y="{Y(th):.1f}" font-size="11" '
               'fill="#a33">rear frame / throat</text>')
    # V centre / gauge line
    out.append(f'<line x1="{X(0):.1f}" y1="{Y(ymin):.1f}" x2="{X(0):.1f}" '
               f'y2="{Y(ymax):.1f}" stroke="#888" stroke-dasharray="2,4"/>')
    if gauge_distance is not None:
        gx = X(gauge_distance)
        out.append(f'<line x1="{gx:.1f}" y1="{Y(-8):.1f}" x2="{gx:.1f}" '
                   f'y2="{Y(60):.1f}" stroke="#0a7" stroke-width="2"/>')
        out.append(f'<text x="{gx+3:.1f}" y="{Y(70):.1f}" font-size="11" '
                   f'fill="#0a7">backgauge {gauge_distance:.1f}</text>')
    # workpiece sections, colored per face
    for s1, z1, s2, z2, fi in section:
        color = PALETTE[fi % len(PALETTE)]
        out.append(
            f'<line x1="{X(s1):.1f}" y1="{Y(z1):.1f}" '
            f'x2="{X(s2):.1f}" y2="{Y(z2):.1f}" stroke="{color}" '
            f'stroke-width="3" stroke-linecap="round"/>')
    out.append(f'<text x="{pad}" y="{height-10}" font-size="11" '
               'fill="#444">s → rear (+) / operator (−), mm &#160;&#160; '
               'z up = ram direction</text>')
    out.append("</svg>")
    return "".join(out)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))
