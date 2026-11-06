"""The GTA height map (horus.maps.heights) on a synthetic HorusGta survey."""

from __future__ import annotations

import gzip
import json
import math
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from horus.maps.heights import NO_HIT, HeightError, HeightMap, build_height_directory, decode_survey_tile

HOME_Z = 29.69
ZERO_Z = HOME_Z + 0.121  # GTA z of HORUS up 0
CELL = 25.0  # 10 x 10 cells per tile keeps the fixture small; the survey's 5 m works the same way
N = 10
ORIGIN = (195.0, -933.0, 0.0)
ROOT = Path(__file__).resolve().parents[1]


def tile_bytes(x: int, y: int, heights: list[int], n: int = N, cell: float = CELL) -> bytes:
    """One tile file as HorusGta writes it (gta/horus_mod/README.md "Survey")."""
    hits = sum(1 for h in heights if h != NO_HIT)
    return struct.pack("<4sHHiifI", b"HSRV", 1, n, x, y, cell, hits) + struct.pack(f"<{n * n}h", *heights)


def write_survey(folder: Path, tiles: dict[tuple[int, int], list[int]], box=(-250.0, -250.0, 249.0, 499.0),
                 unloaded=(), origin=ORIGIN) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "format": "horus-gta-survey", "version": 1, "tile_m": 250, "cell_m": CELL, "cells_per_side": N,
        "box": list(box), "origin": {"x": origin[0], "y": origin[1], "heading_deg": origin[2]},
        "home_ground_z": HOME_Z, "home_ground_source": "probe", "rest_above_ground_m": 0.121,
        "ray": {"top_z": 1500, "bottom_z": -50, "flags": "Map|Objects|Foliage|Glass"},
        "started_utc": "2026-11-04T12:00:00Z",
    }
    (folder / "survey.json").write_text(json.dumps(meta), encoding="utf-8")
    for (x, y), heights in tiles.items():
        (folder / f"{x}_{y}.bin").write_bytes(tile_bytes(x, y, heights))
    (folder / "progress.json").write_text(json.dumps({"state": "done", "unloaded": [list(t) for t in unloaded]}), encoding="utf-8")
    return folder


def ground(dm: int = 297) -> list[int]:
    return [dm] * (N * N)


def cell_index(i: int, j: int) -> int:
    return j * N + i


@pytest.fixture
def survey(tmp_path: Path) -> Path:
    """Tiles (-1,-1), (0,-1), (-1,0), (0,0) and (1,0); (1,-1) unloaded in game. Tile (0,0) has a
    60.0 m tower in cell (i 2, j 3) = east 50..75, north 75..100; tile (0,-1) is sea (no hit)."""
    tower = ground()
    tower[cell_index(2, 3)] = 600
    return write_survey(tmp_path / "survey", {
        (-1, -1): ground(), (0, -1): [NO_HIT] * (N * N), (-1, 0): ground(301), (0, 0): tower, (1, 0): ground(),
    }, unloaded=[(1, -1)])


@pytest.fixture
def heights(survey: Path, tmp_path: Path) -> HeightMap:
    build_height_directory(survey, tmp_path / "heights", ORIGIN)
    return HeightMap(tmp_path / "heights")


def test_top_surface_in_horus_up(heights: HeightMap) -> None:
    # Home ground: GTA z 29.7 is 0.111 m below the resting body centre (up 0).
    assert heights.top_up_m(0.0, 0.0) == pytest.approx(29.7 - ZERO_Z)
    assert heights.top_up_m(87.0, 62.0) == pytest.approx(60.0 - ZERO_Z)  # inside the tower cell
    assert heights.top_up_m(75.0, 50.0) == pytest.approx(60.0 - ZERO_Z)  # its south-west corner belongs to it
    assert heights.top_up_m(74.99, 50.0) == pytest.approx(29.7 - ZERO_Z)
    # Negative coordinates floor into the tile to the south-west: (-0.1, -0.1) is tile (-1,-1)'s last cell.
    assert heights.top_up_m(-0.1, -0.1) == pytest.approx(29.7 - ZERO_Z)
    assert heights.top_up_m(10.0, -0.1) == pytest.approx(30.1 - ZERO_Z)  # tile (-1, 0)


