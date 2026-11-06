"""Build the GTA height map into data/maps/gta/heights/ from a HorusGta survey (map.heights_path).

The survey comes from the mod (``gta/horus_mod/README.md`` "Survey"), fetched to this machine:
    scripts/koko/gta_survey.sh fetch SURVEY_DIR
    python scripts/maps/build_gta_heights.py SURVEY_DIR [--out DIR]

``horus.maps.heights.build_height_directory`` checks every tile, requires the survey's origin to
equal ``HorusGta.ini [origin]`` (the map tiles use it too), and writes ``index.json`` and gzipped
``<x>_<y>.bin.gz`` tiles; the same survey gives the same bytes and version. data/ is gitignored and
rsync to Koko leaves it out: copy the directory to Koko's checkout by hand. The heights are measured
in Rockstar's game: local research use only, never committed or redistributed.
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from horus.maps.heights import HeightError, build_height_directory  # noqa: E402


def read_origin(ini: Path) -> tuple[float, float, float]:
    """``HorusGta.ini [origin]`` x, y, heading_deg (comments after ';' ignored)."""
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    parser.read(ini, encoding="utf-8")
    section = parser["origin"]
    return float(section["x"]), float(section["y"]), float(section.get("heading_deg", "0") or 0)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("survey", type=Path, help="survey folder (survey.json, <x>_<y>.bin, optional progress.json)")
    parser.add_argument("--out", type=Path, default=root / "data/maps/gta/heights", help="height directory")
    parser.add_argument("--ini", type=Path, default=root / "gta/horus_mod/HorusGta.ini", help="HorusGta.ini with [origin]")
    args = parser.parse_args()
    try:
        index = build_height_directory(args.survey, args.out, read_origin(args.ini))
    except HeightError as exc:
        sys.exit(f"build_gta_heights: {exc}")
    summary = {key: index[key] for key in ("version", "cell_m", "bounds", "home_ground_z", "coverage", "bytes")}
    print(json.dumps(summary, indent=1))
    print(f"wrote {len(index['tiles'])} tiles to {args.out}")


if __name__ == "__main__":
    main()
