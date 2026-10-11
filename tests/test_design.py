"""raven/design.py — pure math for the Design mode generators.

No server/UI/Assistant involved here: every generator is a params -> geometry
function, verified purely on its returned dict.
"""
import math

import pytest

from raven import design


def test_golden_rectangle_sides_are_in_golden_ratio():
    el = design.golden_rectangle(width=160)
    assert el["type"] == "polygon"
    assert len(el["points"]) == 4
    width, height = el["params"]["width"], el["params"]["height"]
    assert width == 160
    assert height == pytest.approx(160 / design.PHI)


def test_golden_rectangle_is_centered_on_its_own_local_origin():
    el = design.golden_rectangle(width=160)
    xs = [p[0] for p in el["points"]]
    ys = [p[1] for p in el["points"]]
    assert min(xs) == pytest.approx(-max(xs))
    assert min(ys) == pytest.approx(-max(ys))


def test_golden_spiral_outermost_point_is_at_the_requested_size():
    el = design.golden_spiral(size=120, turns=3)
    assert el["type"] == "path"
    assert el["d"].startswith("M ")
    # the last sampled point is the outermost one (radius grows monotonically)
    last_token = el["d"].split()[-2:]
    x, y = float(last_token[0]), float(last_token[1])
    r = math.hypot(x, y)
    assert r == pytest.approx(120, rel=1e-3)


def test_regular_polygon_vertex_count_and_radius():
    el = design.regular_polygon(sides=6, radius=80)
    assert el["type"] == "polygon"
    assert len(el["points"]) == 6
    for x, y in el["points"]:
        assert math.hypot(x, y) == pytest.approx(80)


def test_regular_polygon_rejects_degenerate_side_counts():
    el = design.regular_polygon(sides=1, radius=50)
    assert len(el["points"]) == 3  # clamped to a minimum triangle


def test_star_polygon_alternates_outer_and_inner_radius():
    el = design.star_polygon(points=5, outer_radius=100, inner_radius=40)
    assert el["type"] == "star"
    assert len(el["points"]) == 10
    radii = [math.hypot(x, y) for x, y in el["points"]]
    assert radii[0::2] == [pytest.approx(100)] * 5
    assert radii[1::2] == [pytest.approx(40)] * 5


def test_radial_pattern_repeats_the_motif_and_spaces_them_evenly():
    motif = design.regular_polygon(sides=6, radius=20)
    el = design.radial_pattern(motif, repetitions=6, orbit_radius=100)
    assert el["type"] == "group"
    assert len(el["children"]) == 6
    angles = sorted(math.degrees(math.atan2(c["y"], c["x"])) % 360 for c in el["children"])
    gaps = [(angles[(i + 1) % 6] - angles[i]) % 360 for i in range(6)]
    for gap in gaps:
        assert gap == pytest.approx(60, abs=1e-6)


def test_radial_pattern_children_are_independent_copies_of_the_motif():
    motif = design.regular_polygon(sides=6, radius=20)
    el = design.radial_pattern(motif, repetitions=3)
    ids = {c["id"] for c in el["children"]}
    assert len(ids) == 3  # each child got its own id, not the motif's


def test_grid_layout_produces_rows_times_cols_children_centered_on_the_grid():
    el = design.grid_layout(rows=2, cols=3, cell_width=40, cell_height=40, gutter=10)
    assert el["type"] == "group"
    assert len(el["children"]) == 6
    xs = [c["x"] for c in el["children"]]
    assert min(xs) == pytest.approx(-max(xs))  # grid is centered on its own origin


def test_bezier_curve_needs_at_least_two_points():
    with pytest.raises(ValueError):
        design.bezier_curve([[0, 0]])


def test_bezier_curve_quadratic_uses_q_command():
    el = design.bezier_curve([[0, 0], [50, -50], [100, 0]])
    assert " Q " in el["d"]
    assert el["params"]["control_points"] == [[0, 0], [50, -50], [100, 0]]


def test_bezier_curve_cubic_uses_c_command():
    el = design.bezier_curve([[0, 0], [30, -50], [70, -50], [100, 0]])
    assert " C " in el["d"]


def test_lissajous_curve_is_closed_and_bounded_by_amplitude():
    el = design.lissajous_curve(a=3, b=2, amplitude=100, samples=100)
    assert el["type"] == "path"
    assert el["d"].endswith("Z")
    coords = [float(tok) for tok in el["d"].replace("M", "").replace("L", "").replace("Z", "").split()]
    xs, ys = coords[0::2], coords[1::2]
    assert max(abs(v) for v in xs) <= 100 + 1e-6
    assert max(abs(v) for v in ys) <= 100 + 1e-6


def test_place_element_layers_position_and_style_uniformly():
    el = design.place_element(design.regular_polygon(sides=4, radius=10), x=5, y=7, rotation=45,
                               fill="#ff0000", z=2)
    assert el["x"] == 5 and el["y"] == 7 and el["rotation"] == 45
    assert el["fill"] == "#ff0000" and el["z"] == 2
    assert el["id"].startswith("el_")
    assert el["scale"] == 1.0


def test_regenerate_reproduces_the_same_geometry_from_params():
    original = design.regular_polygon(sides=6, radius=80)
    again = design.regenerate("regular_polygon", original["params"])
    assert again["points"] == original["points"]


def test_regenerate_rejects_an_unknown_generator_name():
    with pytest.raises(ValueError):
        design.regenerate("not_a_real_generator", {})
