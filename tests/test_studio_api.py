from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import time

import httpx
import pytest

from horus.commander.server import create_app
from horus.studio.link_shaper import LinkShaper
from horus.studio.proc import ProcessGroup
from horus.studio.settings import StudioSettingsStore
from horus.studio.supervisor import StudioSupervisor


class _FakeCommander:
    event_callback = None

    async def submit_task(self, text: str) -> str:
        return f"handled: {text}"

    async def emergency(self, action: str, session=None) -> str:
        return f"emergency:{action}"

    async def reset(self) -> None:
        return None

    async def rebind(self, *args, **kwargs) -> None:
        return None

    async def unbind(self) -> None:
        return None


class _FakeDisplay:
    def __init__(self) -> None:
        self.bound = None

    async def rebind(self, url: str, token: str) -> None:
        self.bound = (url, token)

    async def unbind(self) -> None:
        self.bound = None

    async def state(self):
        return {
            "vehicle": {
                "observation_age_s": 0.1,
                "pose": {"north_m": 0.0, "east_m": 0.0, "up_m": 1.2},
                "battery_pct": 98.0,
                "ground_range_m": 1.2,
                "proximity": {"front_m": 3.5},
                "pilot_override": False,
            },
            "perception": {"camera": "streaming"},
        }


class _FakeMcpResult:
    def __init__(self, payload: str) -> None:
        from types import SimpleNamespace

        self.isError = False
        self.content = [SimpleNamespace(type="text", text=payload)]


class _FakeOpener:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def __call__(self, url: str, token: str):
        opener = self

        class _Sess:
            async def call_tool(self, name: str, args=None):
                opener.calls.append((name, dict(args or {})))
                return _FakeMcpResult('{"ok": true}')

        yield _Sess()