def test_no_hit_is_the_sea_at_gta_z_zero(heights: HeightMap) -> None:
    assert heights.top_up_m(-100.0, 100.0) == pytest.approx(0.0 - ZERO_Z)


def test_unsurveyed_and_unloaded_cells_are_unknown(heights: HeightMap) -> None:
    assert heights.top_up_m(-100.0, 300.0) is None  # tile (1,-1): unloaded in game, no file
    assert heights.top_up_m(300.0, 0.0) is None  # outside the survey
    assert heights.max_in_disc(-5.0, 245.0, 10.0) is None  # the disc reaches into (1,-1)
    assert heights.max_along_segment((10.0, 10.0), (-100.0, 400.0)) is None


def test_disc_maximum_takes_every_cell_the_disc_reaches(heights: HeightMap) -> None:
    # Tower cell spans east 50..75, north 75..100. From (87.5, 40) its west edge is 10 m away.
    assert heights.max_in_disc(87.5, 40.0, 9.9) == pytest.approx(29.7 - ZERO_Z)
    assert heights.max_in_disc(87.5, 40.0, 10.0) == pytest.approx(60.0 - ZERO_Z)
    # Diagonal: the tower's south-west corner (75, 50) is 5·√2 from (70, 45).
    assert heights.max_in_disc(70.0, 45.0, 7.0) == pytest.approx(29.7 - ZERO_Z)
    assert heights.max_in_disc(70.0, 45.0, 7.1) == pytest.approx(60.0 - ZERO_Z)
    # Radius 0: the cell holding the point.
    assert heights.max_in_disc(80.0, 60.0, 0.0) == pytest.approx(60.0 - ZERO_Z)


def test_segment_maximum_along_a_corridor(heights: HeightMap) -> None:
    # East along north 87.5 (the tower's row) crosses it.
    assert heights.max_along_segment((87.5, 10.0), (87.5, 200.0)) == pytest.approx(60.0 - ZERO_Z)
    # North 110 passes 10 m above the tower's north edge: clear at radius 9.9, hits it at 10.
    assert heights.max_along_segment((110.0, 10.0), (110.0, 200.0), 9.9) == pytest.approx(29.7 - ZERO_Z)
    assert heights.max_along_segment((110.0, 10.0), (110.0, 200.0), 10.0) == pytest.approx(60.0 - ZERO_Z)
    # Diagonals from just inside home's cell (the corner (0, 0) touches the west tiles too): one
    # passes 2.6 m from the tower's corner (75, 75), the other runs through it.
    assert heights.max_along_segment((1.0, 1.0), (200.0, 210.0)) == pytest.approx(29.7 - ZERO_Z)
    assert heights.max_along_segment((1.0, 1.0), (200.0, 120.0)) == pytest.approx(60.0 - ZERO_Z)
    assert heights.max_along_segment((0.0, 0.0), (1.0, 1.0)) == pytest.approx(30.1 - ZERO_Z)
    # Into the sea tile and the west tile: both known, the highest wins.
    assert heights.max_along_segment((-100.0, 100.0), (10.0, -10.0)) == pytest.approx(30.1 - ZERO_Z)
    # Reversed segments give the same answer; a point segment is a disc.
    assert heights.max_along_segment((200.0, 120.0), (1.0, 1.0)) == heights.max_along_segment((1.0, 1.0), (200.0, 120.0))
    assert heights.max_along_segment((70.0, 45.0), (70.0, 45.0), 7.1) == heights.max_in_disc(70.0, 45.0, 7.1)
    with pytest.raises(ValueError):
        heights.max_along_segment((0.0, 0.0), (1.0, math.nan))
    with pytest.raises(ValueError):
        heights.max_in_disc(0.0, 0.0, -1.0)


