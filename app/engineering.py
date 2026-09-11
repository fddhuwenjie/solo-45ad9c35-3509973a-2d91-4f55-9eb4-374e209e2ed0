"""Bend engineering rules: V-opening, tonnage, minimum flange, tool match.

All forces are in kilonewtons, lengths in millimetres.  Air-bending model:

    F = C * Rm * t^2 * L / V        (newtons, Rm in MPa, lengths in mm)

with C derived from the included angle (C = 1.42 at 90 deg, growing for
narrower angles).  Sheet metal shops commonly quote the 650*Rm*t^2*L/V
formula in daN; the same constant set is used here, expressed in kN.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import Die, Material, Punch


# ---------------------------------------------------------------- geometry

def included_to_press_angle(included_deg: float,
                            springback_deg: float) -> float:
    """Air-bend overbend: the ram closes to the included angle minus
    springback allowance; the fold angle = 180 - included + springback."""
    return 180.0 - included_deg + springback_deg


def bend_allowance(radius: float, thickness: float, included_deg: float,
                   k_factor: float) -> float:
    """Neutral-axis arc length over the bend region."""
    alpha = math.radians(180.0 - included_deg)
    return alpha * (radius + k_factor * thickness)


# ---------------------------------------------------------------- v / tools

def v_opening_range(thickness: float) -> Tuple[float, float, float]:
    """Conventional air-bend V opening window: (Vmin, Vpref, Vmax) in mm."""
    vmin = 4.0 * thickness
    vpref = 6.0 * thickness if thickness <= 12.0 else 8.0 * thickness
    vmax = 10.0 * thickness if thickness <= 12.0 else 12.0 * thickness
    return vmin, vpref, vmax


def die_for_thickness(dies: List[Die], thickness: float
                      ) -> List[Tuple[float, Die]]:
    """Return (distance_from_preferred, die) pairs within the usable V
    window, sorted closest to the preferred V first (stable by id)."""
    vmin, vpref, vmax = v_opening_range(thickness)
    cand = []
    for d in sorted(dies, key=lambda x: x.id):
        if vmin - 1e-6 <= d.v_width <= vmax + 1e-6:
            cand.append((abs(d.v_width - vpref), d))
    cand.sort(key=lambda x: (x[0], x[1].id))
    return cand


def min_flange_length(thickness: float, v: float) -> float:
    """Minimum straight flange that can sit on the die shoulders and be
    reached by the backgauge: V/2 + 2t (shop rule V/2 + (1.5~2)t)."""
    return v / 2.0 + 2.0 * thickness


def die_supports_angle(die: Die, included_deg: float, tol: float = 0.5) -> bool:
    """The V angle must be no wider than the target included angle (the part
    closes on the V walls); typically the die V is a few degrees sharper."""
    return die.v_angle_deg <= included_deg + tol


def punch_fits(punch: Punch, die: Die, radius: float,
               thickness: float, included_deg: float,
               springback_deg: float) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    # tip radius vs inside radius (punch tip defines the inside radius)
    if punch.tip_radius > radius + 0.5:
        problems.append(
            f"punch tip radius {punch.tip_radius:g} > required inside "
            f"radius {radius:g}")
    if punch.tip_radius < max(0.0, radius - thickness):
        problems.append(
            f"punch tip radius {punch.tip_radius:g} much sharper than "
            f"inside radius {radius:g} (coining/cracking risk)")
    # tip angle must be sharper than the overbent included angle
    ram_included = included_deg - springback_deg
    if punch.tip_angle_deg > ram_included + 0.5:
        problems.append(
            f"punch angle {punch.tip_angle_deg:g} cannot reach ram included "
            f"angle {ram_included:g}")
    # punch nose must enter the V opening
    if punch.nose_width >= die.v_width:
        problems.append(
            f"punch nose {punch.nose_width:g} wider than V {die.v_width:g}")
    return not problems, problems


# ---------------------------------------------------------------- tonnage

def tonnage_kn(thickness: float, length_mm: float, v_width: float,
               tensile_mpa: float, included_deg: float) -> float:
    """Air-bend force.  Base factor 1.42 corresponds to 90 deg; the factor
    rises as the included angle narrows."""
    base = 1.42
    # empirical angle correction normalised at 90 deg
    bend = math.radians(180.0 - included_deg)
    angle_factor = 1.0 / max(0.45, math.sin(bend)) / (1.0 / 1.0)
    angle_factor = 1.0 + max(0.0, (bend - math.pi / 2)) * 0.55
    # F [N] = Rm * t^2 * L / V * 1.42  -> kN /1000
    return tensile_mpa * thickness ** 2 * length_mm / v_width * base * \
        angle_factor / 1000.0


def required_stroke(thickness: float, v_width: float,
                    included_deg: float, springback_deg: float) -> float:
    """Ram travel estimate: sheet is pushed below the die shoulders so the
    chord in the V bends through the required angle."""
    ram_included = math.radians(included_deg - springback_deg)
    # depth of the V chord at the ram included angle
    return v_width / 2.0 / max(0.2, math.tan(ram_included / 2.0)) + \
        thickness * 0.5


# ---------------------------------------------------------------- flange

def flange_depth_outward(bend_axis_p0, bend_axis_p1,
                         contour, flange_side: int) -> float:
    """Maximum perpendicular distance from the bend line, on the flange
    side, to the blank boundary (the flat flange width before bending)."""
    from .geometry import point_side
    best = 0.0
    for p in contour:
        s = point_side(p, bend_axis_p0, bend_axis_p1)
        if (s > 0) == (flange_side > 0):
            L = math.hypot(bend_axis_p1[0] - bend_axis_p0[0],
                           bend_axis_p1[1] - bend_axis_p0[1])
            best = max(best, abs(s) / L)
    return best
