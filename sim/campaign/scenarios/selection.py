"""Select scenario: the runner's largest exact-colour rule must pick the intended person."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sim.campaign.episode import Episode
from sim.campaign.scoring import select_largest_attribute_candidate, selection_rule


def intended_target(episode: Episode) -> Mapping[str, Any] | None:
    """Urban: the unique red-clothed ambient person (else a red-attribute one); forest: the follow target."""
    if episode.kind != "urban":
        return next((person for person in episode.people if person.get("model") == episode.follow.get("model")), None)
    red = [person for person in episode.people if str(person.get("clothing", "")).lower() == "red"
           and person.get("group", "ambient") == "ambient"]
    if len(red) == 1:
        return red[0]
    # Prefer a unique red-attribute crowd target but record ambiguity explicitly.
    red = [person for person in episode.people if person.get("shirt_colour_attribute") == "red"
           and person.get("group", "ambient") == "ambient"]
    return red[0] if red else None


async def capture_intended(
    episode: Episode, person: Mapping[str, Any], label: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Observe ``person`` from a clear vantage and apply the runner's selection rule to the frame."""
    from sim.worldgen.plan import person_point, plan_vantage

    point = person_point(dict(person), episode.ground, str(person.get("posture", "standing")))
    vantage = plan_vantage(episode.world, episode.ground, episode.camera, str(person.get("model")), point,
                           episode.rng, transit_alt=episode.transit_alt, require_los=True,
                           min_clearance_m=1.5, bounds=episode.vantage_bounds_m, attempts=400)
    if vantage is None:
        return None, None, "no clear collision-safe vantage for target"
    route = await episode._navigate(vantage.eye, vantage.yaw_rad)
    if not route.get("passed"):
        return None, None, route.get("reason", "unsafe transit")
    if not await episode._wait_gt(vantage.eye, tolerance=0.5, settle_sim_s=3.0):
        return None, None, "GT position failed to settle"
    frame = await episode._observe(label)
    matching, details, camera_pose = episode._match_frame(frame)
    wanted = person.get("shirt_colour_attribute")
    # Same rule (and confidence floor) the runner's select_candidate applies.
    selected = select_largest_attribute_candidate(frame["candidate_table"], equals=wanted)
    return frame, {"selected": selected, "target_match": matching["matches"].get(str(person.get("model"))),
                   "match_details": details.get(str(person.get("model"))), "all_matches": matching["matches"],
                   "selection_rule": selection_rule("shirt_colour", wanted),
                   "camera_pose": camera_pose}, None


async def run(episode: Episode) -> dict[str, Any]:
    if not await episode._ensure_airborne():
        return episode._takeoff_failure("vehicle could not take off")
    intended = intended_target(episode)
    if intended is None:
        return {"passed": False, "reason": "manifest has no intended uniquely coloured target"}
    frame, selection, reason = await capture_intended(episode, intended, "select-vantage")
    if frame is None or selection is None:
        return {"passed": False, "reason": reason}
    selected = selection["selected"]
    correct = isinstance(selected, Mapping) and isinstance(selection["target_match"], Mapping) \
        and selected.get("id") == selection["target_match"].get("id")
    return {"passed": bool(correct), "reason": None if correct else "largest exact-colour candidate did not match intended target",
            "intended_model": intended.get("model"), "expected_attribute": intended.get("shirt_colour_attribute"),
            "selected_candidate": selected, "selected_matches_intended": correct,
            "selection_rule": selection.get("selection_rule"),
            "target_candidate": selection["target_match"], "frame_id": frame.get("frame_id"),
            "frame_timestamp": frame.get("timestamp"), "confusions": [
                {"model": person.get("model"), "clothing": person.get("clothing"),
                 "reported_attribute": person.get("shirt_colour_attribute")}
                for person in episode.people if person.get("shirt_colour_attribute") == intended.get("shirt_colour_attribute")
                and person.get("model") != intended.get("model")],
            "raw_candidates": frame.get("candidate_table"), "saved_frame": frame.get("saved_frame"),
            "camera_pose": selection["camera_pose"]}
