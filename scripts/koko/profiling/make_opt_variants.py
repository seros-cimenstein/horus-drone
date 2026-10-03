"""RTF experiment variants of a generated p21 world (written next to it in sim/generated).

Usage: make_opt_variants.py KIND VARIANT...  where VARIANT in
  halftrees   every other tree/sapling instance removed (forest)
  civhalf     only half the ambient civilians move (rest regrouped to a static group)
  civnone     no ambient civilian moves
  notof       drone without ToF ring/down (camera only) - upper bound for non-rendered ToF
"""
import re
import shutil
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[3]
kind = sys.argv[1]
stem = f"horus_{kind}_p21"
gen = repo / "sim/generated" / stem
base_sdf = (gen / f"{stem}.sdf").read_text()
base_yaml = (gen / f"{stem}.yaml").read_text()


def write(variant: str, sdf: str, yaml: str) -> None:
    vstem = f"{stem}_{variant}"
    sdf = sdf.replace(f'<world name="{stem}">', f'<world name="{vstem}">', 1)
    yaml = re.sub(r"^world: .*$", f"world: {vstem}", yaml, count=1, flags=re.M)
    (gen / f"{vstem}.sdf").write_text(sdf)
    (gen / f"{vstem}.yaml").write_text(yaml)
    shutil.copy(gen / f"{stem}.scenery.json", gen / f"{vstem}.scenery.json")
    print("wrote", vstem)


def regroup(yaml: str, keep_every: int | None) -> str:
    """Move ambient agents to a never-launched group, keeping every n-th (None: keep none)."""
    count = 0

    def repl(match: re.Match) -> str:
        nonlocal count
        count += 1
        if keep_every is not None and count % keep_every == 0:
            return match.group(0)
        return match.group(0).replace("ambient", "static_probe")
    return re.sub(r"group: ambient", repl, yaml)


for variant in sys.argv[2:]:
    if variant == "halftrees":
        counter = {"n": 0}

        def drop(match: re.Match) -> str:
            counter["n"] += 1
            return "" if counter["n"] % 2 == 0 else match.group(0)
        sdf = re.sub(r'<visual name="tree_(?:pine|oak)_\d+">.*?</visual>', drop, base_sdf, flags=re.S)
        print("tree visuals before", counter["n"])
        write(variant, sdf, base_yaml)
    elif variant == "civhalf":
        write(variant, base_sdf, regroup(base_yaml, 2))
    elif variant == "civnone":
        write(variant, base_sdf, regroup(base_yaml, None))
    elif variant == "notof":
        models = gen / "models"
        air = (repo / "sim/models/horus_iris_with_gimbal_procedural/model.sdf").read_text()
        payload = (repo / "sim/models/horus_sensor_payload_procedural/model.sdf").read_text()
        payload = re.sub(r"<sensor\b.*?</sensor>", "", payload, flags=re.S)
        for name, text in (("opt_payload_nosensor", payload),
                           ("opt_airframe_notof", air.replace("model://horus_sensor_payload_procedural<",
                                                              "model://opt_payload_nosensor<")),
                           ("opt_iris_notof", '<?xml version="1.0"?><sdf version="1.9"><model name="opt_iris_notof">'
                            "<include><uri>model://opt_airframe_notof</uri><name>iris_with_gimbal</name></include>"
                            "</model></sdf>")):
            d = models / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "model.sdf").write_text(re.sub(r'<model name="[^"]+"', f'<model name="{name}"', text, count=1))
            (d / "model.config").write_text(f'<?xml version="1.0"?><model><name>{name}</name><version>1.0</version>'
                                            '<sdf version="1.9">model.sdf</sdf></model>')
        write(variant, base_sdf.replace("model://horus_iris_procedural<", "model://opt_iris_notof<"), base_yaml)
