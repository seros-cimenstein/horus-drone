from __future__ import annotations

import math

import pytest

from scripts.landing_error import parse_pose_info
from sim.campaign.gz_topics import topic_messages


def test_landing_error_parser_uses_vehicle_and_pad_world_positions():
    message = '''
header {
  stamp { sec: 1 nsec: 2 }
}
pose {
  name: "horus_iris"
  position { x: 1.25 y: -2.0 z: 0.4 }
  orientation { w: 1 }
}
pose {
  name: "home_apriltag_pad"
  position { x: -0.5 y: 0.25 z: 0.0 }
  orientation { w: 1 }
}
'''

    error = parse_pose_info(message)

    assert error is not None
    assert error.dx_m == pytest.approx(1.75)
    assert error.dy_m == pytest.approx(-2.25)
    assert error.distance_m == pytest.approx(math.hypot(1.75, -2.25))
    assert parse_pose_info('pose { name: "horus_iris" position { x: 1 y: 2 } }') is None


def test_landing_error_parser_treats_omitted_coordinates_as_zero():
    message = '''
pose {
  name: "horus_iris"
  position { x: 0.45 y: -0.25 z: 0.3 }
}
pose {
  name: "home_apriltag_pad"
  position {
  }
}
'''

    error = parse_pose_info(message)

    assert error is not None
    assert error.pad_x_m == pytest.approx(0.0)
    assert error.pad_y_m == pytest.approx(0.0)
    assert error.dx_m == pytest.approx(0.45)
    assert error.dy_m == pytest.approx(-0.25)
    assert error.distance_m == pytest.approx(math.hypot(0.45, -0.25))


def test_landing_error_stream_frames_pose_info_samples():
    sample = '''header {
  stamp { sec: 1 nsec: 0 }
}
pose {
  name: "horus_iris"
  position { x: 2.0 y: 3.0 z: 1.0 }
}
pose {
  name: "home_apriltag_pad"
  position { x: 0.0 y: 0.0 z: 0.0 }
}
'''
    messages = [text for text, _truncated in
                topic_messages((sample + sample.replace("x: 2.0", "x: 4.0")).splitlines(True))]

    assert len(messages) == 2
    assert parse_pose_info(messages[0]).distance_m == pytest.approx(math.sqrt(13.0))
    assert parse_pose_info(messages[1]).distance_m == pytest.approx(5.0)
