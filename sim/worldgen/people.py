"""Outdoor civilian meshes, materials, and model descriptions.

Canonical worlds use the fixed legacy assets below; procedural worlds give every person a
generated model (``actor_model_files``) built from a sampled ``Appearance``.
"""

from __future__ import annotations

import math
from pathlib import Path

from .appearance import BODIES, COLOURS, HAIR, TROUSERS, Appearance, garment_texture
from .png import solid_png
from .sdf import ACTOR_COLLIDE_BITMASK, collide_surface, floats, rgba

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
TEMPLATES = MODELS_DIR / "horus_human_textures" / "materials" / "textures"
FEMALE_MESH = "model://horus_human_female_a/meshes/casual_female.dae"
FEMALE_TEXTURES = "model://horus_human_female_a/materials/textures"
MALE_TEXTURES = "model://person_standing/materials/textures"
DERIVED_TEXTURES = "model://horus_human_textures/materials/textures"
# Both MakeHuman bodies face mesh -y; the models turn them to face +x, the heading that the
# scenario driver writes as the model yaw.
FACING_YAW = math.pi / 2
# (submesh, role) in each body's COLLADA visual scene; roles select the material.
BODY_SUBMESHES = {
    "male_a": (("v2_Body", "skin"), ("v2_jeans01", "trousers"), ("v2_tshirt02", "garment"),
               ("v2_mhair02", "hair"), ("v2_Low-Poly", None), ("v2_Eyebrow001", None),
               ("v2_Teeth_Base", None)),
    "female_a": (("casual_female-base", "skin"), ("casual_female-ponytail01", "hair"),
                 ("casual_female-low_poly", None), ("casual_female-eyebrow002", None),
                 ("casual_female-eyelashes01", None), ("casual_female-tongue01", None),
                 ("casual_female-female_casualsuit02", "garment"),
                 ("casual_female-female_casualsuit01", "garment"), ("casual_female-shoes05", None)),
}
# shirt_colour_attribute records the classifier's measured result, not the
# human-readable clothing label. None means the torso crop has no dominant
# red/green/blue signal accepted by HORUS's shirt-colour heuristic.
PERSON_ASSETS: dict[str, dict[str, str | None]] = {
    "person_blue": {
        "posture": "standing",
        "clothing": "blue",
        "shirt_colour_attribute": "blue",
    },
    "person_red": {
        "posture": "standing",
        "clothing": "red",
        "shirt_colour_attribute": "red",
    },
    "person_green": {
        "posture": "standing",
        "clothing": "green",
        "shirt_colour_attribute": "green",
    },
    "person_blue_kinematic": {
        "posture": "standing",
        "clothing": "blue",
        "shirt_colour_attribute": "blue",
    },
    "person_red_kinematic": {
        "posture": "standing",
        "clothing": "red",
        "shirt_colour_attribute": "red",
    },
    "person_green_kinematic": {
        "posture": "standing",
        "clothing": "green",
        "shirt_colour_attribute": "green",
    },
    "person_orange": {
        "posture": "standing",
        "clothing": "orange",
        "shirt_colour_attribute": "red",
    },
    "person_hivis": {
        "posture": "standing",
        "clothing": "hivis",
        "shirt_colour_attribute": "green",
    },
    "person_dark": {
        "posture": "standing",
        "clothing": "dark",
        "shirt_colour_attribute": "blue",
    },
    "person_sitting": {
        "posture": "sitting",
        "clothing": "blue",
        "shirt_colour_attribute": "blue",
    },
    "person_laying_kinematic": {
        "posture": "lying",
        "clothing": "orange",
        "shirt_colour_attribute": "red",
    },
    "person_collapsed_kinematic": {
        "posture": "lying",
        "clothing": "dark",
        "shirt_colour_attribute": "blue",
    },
    "person_laying_dark_kinematic": {
        "posture": "lying",
        "clothing": "dark",
        "shirt_colour_attribute": "red",
    },
}

