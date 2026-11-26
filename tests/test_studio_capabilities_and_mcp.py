from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from horus.commander.server import create_app
from horus.studio.capabilities import check_capabilities
from horus.studio.mcp_configs import generate_mcp_configs
from horus.studio.settings import StudioSettings, StudioSettingsStore
from horus.studio.supervisor import StudioSupervisor


class _FakeCommander:
    event_callback = None

    async def submit_task(self, text: str) -> str:
        return text

    async def emergency(self, action: str, session=None) -> str:
        return action

    async def reset(self) -> None:
        return None


def test_capabilities_engine_reports_all_seven_stacks(tmp_path: Path) -> None:
    store = StudioSettingsStore(tmp_path / "cfg")
    caps = check_capabilities(store, cache_dir=tmp_path / "assets", probe_external_tools=False)
    ids = [c["id"] for c in caps]
    assert ids == ["builtin_3d", "gazebo", "gta", "real_drone", "grounder", "agents", "phone_pwa"]
    for cap in caps:
        assert isinstance(cap["ready"], bool)
        assert cap["checks"]
        for chk in cap["checks"]:
            assert {"name", "ok", "detail", "fix_hint"} <= set(chk)


def test_omp_pi_and_tuning_settings_wire_into_commander_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/pi" if cmd == "pi" else None)
    store = StudioSettingsStore(tmp_path / "cfg")
    store.save_settings(
        StudioSettings(
            ai_provider="omp",
            omp_command="pi",
            omp_model="deepseek-4.1",
            omp_thinking="high",
            flight_speed_m_s=2.4,
            link_loss_rth_delay_s=45.0,
            default_gimbal_pitch_deg=-30.0,
            camera_max_fps=8.0,
            open_vocab_hz=1.0,
        )
    )
    cs, api_key, resolved = store.commander_settings()
    assert api_key is None
    assert cs.provider == "omp"
    assert cs.omp_command == "pi"
    assert cs.omp_model == "deepseek-4.1"
    assert cs.omp_thinking == "high"
    assert resolved["configured"] is True

    reloaded = store.load_settings()
    assert reloaded.flight_speed_m_s == 2.4
    assert reloaded.link_loss_rth_delay_s == 45.0
    assert reloaded.default_gimbal_pitch_deg == -30.0


def test_mcp_config_generator_redacts_token_unless_explicit() -> None:
    safe = generate_mcp_configs("http://127.0.0.1:8123/mcp", bearer_token="secret-tok-999", include_token=False)
    assert "secret-tok-999" not in str(safe)
    assert {c["id"] for c in safe["clients"]} == {"claude_code", "codex", "omp_pi", "generic"}

    with_tok = generate_mcp_configs("http://127.0.0.1:8123/mcp", bearer_token="secret-tok-999", include_token=True)
    assert "secret-tok-999" in str(with_tok)


def test_capabilities_mcp_grounder_and_phone_api_routes_and_token_scoping(tmp_path: Path) -> None:
    async def run() -> None:
        store = StudioSettingsStore(tmp_path / "cfg")
        supervisor = StudioSupervisor(settings_store=store, runs_root=tmp_path / "runs")
        supervisor.set_control_endpoint(8765, "phone-tok")
        app = create_app(_FakeCommander(), "laptop-tok", phone_token="phone-tok", studio=supervisor)

        lh = {"Authorization": "Bearer laptop-tok"}
        ph = {"Authorization": "Bearer phone-tok"}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            # Capabilities readable by both laptop & phone tokens
            assert (await client.get("/api/studio/capabilities?probe=0", headers=ph)).status_code == 200
            cap_res = await client.get("/api/studio/capabilities?probe=0", headers=lh)
            assert cap_res.status_code == 200
            assert len(cap_res.json()["capabilities"]) == 7

            # Laptop-only routes enforce 403 for phone token
            for path in ("/api/studio/mcp-configs", "/api/studio/grounder", "/api/studio/phone"):
                assert (await client.get(path, headers=ph)).status_code == 403
                assert (await client.get(path, headers=lh)).status_code == 200

            phone_data = (await client.get("/api/studio/phone", headers=lh)).json()
            assert "127.0.0.1:8765/?view=phone#token=phone-tok" in phone_data["local_url"]

        await supervisor.close()

    asyncio.run(run())
