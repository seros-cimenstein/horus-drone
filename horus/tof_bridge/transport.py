"""Lazy adapter for gz-transport 13 LaserScan subscriptions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def subscribe_laser_scan(topic: str, callback: Callable[[Any], None]) -> Any:
    """Subscribe to a gz.msgs10 LaserScan topic and return its live Node.

    Gazebo bindings are imported only when this function is called. Keep the
    returned node alive for as long as messages should be received.
    """
    if not topic or not topic.strip():
        raise ValueError("Gazebo LaserScan topic must not be empty")
    if not callable(callback):
        raise TypeError("LaserScan callback must be callable")
    try:
        from gz.msgs10.laserscan_pb2 import LaserScan
        from gz.transport13 import Node
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Gazebo Python bindings are unavailable; install python3-gz-transport13 "
            "and python3-gz-msgs10 in the simulation container"
        ) from exc

    try:
        node = Node()
        subscribed = node.subscribe(LaserScan, topic, callback)
    except Exception as exc:
        raise RuntimeError(f"could not subscribe to Gazebo LaserScan topic {topic!r}: {exc}") from exc
    if not subscribed:
        raise RuntimeError(
            f"Gazebo transport rejected LaserScan topic {topic!r}; verify the topic name and message type"
        )
    return node