# gz-common's ColladaLoader sets each SubMesh name from the COLLADA
# visual-scene node's `name` attribute (the loader replaces no underscores).
# These are the names from person_blue/meshes/standing.dae.
# Keeping each visual uncentred preserves the source mesh's common local
# coordinate frame and the original body pose.
STANDING_SUBMESHES = (
    "v2_Body",
    "v2_jeans01",
    "v2_tshirt02",
    "v2_mhair02",
    "v2_Low-Poly",
    "v2_Eyebrow001",
    "v2_Teeth_Base",
)
SHARED_STANDING_MESH = "model://person_blue/meshes/standing.dae"

# Procedural seated actors use the committed CC0 seated dummy (``assets.ASSETS`` key
# ``person_sitting_a``: OpenRobotics Rescue Randy Sitting, hi-vis rescue jacket). The visual
# pose/scale are the registry measurements; the actor keeps the envelope-collision convention
# (one box bounding the rendered mesh, see ``appearance.SITTING_ENVELOPE``).
SITTING_MESH = "model://horus_person_sitting_a/meshes/rescue_randy.dae"
SITTING_MESH_SCALE = 1.6438
SITTING_MESH_LIFT_M = -1.01533  # mesh base onto z=0 at scale 1
# Measured with horus.perception.attributes.classify_shirt_colour on drone-view renders of
# the generated seated actor (12 views, 3-5 m slant, -45/-70 deg pitch): the grey rescue
# coverall torso is unsaturated, so the classifier reads None in 11/12 views; the hi-vis lime
# trim read green once (0.96), so green stays a possible reading for uniqueness checks.
SITTING_SHIRT_COLOUR: str | None = None
SITTING_POSSIBLE_SHIRT_COLOURS = ("green",)
SITTING_CLOTHING = "grey rescue coverall with hi-vis trim"

# RGB values preserve the previous generator's shirt colors. Texture basenames
# deliberately include the model name because Ogre2 caches by filename.
_PERSON_VARIANTS = {
    "person_blue_kinematic": ((34, 72, 190), "blue shirt"),
    "person_red_kinematic": ((220, 24, 35), "red shirt"),
    "person_green_kinematic": ((90, 190, 35), "green shirt"),
    "person_orange": ((250, 92, 14), "orange hiker jacket"),
    "person_hivis": ((198, 238, 20), "high-visibility lime ranger jacket"),
    "person_dark": ((27, 38, 52), "dark outdoor clothing"),
}


def legacy_envelope(asset: str) -> dict[str, float]:
    """Model-frame body envelope of a fixed legacy asset (unscaled standing/lying male mesh)."""
    posture = PERSON_ASSETS[asset]["posture"]
    if posture == "sitting":
        # The canonical primitive ``person_sitting`` figure is frozen with its own envelope;
        # procedural seated actors use the measured rescue-dummy box from appearance.py.
        x0, x1, half, top = -0.22, 0.46, 0.22, 1.17
        return {"x_min_m": x0, "x_max_m": x1, "y_min_m": -half, "y_max_m": half,
                "top_z_m": top, "torso_z_m": 0.7, "height_m": 1.926}
    body = BODIES["male_a"]
    back, front = body["forward_m"]
    if posture == "lying":
        return {"x_min_m": -body["half_width_m"], "x_max_m": body["half_width_m"],
                "y_min_m": -body["height_m"] / 2, "y_max_m": body["height_m"] / 2,
                "top_z_m": round(front - back, 3), "torso_z_m": 0.25, "height_m": body["height_m"]}
    return {"x_min_m": back, "x_max_m": front, "y_min_m": -body["half_width_m"],
            "y_max_m": body["half_width_m"], "top_z_m": body["height_m"], "torso_z_m": 1.1,
            "height_m": body["height_m"]}


def _envelope_collision(envelope: dict[str, float]) -> str:
    size = (envelope["x_max_m"] - envelope["x_min_m"], envelope["y_max_m"] - envelope["y_min_m"],
            envelope["top_z_m"])
    centre = ((envelope["x_max_m"] + envelope["x_min_m"]) / 2, (envelope["y_max_m"] + envelope["y_min_m"]) / 2,
              envelope["top_z_m"] / 2)
    return _collision("body_envelope", _xyz(*centre), f"<box><size>{floats(*size)}</size></box>")


