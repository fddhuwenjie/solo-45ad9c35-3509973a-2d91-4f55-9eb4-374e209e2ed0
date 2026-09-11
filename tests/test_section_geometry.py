"""Regression for the double-bend U-channel cross section.

Defect: after both bends, face 0's vertices span z=0..80 but ``section``
returned only z=40..60 (interior triangulation samples), which made the SVG
side views and collision checks miss the flange.  A correct section must cover
the full flange height, and its endpoints must lie on folded face boundaries.
"""
import math

from app import geometry as G
from app.machine import n_axis
from app.partmodel import Bend, Part


def _u_channel():
    contour = [(0, 0), (100, 0), (100, 260), (0, 260)]
    b1 = Bend("b1", (0, 80), (100, 80), 90.0, 2.0, -1, fold_angle=90)
    b2 = Bend("b2", (0, 180), (100, 180), 90.0, 2.0, +1, fold_angle=90)
    part = Part.build(contour, [b1, b2], 2.0)
    part.apply_fold("b1")
    part.apply_fold("b2")
    return part


def test_section_covers_full_flange_height():
    part = _u_channel()
    segs = part.section([0, 80, 0], [1, 0, 0])
    assert segs, "section must not be empty"
    zs = sorted({round(z, 3) for s1, z1, s2, z2, _ in segs
                 for z in (z1, z2)})
    assert min(zs) <= 0.01
    assert max(zs) >= 79.99, zs


def test_section_has_three_profile_segments():
    part = _u_channel()
    segs = part.section([0, 80, 0], [1, 0, 0])
    assert len(segs) == 3, segs
    base = [s for s in segs if abs(s[1]) < 1e-6 and abs(s[3]) < 1e-6]
    legs = [s for s in segs
            if abs(s[1] - s[3]) > 50 or abs(s[0] - s[2]) < 1.0]
    assert len(base) == 1
    assert len(legs) == 2


def test_section_endpoints_lie_on_face_boundaries():
    part = _u_channel()
    q, u = [0, 80, 0], [1, 0, 0]
    nv = n_axis(u)

    def on_boundary(s, z):
        target = G.vadd(q, G.vadd(G.vmul(nv, s), [0.0, 0.0, z]))
        for f in part.faces:
            n3 = len(f.poly2d)
            for i in range(n3):
                A = f.point3d(f.poly2d[i])
                B = f.point3d(f.poly2d[(i + 1) % n3])
                ab = G.vsub(B, A)
                L2 = G.vdot(ab, ab)
                if L2 < 1e-9:
                    continue
                t = G.vdot(G.vsub(target, A), ab) / L2
                if -1e-4 <= t <= 1 + 1e-4:
                    proj = G.vadd(A, G.vmul(ab, t))
                    if G.vnorm(G.vsub(proj, target)) < 1e-3:
                        return True
        return False

    segs = part.section(q, u)
    for s1, z1, s2, z2, _ in segs:
        assert on_boundary(s1, z1), (s1, z1)
        assert on_boundary(s2, z2), (s2, z2)


def test_section_order_independent():
    contour = [(0, 0), (100, 0), (100, 260), (0, 260)]

    def folded(order):
        b1 = Bend("b1", (0, 80), (100, 80), 90.0, 2.0, -1, fold_angle=90)
        b2 = Bend("b2", (0, 180), (100, 180), 90.0, 2.0, +1, fold_angle=90)
        part = Part.build(contour, [b1, b2], 2.0)
        for bid in order:
            part.apply_fold(bid)
        return part

    a = folded(["b1", "b2"]).section([0, 80, 0], [1, 0, 0])
    b = folded(["b2", "b1"]).section([0, 80, 0], [1, 0, 0])

    def norm(segs):
        return sorted((round(s1, 2), round(z1, 2), round(s2, 2),
                       round(z2, 2), fi) for s1, z1, s2, z2, fi in segs)
    assert norm(a) == norm(b)