def test_segment_maximum_agrees_with_a_sampled_scan(heights: HeightMap) -> None:
    """Random corridors against a scan of every cell, the distance taken from 400 samples of the
    segment (at most 1.4 m long each): cells surely inside must count, cells surely outside must not."""
    import random

    rng = random.Random(7)
    for _ in range(40):
        a = (rng.uniform(-200, 200), rng.uniform(-200, 240))
        b = (rng.uniform(-200, 200), rng.uniform(-200, 240))
        r = rng.choice([0.0, 2.0, 7.5, 12.0])
        samples = [(a[0] + k / 400 * (b[0] - a[0]), a[1] + k / 400 * (b[1] - a[1])) for k in range(401)]
        step = math.dist(a, b) / 400
        inside, maybe = [], []
        for row in range(math.floor((min(a[0], b[0]) - r) / CELL) - 1, math.floor((max(a[0], b[0]) + r) / CELL) + 2):
            for col in range(math.floor((min(a[1], b[1]) - r) / CELL) - 1, math.floor((max(a[1], b[1]) + r) / CELL) + 2):
                n0, e0 = row * CELL, col * CELL
                d = min(math.hypot(max(n0 - pn, 0, pn - n0 - CELL), max(e0 - pe, 0, pe - e0 - CELL)) for pn, pe in samples)
                up = heights.top_up_m(n0 + CELL / 2, e0 + CELL / 2)
                if d == 0.0 or d <= r - 1e-9:
                    inside.append(up)
                if d <= r + step:
                    maybe.append(up)
        got = heights.max_along_segment(a, b, r)
        if None in inside:
            assert got is None, (a, b, r)
        elif None not in maybe:
            assert got is not None and max(inside) - 1e-9 <= got <= max(maybe) + 1e-9, (a, b, r, got)


def test_index_records_grid_home_and_coverage(survey: Path, tmp_path: Path) -> None:
    index = build_height_directory(survey, tmp_path / "h", ORIGIN)
    assert index["cell_m"] == CELL and index["cells_per_side"] == N
    assert index["bounds"] == [-250.0, -250.0, 250.0, 500.0]  # tiles x -1..1, y -1..0
    assert index["home_ground_z"] == HOME_Z and index["rest_above_ground_m"] == 0.121
    coverage = index["coverage"]
    assert coverage["tiles"] == 5 and coverage["tiles_in_box"] == 6 and coverage["missing_in_box"] == 1
    assert coverage["unloaded"] == [[1, -1]]
    assert coverage["cells"] == 500 and coverage["no_hit_cells"] == 100 and coverage["no_hit_share"] == 0.2
    stored = json.loads((tmp_path / "h" / "index.json").read_text())
    assert stored == index and sorted(p.name for p in (tmp_path / "h").iterdir()) == [
        "-1_-1.bin.gz", "-1_0.bin.gz", "0_-1.bin.gz", "0_0.bin.gz", "1_0.bin.gz", "index.json"]
    # The tiles are the survey's bytes, gzipped.
    assert gzip.decompress((tmp_path / "h" / "0_0.bin.gz").read_bytes()) == (survey / "0_0.bin").read_bytes()


def test_version_is_stable_across_rebuilds_and_follows_the_data(survey: Path, tmp_path: Path) -> None:
    first = build_height_directory(survey, tmp_path / "a", ORIGIN)
    second = build_height_directory(survey, tmp_path / "b", ORIGIN)
    assert first["version"] == second["version"]
    for path in (tmp_path / "a").iterdir():
        assert path.read_bytes() == (tmp_path / "b" / path.name).read_bytes(), path.name
    # Rebuilding over an existing directory replaces it whole.
    (tmp_path / "a" / "stale.bin.gz").write_bytes(b"x")
    assert build_height_directory(survey, tmp_path / "a", ORIGIN)["version"] == first["version"]
    assert not (tmp_path / "a" / "stale.bin.gz").exists() and not (tmp_path / "a.new").exists()
    # One cell 0.1 m higher is another map.
    changed = ground()
    changed[5] = 298
    (survey / "-1_-1.bin").write_bytes(tile_bytes(-1, -1, changed))
    assert build_height_directory(survey, tmp_path / "c", ORIGIN)["version"] != first["version"]