def _person_model_sdf(
    model_name: str, shirt_rgb: tuple[int, int, int], texture_model: str, *, lying: bool = False
) -> str:
    shirt_colour = rgba(shirt_rgb)
    texture_uri = f"model://{texture_model}/materials/textures/tshirt_{texture_model}.png"
    # The mesh faces its -y axis; standing figures are turned to face +x (the driven heading).
    visual_pose = "0 0.963 0.252 1.5708 0 0" if lying else f"0 0 0.02 0.04 0 {FACING_YAW:.4f}"
    visuals = []
    for index, submesh in enumerate(STANDING_SUBMESHES):
        material = ""
        if submesh == "v2_tshirt02":
            material = (
                f"<material><ambient>{shirt_colour}</ambient>"
                f"<diffuse>{shirt_colour}</diffuse><pbr><metal><metalness>0.0</metalness>"
                f"<albedo_map>{texture_uri}</albedo_map></metal></pbr></material>"
            )
        visuals.append(
            f'<visual name="mesh_{index}"><pose>{visual_pose}</pose>'
            "<geometry><mesh>"
            f"<uri>{SHARED_STANDING_MESH}</uri>"
            f"<submesh><name>{submesh}</name><center>false</center></submesh>"
            f"</mesh></geometry>{material}</visual>"
        )
    posture = "lying" if lying else "standing"
    inertial = (
        "<inertial><pose>0 0 0.22 0 0 0</pose><mass>75</mass>"
        "<inertia><ixx>8</ixx><ixy>0</ixy><ixz>0</ixz><iyy>8</iyy><iyz>0</iyz><izz>8</izz></inertia></inertial>"
        if lying else
        "<inertial><pose>0.1 0 0.95 0 0 0</pose><mass>80</mass>"
        "<inertia><ixx>25.73</ixx><ixy>0</ixy><ixz>0</ixz><iyy>24.88</iyy><iyz>0</iyz><izz>2.48</izz></inertia></inertial>"
    )
    envelope = legacy_envelope(next(name for name, record in PERSON_ASSETS.items()
                                    if record["posture"] == posture))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<sdf version="1.6"><model name="{model_name}"><static>false</static>'
        f"<link name=\"body\">{inertial}<gravity>false</gravity><kinematic>true</kinematic>"
        f"{_envelope_collision(envelope)}{''.join(visuals)}"
        "</link></model></sdf>\n"
    )


def _model_config(model_name: str, description: str, *, author: str = "HORUS") -> str:
    return (
        '<?xml version="1.0"?>\n'
        f"<model><name>{model_name}</name><version>1.0</version>"
        f'<sdf version="1.6">model.sdf</sdf><author><name>{author}</name></author>'
        f"<description>{description}</description></model>\n"
    )


def _write_mesh_person(
    model_dir: Path,
    model_name: str,
    rgb: tuple[int, int, int],
    description: str,
    texture_model: str,
    *,
    lying: bool = False,
) -> None:
    """Write one kinematic person; ``texture_model`` owns (and writes) its shirt texture."""
    model = model_dir / model_name
    model.mkdir(parents=True, exist_ok=True)
    if texture_model == model_name:
        texture_dir = model / "materials" / "textures"
        texture_dir.mkdir(parents=True, exist_ok=True)
        (texture_dir / f"tshirt_{model_name}.png").write_bytes(solid_png(rgb))
    (model / "model.sdf").write_text(
        _person_model_sdf(model_name, rgb, texture_model, lying=lying), encoding="utf-8"
    )
    (model / "model.config").write_text(
        _model_config(
            model_name,
            f"Kinematic {description} adapted from OpenRobotics Standing person v3 (CC0 1.0, "
            "Marina Kollmitz; MakeHuman); turned to face +x with a measured-envelope collision box.",
            author="Marina Kollmitz",
        ),
        encoding="utf-8",
    )


