"""Persistent Studio settings and write-only secret store with provider auto-detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Any, Mapping

import httpx

from horus.config import CommanderSettings
from horus.paths import default_config_dir

SECRET_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "CUSTOM_API_KEY",
    "REMOTE_MCP_TOKEN",
)

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

DEFAULT_MODELS = {
    "openai": "gpt-4.1",
    "anthropic": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.5-flash",
    "openrouter": "openai/gpt-4.1",
    "custom": "qwen2.5:14b",
    "claude-code": "sonnet",
    "omp": "default",
}


def mask_secret(value: str | None) -> str | None:
    """Return a non-recoverable masked hint for UI display, never the raw secret."""
    if not value or not value.strip():
        return None
    stripped = value.strip()
    if len(stripped) <= 8:
        return "****"
    return f"{stripped[:3]}...{stripped[-4:]}"


@dataclass(slots=True)
class StudioSettings:
    """Operator-editable non-secret HORUS Studio preferences."""

    ai_provider: str = "auto"  # auto | openai | anthropic | gemini | openrouter | custom | claude-code | omp
    model: str = ""
    base_url: str = ""
    reasoning_effort: str = ""  # "" | low | medium | high | xhigh | max
    claude_command: str = "claude"
    claude_effort: str = ""
    omp_command: str = "omp"  # users can set "omp", "pi", or a custom path
    omp_model: str = ""
    omp_thinking: str = ""
    default_scene: str = "indoor_sar"
    prefer_3d_view: bool = False
    theme: str = "dark"
    # Flight & Vision tuning applied to Studio-launched sessions
    flight_speed_m_s: float = 1.2
    vertical_speed_m_s: float = 0.5
    turn_rate_deg_s: float = 30.0
    arrival_tolerance_m: float = 0.25
    command_ttl_s: float = 30.0
    link_loss_rth_delay_s: float = 30.0
    default_gimbal_pitch_deg: float = -15.0
    camera_max_fps: float = 15.0
    open_vocab_hz: float = 0.5
    perception_render_width_px: int = 640
    perception_point_compensation: bool = True
    ai_max_tokens: int = 8000
    ai_timeout_s: float = 180.0
    ai_max_turns: int = 40
    grounder_url: str = "http://127.0.0.1:8100"
    grounder_backend: str = "auto"  # auto | cloud-vlm | grounding-dino-tiny | owlv2-base-patch16-ensemble
    recent_hardware: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StudioSettings":
        def _num(key: str, default: float, lo: float, hi: float) -> float:
            raw = data.get(key, default)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, val))

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            raw = data.get(key, default)
            try:
                val = int(round(float(raw)))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, val))

        raw_gb = str(data.get("grounder_backend", "auto") or "auto").strip().lower()
        grounder_backend = (
            raw_gb
            if raw_gb in {"auto", "cloud-vlm", "grounding-dino-tiny", "owlv2-base-patch16-ensemble"}
            else "auto"
        )

        return cls(
            ai_provider=str(data.get("ai_provider", "auto") or "auto"),
            model=str(data.get("model", "") or ""),
            base_url=str(data.get("base_url", "") or ""),
            reasoning_effort=str(data.get("reasoning_effort", "") or ""),
            claude_command=str(data.get("claude_command", "claude") or "claude").strip() or "claude",
            claude_effort=str(data.get("claude_effort", "") or ""),
            omp_command=str(data.get("omp_command", "omp") or "omp").strip() or "omp",
            omp_model=str(data.get("omp_model", "") or ""),
            omp_thinking=str(data.get("omp_thinking", "") or ""),
            default_scene=str(data.get("default_scene", "indoor_sar") or "indoor_sar"),
            prefer_3d_view=bool(data.get("prefer_3d_view", False)),
            theme="light" if data.get("theme") == "light" else "dark",
            flight_speed_m_s=_num("flight_speed_m_s", 1.2, 0.3, 12.0),
            vertical_speed_m_s=_num("vertical_speed_m_s", 0.5, 0.2, 3.0),
            turn_rate_deg_s=_num("turn_rate_deg_s", 30.0, 10.0, 120.0),
            arrival_tolerance_m=_num("arrival_tolerance_m", 0.25, 0.1, 2.0),
            command_ttl_s=_num("command_ttl_s", 30.0, 5.0, 120.0),
            link_loss_rth_delay_s=_num("link_loss_rth_delay_s", 30.0, 3.0, 300.0),
            default_gimbal_pitch_deg=_num("default_gimbal_pitch_deg", -15.0, -90.0, 45.0),
            camera_max_fps=_num("camera_max_fps", 15.0, 1.0, 30.0),
            open_vocab_hz=_num("open_vocab_hz", 0.5, 0.1, 5.0),
            perception_render_width_px=_int("perception_render_width_px", 640, 160, 640),
            perception_point_compensation=bool(data.get("perception_point_compensation", True)),
            ai_max_tokens=_int("ai_max_tokens", 8000, 1000, 32000),
            ai_timeout_s=_num("ai_timeout_s", 180.0, 30.0, 600.0),
            ai_max_turns=_int("ai_max_turns", 40, 5, 100),
            grounder_url=str(data.get("grounder_url", "http://127.0.0.1:8100") or "").strip(),
            grounder_backend=grounder_backend,
            recent_hardware=list(data.get("recent_hardware") or [])[:8],
        )


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if secret and os.name == "posix":
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    if secret and os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class StudioSettingsStore:
    """Loads/saves ``settings.json`` and write-only ``secrets.json`` (0600)."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = Path(config_dir) if config_dir is not None else default_config_dir()
        self.settings_path = self.config_dir / "settings.json"
        self.secrets_path = self.config_dir / "secrets.json"

    def load_settings(self) -> StudioSettings:
        if not self.settings_path.is_file():
            return StudioSettings()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return StudioSettings()
        if not isinstance(raw, Mapping):
            return StudioSettings()
        return StudioSettings.from_dict(raw)

    def save_settings(self, settings: StudioSettings) -> StudioSettings:
        _atomic_write_json(self.settings_path, asdict(settings), secret=False)
        return settings

    def load_secrets(self) -> dict[str, str]:
        if not self.secrets_path.is_file():
            return {}
        try:
            raw = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, Mapping):
            return {}
        return {
            key: str(val).strip()
            for key, val in raw.items()
            if key in SECRET_KEYS and isinstance(val, str) and val.strip()
        }

    def update_secrets(self, updates: Mapping[str, str | None]) -> None:
        current = self.load_secrets()
        for key, value in updates.items():
            if key not in SECRET_KEYS:
                continue
            if value is None or not str(value).strip():
                current.pop(key, None)
            else:
                current[key] = str(value).strip()
        _atomic_write_json(self.secrets_path, current, secret=True)

    def get_secret(self, key: str) -> str | None:
        stored = self.load_secrets().get(key)
        if stored:
            return stored
        env_val = os.environ.get(key)
        return env_val.strip() if env_val and env_val.strip() else None

    def resolve_ai_config(self) -> dict[str, Any]:
        """Resolve effective AI provider, model, base_url, api_key, and configuration status."""
        settings = self.load_settings()
        provider = settings.ai_provider.strip().lower() or "auto"

        if provider == "auto":
            if self.get_secret("OPENAI_API_KEY"):
                provider = "openai"
            elif self.get_secret("ANTHROPIC_API_KEY"):
                provider = "anthropic"
            elif self.get_secret("GEMINI_API_KEY"):
                provider = "gemini"
            elif self.get_secret("OPENROUTER_API_KEY"):
                provider = "openrouter"
            elif shutil.which(settings.claude_command or "claude"):
                provider = "claude-code"
            elif shutil.which(settings.omp_command or "omp") or shutil.which("pi"):
                provider = "omp"
            else:
                return {
                    "configured": False,
                    "provider": "unconfigured",
                    "effective_provider": "claude-code",
                    "model": "",
                    "base_url": None,
                    "api_key": None,
                    "reasoning_effort": settings.reasoning_effort or None,
                    "detail": "No AI key or CLI detected yet. Add one in Settings or fly manually.",
                }

        model = settings.model.strip() or DEFAULT_MODELS.get(provider, "")
        effort = settings.reasoning_effort.strip() or None

        if provider == "openai":
            key = self.get_secret("OPENAI_API_KEY")
            return {
                "configured": bool(key),
                "provider": "openai",
                "effective_provider": "openai",
                "model": model,
                "base_url": settings.base_url.strip() or None,
                "api_key": key,
                "reasoning_effort": effort,
                "detail": "OpenAI API ready" if key else "OPENAI_API_KEY missing",
            }
        if provider == "anthropic":
            key = self.get_secret("ANTHROPIC_API_KEY")
            return {
                "configured": bool(key),
                "provider": "anthropic",
                "effective_provider": "anthropic",
                "model": model,
                "base_url": settings.base_url.strip() or None,
                "api_key": key,
                "reasoning_effort": None,
                "detail": "Anthropic API ready" if key else "ANTHROPIC_API_KEY missing",
            }
        if provider == "gemini":
            key = self.get_secret("GEMINI_API_KEY")
            return {
                "configured": bool(key),
                "provider": "gemini",
                "effective_provider": "openai",
                "model": model,
                "base_url": settings.base_url.strip() or GEMINI_OPENAI_BASE_URL,
                "api_key": key,
                "reasoning_effort": effort,
                "detail": "Google Gemini API ready" if key else "GEMINI_API_KEY missing",
            }
        if provider == "openrouter":
            key = self.get_secret("OPENROUTER_API_KEY")
            return {
                "configured": bool(key),
                "provider": "openrouter",
                "effective_provider": "openai",
                "model": model,
                "base_url": settings.base_url.strip() or OPENROUTER_BASE_URL,
                "api_key": key,
                "reasoning_effort": effort,
                "detail": "OpenRouter API ready" if key else "OPENROUTER_API_KEY missing",
            }
        if provider == "custom":
            base_url = settings.base_url.strip() or "http://127.0.0.1:11434/v1"
            key = self.get_secret("CUSTOM_API_KEY") or "local"
            return {
                "configured": True,
                "provider": "custom",
                "effective_provider": "openai",
                "model": model,
                "base_url": base_url,
                "api_key": key,
                "reasoning_effort": effort,
                "detail": f"Custom OpenAI endpoint ({base_url})",
            }
        if provider == "claude-code":
            cmd = settings.claude_command or "claude"
            found = shutil.which(cmd) is not None
            return {
                "configured": found,
                "provider": "claude-code",
                "effective_provider": "claude-code",
                "model": model,
                "base_url": None,
                "api_key": None,
                "reasoning_effort": settings.claude_effort or effort,
                "detail": f"Claude Code CLI ready ({cmd})" if found else f"`{cmd}` CLI not found on PATH",
            }
        if provider == "omp":
            cmd = settings.omp_command or "omp"
            found = shutil.which(cmd) is not None
            return {
                "configured": found,
                "provider": "omp",
                "effective_provider": "omp",
                "model": settings.omp_model or model,
                "base_url": None,
                "api_key": None,
                "reasoning_effort": settings.omp_thinking or effort,
                "detail": f"OMP/Pi CLI ready ({cmd})" if found else f"`{cmd}` CLI not found on PATH",
            }
        raise ValueError(f"unsupported ai_provider {provider!r}")

    def commander_settings(self) -> tuple[CommanderSettings, str | None, dict[str, Any]]:
        """Build a ``CommanderSettings`` and API key from current Studio settings."""
        settings = self.load_settings()
        resolved = self.resolve_ai_config()
        eff = resolved["effective_provider"]
        effort = resolved["reasoning_effort"]
        model = resolved["model"] or "claude-3-5-sonnet-latest"
        if eff == "claude-code":
            c_model = model if model not in {"default", ""} else None
            c_effort = settings.claude_effort or effort
            cs = CommanderSettings(
                provider="claude-code",
                claude_command=settings.claude_command or "claude",
                claude_model=c_model,
                claude_effort=c_effort if c_effort in {"low", "medium", "high", "xhigh", "max"} else None,
                max_tokens=settings.ai_max_tokens,
                timeout_s=settings.ai_timeout_s,
                max_turns=settings.ai_max_turns,
            )
            return cs, None, resolved
        if eff == "omp":
            o_model = settings.omp_model or (model if model not in {"default", ""} else None)
            o_think = settings.omp_thinking or effort
            cs = CommanderSettings(
                provider="omp",
                omp_command=settings.omp_command or "omp",
                omp_model=o_model,
                omp_thinking=o_think if o_think in {"off", "minimal", "low", "medium", "high", "xhigh", "max"} else None,
                max_tokens=settings.ai_max_tokens,
                timeout_s=settings.ai_timeout_s,
                max_turns=settings.ai_max_turns,
            )
            return cs, None, resolved
        cs = CommanderSettings(
            provider=eff,
            model=model,
            base_url=resolved["base_url"],
            reasoning_effort=effort if eff == "openai" else None,
            max_tokens=settings.ai_max_tokens,
            timeout_s=settings.ai_timeout_s,
            max_turns=settings.ai_max_turns,
        )
        return cs, resolved["api_key"], resolved

    def public_view(self) -> dict[str, Any]:
        """Return settings + masked secret hints (never raw keys) for HTTP responses."""
        settings = self.load_settings()
        secrets = self.load_secrets()
        resolved = self.resolve_ai_config()
        configured_flags = {}
        hints = {}
        for key in SECRET_KEYS:
            val = secrets.get(key) or os.environ.get(key) or ""
            val = val.strip()
            configured_flags[key] = bool(val)
            hints[key] = mask_secret(val)
        return {
            "settings": asdict(settings),
            "secrets_configured": configured_flags,
            "secrets_hints": hints,
            "ai_status": {
                "configured": resolved["configured"],
                "provider": resolved["provider"],
                "effective_provider": resolved["effective_provider"],
                "model": resolved["model"],
                "detail": resolved["detail"],
            },
        }


