"""Gazebo lidar to MAVLink proximity bridge."""

from .bridge import (
    DistanceSensorReading,
    MultiSensorBridge,
    RING_BEAMS,
    RingBeam,
    RingSensorSpec,
    SensorSpec,
    laser_scan_to_distance_sensor,
    ring_scan_to_distance_sensors,
    sensor_ids,
    validate_rate_hz,
)
from .transport import subscribe_laser_scan

__all__ = [
    "MultiSensorBridge",
    "RING_BEAMS",
    "RingBeam",
    "RingSensorSpec",
    "SensorSpec",
    "DistanceSensorReading",
    "laser_scan_to_distance_sensor",
    "ring_scan_to_distance_sensors",
    "sensor_ids",
    "subscribe_laser_scan",
    "validate_rate_hz",
]