def _write_sitting_person(model_dir: Path) -> None:
    sitting = model_dir / "person_sitting"
    sitting.mkdir(parents=True, exist_ok=True)
    (sitting / "model.sdf").write_text(
        '''<?xml version="1.0"?>
<sdf version="1.6"><model name="person_sitting"><static>false</static><link name="body"><inertial><mass>75</mass><inertia><ixx>7</ixx><ixy>0</ixy><ixz>0</ixz><iyy>7</iyy><iyz>0</iyz><izz>7</izz></inertia></inertial><gravity>false</gravity><kinematic>true</kinematic>
  ''' + _envelope_collision(legacy_envelope("person_sitting")) + '''
  <visual name="torso"><pose>0 0 0.66 0 0 0</pose><geometry><box><size>0.43 0.31 0.56</size></box></geometry><material><ambient>0.08 0.18 0.42 1</ambient><diffuse>0.10 0.25 0.75 1</diffuse></material></visual>
  <visual name="head"><pose>0 0 1.05 0 0 0</pose><geometry><sphere><radius>0.12</radius></sphere></geometry><material><ambient>0.63 0.43 0.29 1</ambient><diffuse>0.72 0.52 0.37 1</diffuse></material></visual>
  <visual name="left_leg"><pose>0.18 0.13 0.30 0 1.5708 0</pose><geometry><cylinder><radius>0.09</radius><length>0.46</length></cylinder></geometry><material><ambient>0.12 0.15 0.19 1</ambient><diffuse>0.14 0.18 0.23 1</diffuse></material></visual>
  <visual name="right_leg"><pose>0.18 -0.13 0.30 0 1.5708 0</pose><geometry><cylinder><radius>0.09</radius><length>0.46</length></cylinder></geometry><material><ambient>0.12 0.15 0.19 1</ambient><diffuse>0.14 0.18 0.23 1</diffuse></material></visual>
  <visual name="left_shin"><pose>0.39 0.13 0.17 0 0 0</pose><geometry><cylinder><radius>0.07</radius><length>0.30</length></cylinder></geometry><material><ambient>0.12 0.15 0.19 1</ambient><diffuse>0.14 0.18 0.23 1</diffuse></material></visual>
  <visual name="right_shin"><pose>0.39 -0.13 0.17 0 0 0</pose><geometry><cylinder><radius>0.07</radius><length>0.30</length></cylinder></geometry><material><ambient>0.12 0.15 0.19 1</ambient><diffuse>0.14 0.18 0.23 1</diffuse></material></visual>
</link></model></sdf>
''',
        encoding="utf-8",
    )
    (sitting / "model.config").write_text(
        '''<?xml version="1.0"?>
<model><name>person_sitting</name><version>1.0</version><sdf version="1.6">model.sdf</sdf><author><name>HORUS</name></author><description>Kinematic seated pedestrian built from low-cost primitive visuals.</description></model>
''',
        encoding="utf-8",
    )


def write_person_models(model_dir: Path) -> None:
    """Write outdoor person models using the one shared standing COLLADA mesh.

    Each shirt colour gets one texture file, owned by the first model of that colour and
    referenced by the rest; file names stay unique per colour, so Ogre's by-name texture cache
    never hands one colour's texture to another.
    """
    model_dir = Path(model_dir)
    standing = [(name, rgb, label, False) for name, (rgb, label) in _PERSON_VARIANTS.items()]
    # Outdoor emergency figures share the lying mesh placement and contact geometry.
    lying = [
        ("person_laying_kinematic", (250, 92, 14), "orange missing hiker", True),
        ("person_collapsed_kinematic", (27, 38, 52), "dark-clothed collapsed person", True),
        ("person_laying_dark_kinematic", (27, 38, 52), "dark-clothed lying person", True),
    ]
    texture_owner: dict[tuple[int, int, int], str] = {}
    for model_name, rgb, label, is_lying in standing + lying:
        owner = texture_owner.setdefault(rgb, model_name)
        _write_mesh_person(model_dir, model_name, rgb, label, owner, lying=is_lying)

    _write_sitting_person(model_dir)


def _textured(texture_uri: str | None, tint: str) -> str:
    albedo = (f"<pbr><metal><metalness>0.0</metalness><roughness>0.9</roughness>"
              f"<albedo_map>{texture_uri}</albedo_map></metal></pbr>") if texture_uri else ""
    return f"<material><ambient>{tint}</ambient><diffuse>{tint}</diffuse>{albedo}</material>"


def _flat(rgb: tuple[int, int, int]) -> str:
    colour = rgba(rgb)
    return f"<material><ambient>{colour}</ambient><diffuse>{colour}</diffuse></material>"


