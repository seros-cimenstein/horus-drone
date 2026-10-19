from __future__ import annotations

import pytest

from tests.fakes import RunningServer


@pytest.fixture
def serve():
    """Start ASGI apps on loopback for the test; every server is stopped at teardown."""
    servers: list[RunningServer] = []

    def start(app):
        server = RunningServer(app)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()