def test_ai_connection(
    store: StudioSettingsStore,
    *,
    http_client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Verify the currently configured AI provider credentials or CLI availability."""
    resolved = store.resolve_ai_config()
    if not resolved["configured"]:
        return {"ok": False, "provider": resolved["provider"], "detail": resolved["detail"]}
    provider = resolved["provider"]
    if provider in {"claude-code", "omp"}:
        return {"ok": True, "provider": provider, "detail": resolved["detail"]}

    api_key = resolved["api_key"] or ""
    base_url = (resolved["base_url"] or (
        "https://api.anthropic.com" if provider == "anthropic" else "https://api.openai.com/v1"
    )).rstrip("/")

    owns = http_client is None
    client = http_client or httpx.Client(timeout=10.0)
    try:
        if provider == "anthropic":
            resp = client.get(
                f"{base_url}/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
        else:
            resp = client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if 200 <= resp.status_code < 300:
            return {"ok": True, "provider": provider, "detail": f"Connected ({resp.status_code})"}
        return {"ok": False, "provider": provider, "detail": f"HTTP {resp.status_code} from {base_url}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "provider": provider, "detail": f"Connection error: {exc}"}
    finally:
        if owns:
            client.close()


test_ai_connection.__test__ = False  # type: ignore[attr-defined]