def test_studio_api_auth_token_scoping_settings_shaper_preflight_and_manual_move(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-studio-secret-9999")
        store = StudioSettingsStore(tmp_path / "cfg")
        shaper = LinkShaper()
        opener = _FakeOpener()
        runs_dir = tmp_path / "runs"
        (runs_dir / "demo").mkdir(parents=True)
        (runs_dir / "demo" / "events.jsonl").write_text('{"kind":"ok"}\n', encoding="utf-8")

        commander = _FakeCommander()
        display = _FakeDisplay()
        supervisor = StudioSupervisor(
            settings_store=store,
            link_shaper=shaper,
            runs_root=runs_dir,
            commander=commander,
            display=display,
            session_opener=opener,
        )
        app = create_app(
            commander,
            "laptop-token-123",
            display=display,
            phone_token="phone-token-456",
            studio=supervisor,
        )

        laptop_headers = {"Authorization": "Bearer laptop-token-123"}
        phone_headers = {"Authorization": "Bearer phone-token-456"}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Unauthenticated is rejected with 401.
            assert (await client.get("/api/studio/status")).status_code == 401

            # Phone token can read status & preflight, but is forbidden (403) on host-mutating routes.
            assert (await client.get("/api/studio/status", headers=phone_headers)).status_code == 200
            assert (await client.get("/api/studio/preflight", headers=phone_headers)).status_code == 200
            assert (await client.put("/api/studio/settings", headers=phone_headers, json={"theme": "light"})).status_code == 403
            assert (await client.post("/api/studio/link-shaper", headers=phone_headers, json={"link_cut": True})).status_code == 403
            assert (await client.post("/api/studio/session/start", headers=phone_headers, json={"mode": "real_drone"})).status_code == 403

            # Laptop token updates settings & secrets; GET never returns raw secret.
            put_res = await client.put(
                "/api/studio/settings",
                headers=laptop_headers,
                json={"theme": "light", "secrets": {"OPENAI_API_KEY": "sk-updated-12345678"}},
            )
            assert put_res.status_code == 200
            assert "sk-updated-12345678" not in put_res.text
            assert put_res.json()["secrets_configured"]["OPENAI_API_KEY"] is True

            # Fault Lab link-shaper updates runtime state.
            shaper_res = await client.post(
                "/api/studio/link-shaper",
                headers=laptop_headers,
                json={"delay_ms": 500, "jitter_ms": 50, "link_cut": True},
            )
            assert shaper_res.status_code == 200
            assert shaper_res.json() == {"delay_ms": 500, "jitter_ms": 50, "link_cut": True}

            # Start remote real_drone session -> binds display + commander and allows manual_move.
            start_res = await client.post(
                "/api/studio/session/start",
                headers=laptop_headers,
                json={"mode": "real_drone", "submode": "remote", "mcp_url": "http://127.0.0.1:8000/mcp", "mcp_token": "tok"},
            )
            assert start_res.status_code == 200
            assert start_res.json()["lifecycle"] == "running"
            assert display.bound == ("http://127.0.0.1:8000/mcp", "tok")

            move_res = await client.post(
                "/api/studio/manual-move",
                headers=laptop_headers,
                json={"action": "take_off", "params": {"altitude_m": 2.0}},
            )
            assert move_res.status_code == 200
            assert [c[0] for c in opener.calls] == ["operator_release", "take_off"]

            preflight_res = await client.get("/api/studio/preflight", headers=laptop_headers)
            assert preflight_res.status_code == 200
            items = {item["id"]: item["status"] for item in preflight_res.json()["items"]}
            assert items["telemetry"] == "pass"
            assert items["camera"] == "pass"

            runs_res = await client.get("/api/studio/runs", headers=laptop_headers)
            assert runs_res.status_code == 200
            assert runs_res.json()["runs"][0]["name"] == "demo/events.jsonl"

            stop_res = await client.post("/api/studio/session/stop", headers=laptop_headers)
            assert stop_res.status_code == 200
            assert stop_res.json()["lifecycle"] == "idle"
            assert display.bound is None

        await supervisor.close()

    asyncio.run(run())


def test_process_group_stops_children_cleanly_with_no_orphans() -> None:
    pg = ProcessGroup()
    proc = pg.spawn("sleeper", [sys.executable, "-c", "import time; time.sleep(60)"])
    assert proc.pid is not None and proc.poll() is None
    pg.close()
    deadline = time.monotonic() + 3.0
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None


def test_perception_streams_without_onnx_models(tmp_path: Path, monkeypatch) -> None:
    import numpy as np

    from horus.config import PerceptionSettings
    from horus.perception.factory import create_perception_system
    from horus.perception.types import Frame

    monkeypatch.chdir(tmp_path)
    perception = create_perception_system(
        PerceptionSettings(
            source="sim://127.0.0.1:40000",
            detector_model="none",
            embedding_model="color_histogram",
            grounder_url=None,
        ),
        flight=None,  # type: ignore[arg-type]
    )
    frame = Frame(frame_id=1, timestamp=1.0, image=np.zeros((120, 160, 3), dtype=np.uint8))
    obs = perception.process_frame(frame)
    assert obs.frame_id == 1
    assert obs.candidates == ()
    latest = perception.get_frame()
    assert latest is not None and latest.frame_id == 1


def test_manual_move_unwraps_taskgroup_exception_and_uses_udpin_gimbal(tmp_path: Path) -> None:
    @asynccontextmanager
    async def _failing_opener(url: str, token: str):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("gimbal reports no attitude")])
        yield  # pragma: no cover

    async def run() -> None:
        supervisor = StudioSupervisor(
            settings_store=StudioSettingsStore(tmp_path / "cfg"),
            link_shaper=LinkShaper(),
            runs_root=tmp_path / "runs",
            commander=_FakeCommander(),
            display=_FakeDisplay(),
            session_opener=_failing_opener,
        )
        supervisor._state.mcp_url = "http://127.0.0.1:8000/mcp"
        supervisor._active_mcp_token = "tok"
        app = create_app(_FakeCommander(), "laptop-token", display=_FakeDisplay(), studio=supervisor)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            res = await client.post(
                "/api/studio/manual-move",
                headers={"Authorization": "Bearer laptop-token"},
                json={"action": "look", "params": {"pitch_deg": -15, "yaw_deg": 0}},
            )
            assert res.status_code == 503
            err = res.json()["error"]
            assert "gimbal reports no attitude" in err
            assert "unhandled errors in a TaskGroup" not in err

        await supervisor.close()

    asyncio.run(run())
