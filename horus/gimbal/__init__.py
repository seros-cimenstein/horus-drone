"""Camera gimbal driven directly by HORUS over the MAVLink gimbal-device protocol."""

from horus.gimbal.link import Gimbal, GimbalAttitude, MavlinkGimbal

__all__ = ["Gimbal", "GimbalAttitude", "MavlinkGimbal"]