def _collision(name: str, pose: str, geometry: str) -> str:
    return (f'<collision name="{name}"><pose>{pose}</pose><geometry>{geometry}</geometry>'
            f"{collide_surface(ACTOR_COLLIDE_BITMASK)}</collision>")


def _xyz(x: float, y: float, z: float) -> str:
    return floats(x, y, z, 0, 0, 0)


def _body_materials(asset: str, appearance: Appearance, textures: bool) -> dict[str, str | None]:
    skin_base = (f"{MALE_TEXTURES}/young_lightskinned_male_diffuse.png" if appearance.body == "male_a"
                 else f"{FEMALE_TEXTURES}/young_lightskinned_female_diffuse.png")
    hair_base = (f"{DERIVED_TEXTURES}/male_hair_neutral.png" if appearance.body == "male_a"
                 else f"{DERIVED_TEXTURES}/female_hair_neutral.png")
    garment_uri = f"model://{asset}/materials/textures/garment_{asset}.png"
    trousers = TROUSERS[appearance.trousers]
    skin = rgba(appearance.skin_multiplier, 1.0)
    return {
        "skin": _textured(skin_base, skin),
        "hair": None if appearance.hair is None else _textured(hair_base, rgba(HAIR[appearance.hair])),
        # Male trousers are a separate mesh with a neutral denim base; the female outfit atlas
        # carries the trousers inside the generated garment texture.
        "trousers": _textured(f"{DERIVED_TEXTURES}/male_denim_neutral.png" if textures else None,
                              rgba(trousers)),
        "garment": (_textured(garment_uri, "1 1 1 1") if textures
                    else _flat(appearance.top.rgb(0))),
    }


def _accessories(appearance: Appearance, height: float, width: float, *, lying: bool) -> str:
    if lying:
        return ""
    parts = []
    head_x = BODIES[appearance.body]["head_forward_m"] * width
    for index, (kind, colour_name) in enumerate(appearance.accessories):
        rgb = COLOURS[colour_name][0]
        if kind == "backpack":
            x = BODIES[appearance.body]["forward_m"][0] * width - 0.09
            geometry = f"<box><size>{floats(0.18, 0.32 * width, 0.42 * height / 1.8)}</size></box>"
            pose = floats(x, 0, height * 0.66, 0, 0, 0)
            parts.append(f'<visual name="acc_{index}_backpack"><pose>{pose}</pose><geometry>{geometry}</geometry>{_flat(rgb)}</visual>')
        elif kind == "cap":
            parts.append(f'<visual name="acc_{index}_cap"><pose>{floats(head_x, 0, height - 0.05, 0, 0, 0)}</pose>'
                         f"<geometry><cylinder><radius>{floats(0.105 * width)}</radius><length>0.09</length></cylinder></geometry>{_flat(rgb)}</visual>")
            parts.append(f'<visual name="acc_{index}_cap_peak"><pose>{floats(head_x + 0.12 * width, 0, height - 0.085, 0, 0, 0)}</pose>'
                         f"<geometry><box><size>{floats(0.1, 0.16 * width, 0.015)}</size></box></geometry>{_flat(rgb)}</visual>")
        elif kind == "sun_hat":
            parts.append(f'<visual name="acc_{index}_hat_brim"><pose>{floats(head_x, 0, height - 0.07, 0, 0, 0)}</pose>'
                         f"<geometry><cylinder><radius>{floats(0.22 * width)}</radius><length>0.015</length></cylinder></geometry>{_flat(rgb)}</visual>")
            parts.append(f'<visual name="acc_{index}_hat_crown"><pose>{floats(head_x, 0, height - 0.01, 0, 0, 0)}</pose>'
                         f"<geometry><cylinder><radius>{floats(0.11 * width)}</radius><length>0.11</length></cylinder></geometry>{_flat(rgb)}</visual>")
        else:
            raise ValueError(f"unknown accessory {kind!r}")
    return "".join(parts)


