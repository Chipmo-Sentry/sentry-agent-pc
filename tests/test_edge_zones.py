"""Edge zone geometry (docs/29 P1c): compile + point-in-polygon."""

from __future__ import annotations

from sentry_agent_pc.edge.zones import compile_zones, point_in_poly, zones_at

_SHELF = {"type": "shelf", "points": [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]}
_EXIT = {"type": "exit", "points": [[0.8, 0.0], [1.0, 0.0], [1.0, 0.2], [0.8, 0.2]]}


def test_compile_groups_by_type() -> None:
    c = compile_zones([_SHELF, _EXIT])
    assert set(c) == {"shelf", "exit"}


def test_compile_skips_unknown_and_short() -> None:
    c = compile_zones(
        [
            {"type": "aisle", "points": [[0, 0], [1, 0], [0.5, 1]]},
            {"type": "shelf", "points": [[0.1, 0.1], [0.2, 0.2]]},  # <3 pts
            "nope",
        ]
    )
    assert c == {}


def test_compile_rejects_out_of_range_vertex_whole_polygon() -> None:
    c = compile_zones([{"type": "shelf", "points": [[0, 0], [1, 0], [1.2, 0.5], [1, 1], [0, 1]]}])
    assert c == {}  # reshaped phantom zone must NOT be produced


def test_point_in_poly() -> None:
    sq = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert point_in_poly(0.5, 0.5, sq) is True
    assert point_in_poly(1.5, 0.5, sq) is False


def test_zones_at() -> None:
    c = compile_zones([_SHELF, _EXIT])
    assert zones_at(0.25, 0.5, c) == {"shelf"}
    assert zones_at(0.9, 0.1, c) == {"exit"}
    assert zones_at(0.7, 0.7, c) == set()
