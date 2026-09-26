from __future__ import annotations

from itertools import islice
from pathlib import Path
from xml.etree import ElementTree

from scripts.move_target import run, trajectory


def test_blue_target_is_fully_hidden_from_a_follower_for_at_least_two_seconds():
    """A follower 2 m south of the screen, at 2 m altitude, loses the whole person for 2-3 s."""
    root = Path(__file__).parents[1]
    world = ElementTree.parse(root / "sim/worlds/horus_indoor.sdf").getroot()
    screen = world.find(".//model[@name='corridor_screen']")
    assert screen is not None
    screen_x, screen_y, screen_z, *_ = map(float, screen.findtext("pose").split())
    width_x, depth_y, height_z = map(float, screen.findtext("./link/collision/geometry/box/size").split())
    near_face_y = screen_y - depth_y / 2
    viewer_x, viewer_y, viewer_z = 0.0, near_face_y - 2.0, 2.0
    person_half_width, person_top_z = 0.25, 1.75

    def hidden(x: float, y: float) -> bool:
        if y <= near_face_y + depth_y:
            return False
        fraction = (near_face_y - viewer_y) / (y - viewer_y)  # projection onto the screen's near face
        left = viewer_x + (x - person_half_width - viewer_x) * fraction
        right = viewer_x + (x + person_half_width - viewer_x) * fraction
        top = viewer_z + (person_top_z - viewer_z) * fraction
        return (
            screen_x - width_x / 2 <= left
            and right <= screen_x + width_x / 2
            and top <= screen_z + height_z / 2
        )

    rate_hz = 20
    flags = [hidden(x, y) for x, y, _ in islice(trajectory(rate_hz), 30 * rate_hz)]
    spans, run = [], 0
    for flag in flags + [False]:
        if flag:
            run += 1
        elif run:
            spans.append(run / rate_hz)
            run = 0
    assert not flags[0]
    assert len(spans) == 1
    assert 2.0 <= spans[0] <= 3.0


def test_blue_target_can_be_kinematically_moved_without_gravity():
    root = Path(__file__).parents[1]
    model = ElementTree.parse(root / "sim/models/person_blue/model.sdf").getroot()
    assert model.findtext("./model/static") == "false"
    link = model.find("./model/link[@name='body']")
    assert link is not None
    assert link.findtext("gravity") == "false"
    assert link.findtext("kinematic") == "true"


def test_route_is_paced_by_the_supplied_simulation_clock():
    # Simulation time advances at half of wall speed; one simulated second must yield 20 poses.
    sim_time = [100.0]
    sent = []

    class Stop(Exception):
        pass

    class FakeNode:
        def request(self, service, request, request_type, response_type, timeout_ms):
            if sim_time[0] >= 101.0:
                raise Stop
            sent.append((request.position.x, request.position.y))
            response = response_type()
            response.data = True
            return True, response

    class Message:
        def __init__(self):
            self.name = ""
            self.position = type("P", (), {})()
            self.orientation = type("O", (), {})()

    class Boolean:
        data = False

    def advance(seconds):
        sim_time[0] += seconds * 0.5

    try:
        run(FakeNode(), Message, Boolean, rate_hz=20.0, clock=lambda: sim_time[0], sleep=advance)
    except Stop:
        pass
    assert len(sent) == 20


def test_sim_clock_parses_world_statistics_text():
    import threading

    from scripts.move_target import _SimClock

    clock = _SimClock.__new__(_SimClock)
    clock._lock = threading.Lock()
    clock._sample = None
    clock._parse(
        "sim_time {\n  sec: 12\n  nsec: 500000000\n}\nreal_time {\n  sec: 20\n}\nreal_time_factor: 0.65\n"
    )
    assert clock._sample[0] == 12.5
    assert clock._sample[2] == 0.65
    # A statistics message without a sim_time block must not overwrite the last sample.
    clock._parse("real_time_factor: 0.9\n")
    assert clock._sample[0] == 12.5