def test_builder_refuses_surveys_that_do_not_fit(survey: Path, tmp_path: Path) -> None:
    with pytest.raises(HeightError, match="origin"):
        build_height_directory(survey, tmp_path / "h", (196.0, -933.0, 0.0))
    raw = (survey / "0_0.bin").read_bytes()
    (survey / "0_0.bin").write_bytes(raw[:-2])
    with pytest.raises(HeightError, match="0_0.bin: .*bytes"):
        build_height_directory(survey, tmp_path / "h", ORIGIN)
    (survey / "0_0.bin").write_bytes(raw)
    (survey / "5_5.bin").write_bytes(raw)  # tile (0,0) under another name
    with pytest.raises(HeightError, match="does not belong"):
        build_height_directory(survey, tmp_path / "h", ORIGIN)
    (survey / "5_5.bin").unlink()
    meta = json.loads((survey / "survey.json").read_text())
    (survey / "survey.json").write_text(json.dumps({**meta, "format": "other"}))
    with pytest.raises(HeightError, match="not a version 1"):
        build_height_directory(survey, tmp_path / "h", ORIGIN)
    assert not (tmp_path / "h").exists()


def test_decoder_matches_the_mods_layout() -> None:
    """The bytes HorusGta's SurveyTileFormat test pins: tile (-3, 7), 5 x 5 cells of 50 m."""
    values = [k * 10 - 50 for k in range(25)]
    values[0], values[3], values[24] = 297, NO_HIT, NO_HIT
    raw = struct.pack("<4sHHiifI", b"HSRV", 1, 5, -3, 7, 50.0, 23) + struct.pack("<25h", *values)
    assert raw[8:13] == bytes([0xFD, 0xFF, 0xFF, 0xFF, 7]) and raw[24:26] == bytes([0x29, 0x01]) and raw[30:32] == bytes([0, 0x80])
    key, n, cell, heights = decode_survey_tile(raw)
    assert key == (-3, 7) and n == 5 and cell == 50.0 and list(heights) == values
    for bad, why in ((raw[:-1], "bytes"), (b"X" + raw[1:], "HSRV"), (raw[:4] + b"\x02" + raw[5:], "version"),
                     (raw[:20] + struct.pack("<I", 24) + raw[24:], "hit count")):
        with pytest.raises(HeightError, match=why):
            decode_survey_tile(bad)


def test_build_script_checks_the_origin_against_horusgta_ini(survey: Path, tmp_path: Path) -> None:
    script = ROOT / "scripts/maps/build_gta_heights.py"
    done = subprocess.run([sys.executable, str(script), str(survey), "--out", str(tmp_path / "h")],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout[:done.stdout.rindex("}") + 1])["coverage"]["tiles"] == 5
    assert HeightMap(tmp_path / "h").top_up_m(0.0, 0.0) == pytest.approx(29.7 - ZERO_Z)
    ini = tmp_path / "HorusGta.ini"
    ini.write_text("[origin]\nx=100 ; elsewhere\ny=-933\nheading_deg=0\nz=\n", encoding="utf-8")
    done = subprocess.run([sys.executable, str(script), str(survey), "--out", str(tmp_path / "h2"), "--ini", str(ini)],
                          capture_output=True, text=True, check=False)
    assert done.returncode != 0 and "HorusGta.ini [origin]" in done.stderr and not (tmp_path / "h2").exists()