def _sitting_model(appearance: Appearance) -> str:
    """The seated actor visual: the measured ``person_sitting_a`` mesh, facing +x, scaled."""
    s, w = appearance.stature_scale, appearance.width_scale
    scale = (SITTING_MESH_SCALE * w, SITTING_MESH_SCALE * w, SITTING_MESH_SCALE * s)
    return (f'<visual name="sitting"><pose>{floats(0, 0, SITTING_MESH_LIFT_M * s, 0, 0, FACING_YAW)}</pose>'
            f"<geometry><mesh><uri>{SITTING_MESH}</uri>"
            f"<scale>{floats(*scale)}</scale></mesh></geometry></visual>")


def actor_model_files(asset: str, appearance: Appearance, posture: str, *,
                      textures: bool = True, seed: int = 0) -> dict[str, bytes | str]:
    """Files for one generated person model under ``models/<asset>/``."""
    if posture not in ("standing", "lying", "sitting"):
        raise ValueError(f"unsupported posture {posture!r}")
    base = BODIES[appearance.body]
    height = base["height_m"] * appearance.stature_scale
    s, w = appearance.stature_scale, appearance.width_scale
    envelope = appearance.envelope(posture)
    files: dict[str, bytes | str] = {}
    # One collision box bounds the rendered body (the measured mesh envelope), so the drone,
    # the geometry oracle and line-of-sight checks all see the whole figure.
    size_x = envelope["x_max_m"] - envelope["x_min_m"]
    size_y = envelope["y_max_m"] - envelope["y_min_m"]
    centre_x = (envelope["x_max_m"] + envelope["x_min_m"]) / 2
    envelope_box = _collision("body_envelope", _xyz(centre_x, 0, envelope["top_z_m"] / 2),
                              f"<box><size>{floats(size_x, size_y, envelope['top_z_m'])}</size></box>")
    if posture == "sitting":
        body = envelope_box + _sitting_model(appearance)
        mass, inertia = 75, (7, 7, 7)
        author = "Ian Chen, Cole Biesemeyer (OpenRobotics Rescue Randy Sitting, CC0 1.0)"
    else:
        lying = posture == "lying"
        materials = _body_materials(asset, appearance, textures)
        mesh = SHARED_STANDING_MESH if appearance.body == "male_a" else FEMALE_MESH
        if lying:
            # Roll the upright mesh face-down along -y; lift it by its front depth.
            visual_pose = floats(0, 0.5 * height, base["forward_m"][1] * w, math.pi / 2, 0, 0)
        else:
            visual_pose = floats(0, 0, 0.02, 0.04, 0, FACING_YAW)
        visuals = []
        for index, (submesh, role) in enumerate(BODY_SUBMESHES[appearance.body]):
            material = "" if role is None else materials[role]
            if role == "hair" and material is None:
                continue  # bald
            visuals.append(
                f'<visual name="mesh_{index}"><pose>{visual_pose}</pose><geometry><mesh>'
                f"<uri>{mesh}</uri><scale>{floats(w, w, s)}</scale>"
                f"<submesh><name>{submesh}</name><center>false</center></submesh>"
                f"</mesh></geometry>{material or ''}</visual>")
        body = envelope_box + "".join(visuals) + _accessories(appearance, height, w, lying=lying)
        mass, inertia = 80, (24.9, 25.7, 2.5)
        author = base["author"]
        if textures:
            template = TEMPLATES / ("male_tshirt_template.png" if appearance.body == "male_a"
                                    else "female_garment_template.png")
            files[f"models/{asset}/materials/textures/garment_{asset}.png"] = garment_texture(template, appearance, seed)
    files[f"models/{asset}/model.sdf"] = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<sdf version="1.9"><model name="{asset}"><static>false</static><link name="body">'
        f"<inertial><mass>{mass}</mass><inertia><ixx>{inertia[0]}</ixx><ixy>0</ixy><ixz>0</ixz>"
        f"<iyy>{inertia[1]}</iyy><iyz>0</iyz><izz>{inertia[2]}</izz></inertia></inertial>"
        f"<gravity>false</gravity><kinematic>true</kinematic>{body}</link></model></sdf>\n")
    files[f"models/{asset}/model.config"] = _model_config(
        asset, f"Generated {posture} civilian: {base['source']} mesh by {base['author']} for standing/lying "
               f"figures (person_sitting_a Rescue Randy mesh with a hi-vis rescue jacket for seated "
               f"figures), {appearance.top.style} top; kinematic, no rigged gait.",
        author=author)
    return files
