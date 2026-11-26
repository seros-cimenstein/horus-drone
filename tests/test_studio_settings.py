from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import httpx

from horus.studio.settings import StudioSettings, StudioSettingsStore, mask_secret, test_ai_connection


def test_settings_store_persists_settings_and_0600_masked_secrets(tmp_path: Path, monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    store = StudioSettingsStore(tmp_path)
    store.save_settings(StudioSettings(ai_provider="gemini", model="gemini-2.5-flash", theme="light"))
    store.update_secrets({"GEMINI_API_KEY": "AIzaSySecretKey9876"})

    if os.name == "posix":
        mode = stat.S_IMODE(store.secrets_path.stat().st_mode)
        assert mode == 0o600

    view = store.public_view()
    serialized = json.dumps(view)
    assert "AIzaSySecretKey9876" not in serialized
    assert view["secrets_configured"]["GEMINI_API_KEY"] is True
    assert view["secrets_hints"]["GEMINI_API_KEY"] == "AIz...9876"
    assert view["ai_status"]["configured"] is True
    assert view["ai_status"]["effective_provider"] == "openai"

    cs, api_key, resolved = store.commander_settings()
    assert cs.provider == "openai"
    assert cs.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert api_key == "AIzaSySecretKey9876"
    assert resolved["model"] == "gemini-2.5-flash"
    assert mask_secret("short") == "****"


def test_auto_provider_detection_falls_back_cleanly(tmp_path: Path, monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)

    store = StudioSettingsStore(tmp_path)
    resolved = store.resolve_ai_config()
    assert resolved["configured"] is False
    assert resolved["provider"] == "unconfigured"

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-openai-123456")
    resolved_env = store.resolve_ai_config()
    assert resolved_env["configured"] is True
    assert resolved_env["provider"] == "openai"


def test_ai_connection_check_uses_provider_endpoint(tmp_path: Path) -> None:
    store = StudioSettingsStore(tmp_path)
    store.save_settings(StudioSettings(ai_provider="openai"))
    store.update_secrets({"OPENAI_API_KEY": "sk-test-key-1234"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test-key-1234"
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res = test_ai_connection(store, http_client=client)
    assert res["ok"] is True
