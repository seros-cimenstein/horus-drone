"""Cross-platform asset and ArduCopter SITL bootstrapper for standalone HORUS."""

from __future__ import annotations

import ctypes.util
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import platform
import shutil
import stat
import sys
import tarfile
from typing import Any, Callable, Mapping
import zipfile

import httpx

from horus.paths import (
    default_assets_dir,
    default_config_dir,
    default_runs_dir,
    detect_project_root,
    storage_mode,
)

ASSETS_REPO = "seros-cimenstein/horus-drone"
ASSETS_RELEASE_TAG = "assets-copter-4.7.1"
ASSETS_BASE_URL = f"https://github.com/{ASSETS_REPO}/releases/download/{ASSETS_RELEASE_TAG}"

ProgressCallback = Callable[[str, int, int | None, str], None]


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """Description of one downloadable binary, archive, or model asset."""

    name: str
    filename: str
    url: str
    sha256: str | None
    kind: str  # "sitl_archive" | "model"
    binary_relpath: str | None = None
    default_parm_relpath: str | None = None
    optional: bool = False


@dataclass(frozen=True, slots=True)
class AssetStatus:
    """Resolution and verification status of one HORUS asset."""

    name: str
    available: bool
    path: Path | None
    source: str  # "env" | "repo" | "cache" | "missing"
    sha256_verified: bool
    detail: str
    optional: bool = False


def platform_key() -> str:
    """Normalized ``<os>-<arch>`` key for SITL asset lookup."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64", "x64"}:
        arch = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        arch = "aarch64"
    else:
        arch = machine
    if system == "windows":
        return f"windows-{arch}"
    if system == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def default_cache_dir() -> Path:
    """User-writable cache or project-local directory for HORUS SITL binaries and ONNX models."""
    return default_assets_dir()


def _repo_models_dir() -> Path:
    return (detect_project_root(include_package_root=False) or Path.cwd()) / "models"


def _models_dir_for_root(root: Path) -> Path:
    """Return the ONNX models directory inside ``root`` without double-nesting ``models/models``."""
    return root if root.name == "models" else root / "models"


def _is_project_models_root(root: Path) -> bool:
    project_root = detect_project_root()
    if project_root is None or root.name != "models":
        return False
    try:
        return root.resolve() == (project_root / "models").resolve()
    except OSError:
        return False


def _sitl_asset_for_platform(key: str | None = None) -> AssetSpec:
    key = key or platform_key()
    base = os.environ.get("HORUS_ASSETS_BASE_URL", ASSETS_BASE_URL).rstrip("/")
    if key == "windows-x86_64":
        return AssetSpec(
            name="sitl",
            filename="arducopter-4.7.1-windows-x86_64.zip",
            url=f"{base}/arducopter-4.7.1-windows-x86_64.zip",
            sha256=None,
            kind="sitl_archive",
            binary_relpath="ArduCopter.exe",
            default_parm_relpath="copter-default.parm",
        )
    if key == "linux-aarch64":
        return AssetSpec(
            name="sitl",
            filename="arducopter-4.7.1-linux-aarch64.tar.gz",
            url=f"{base}/arducopter-4.7.1-linux-aarch64.tar.gz",
            sha256=None,
            kind="sitl_archive",
            binary_relpath="arducopter",
            default_parm_relpath="copter-default.parm",
        )
    # Default Linux x86_64 (also used as fallback manifest target).
    return AssetSpec(
        name="sitl",
        filename="arducopter-4.7.1-linux-x86_64.tar.gz",
        url=f"{base}/arducopter-4.7.1-linux-x86_64.tar.gz",
        sha256=None,
        kind="sitl_archive",
        binary_relpath="arducopter",
        default_parm_relpath="copter-default.parm",
    )


def asset_manifest(key: str | None = None) -> tuple[AssetSpec, ...]:
    """Return the required standalone HORUS asset specifications for the host platform."""
    base = os.environ.get("HORUS_ASSETS_BASE_URL", ASSETS_BASE_URL).rstrip("/")
    return (
        _sitl_asset_for_platform(key),
        AssetSpec(
            name="yolov8n",
            filename="yolov8n.onnx",
            url=f"{base}/yolov8n.onnx",
            sha256=None,
            kind="model",
        ),
        AssetSpec(
            name="reid",
            filename="osnet_x0_25_msmt17.onnx",
            url=f"{base}/osnet_x0_25_msmt17.onnx",
            sha256=None,
            kind="model",
            optional=True,
        ),
    )


def sha256_file(path: Path) -> str:
    """Compute hex SHA-256 of ``path``."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str | None) -> bool:
    """Return True when ``expected`` is None or matches the file's SHA-256 digest."""
    if not expected:
        return True
    return sha256_file(path).lower() == expected.strip().lower()


def _safe_extract_tar(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"unsafe path in tar archive: {member.name!r}")
        tf.extractall(dest, filter="data")


def _safe_extract_zip(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest_resolved):
                raise ValueError(f"unsafe path in zip archive: {info.filename!r}")
        zf.extractall(dest)


def extract_archive_safe(archive: Path, dest: Path) -> None:
    """Extract a ``.tar.gz`` or ``.zip`` archive into ``dest`` after rejecting path traversal."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        _safe_extract_zip(archive, dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        _safe_extract_tar(archive, dest)
    else:
        raise ValueError(f"unsupported archive format: {archive.name!r}")


def _sitl_binary_name() -> str:
    return "ArduCopter.exe" if platform.system().lower() == "windows" else "arducopter"


def _resolve_sitl(cache_dir: Path, spec: AssetSpec, *, check_repo: bool = True) -> AssetStatus:
    env_bin = os.environ.get("HORUS_SITL_BIN")
    if env_bin:
        candidate = Path(env_bin).expanduser()
        if candidate.is_file():
            return AssetStatus("sitl", True, candidate, "env", True, f"HORUS_SITL_BIN={candidate}")
        return AssetStatus("sitl", False, None, "env", False, f"HORUS_SITL_BIN not found: {candidate}")

    # Check repo-local models/sitl or system PATH for developer checkouts.
    bin_name = spec.binary_relpath or _sitl_binary_name()
    if check_repo:
        repo_candidate = _repo_models_dir() / "sitl" / bin_name
        if repo_candidate.is_file():
            return AssetStatus("sitl", True, repo_candidate, "repo", True, f"repo {repo_candidate}")

    cache_candidate = cache_dir / "sitl" / bin_name
    if cache_candidate.is_file():
        src = "repo" if _is_project_models_root(cache_dir) else "cache"
        label = f"repo {cache_candidate}" if src == "repo" else f"cached {cache_candidate}"
        return AssetStatus("sitl", True, cache_candidate, src, True, label)

    path_bin = shutil.which(bin_name)
    if path_bin:
        return AssetStatus("sitl", True, Path(path_bin), "env", True, f"PATH {path_bin}")

    return AssetStatus("sitl", False, None, "missing", False, f"missing {spec.filename}")


def _resolve_model(
    cache_dir: Path,
    spec: AssetSpec,
    env_var: str,
    *,
    check_repo: bool = True,
) -> AssetStatus:
    env_path = os.environ.get(env_var)
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            ok = verify_sha256(candidate, spec.sha256)
            return AssetStatus(
                spec.name,
                ok,
                candidate if ok else None,
                "env",
                ok,
                f"{env_var}={candidate}",
                optional=spec.optional,
            )
        return AssetStatus(
            spec.name,
            False,
            None,
            "env",
            False,
            f"{env_var} missing: {candidate}",
            optional=spec.optional,
        )

    if check_repo:
        repo_candidate = _repo_models_dir() / spec.filename
        if repo_candidate.is_file():
            ok = verify_sha256(repo_candidate, spec.sha256)
            if ok:
                return AssetStatus(
                    spec.name,
                    True,
                    repo_candidate,
                    "repo",
                    True,
                    f"repo {repo_candidate}",
                    optional=spec.optional,
                )

    cache_candidate = _models_dir_for_root(cache_dir) / spec.filename
    if cache_candidate.is_file():
        src = "repo" if _is_project_models_root(cache_dir) else "cache"
        ok = verify_sha256(cache_candidate, spec.sha256)
        if ok:
            label = f"repo {cache_candidate}" if src == "repo" else f"cached {cache_candidate}"
            return AssetStatus(
                spec.name,
                True,
                cache_candidate,
                src,
                True,
                label,
                optional=spec.optional,
            )
        return AssetStatus(
            spec.name,
            False,
            None,
            src,
            False,
            f"digest mismatch at {cache_candidate}",
            optional=spec.optional,
        )

    detail = (
        f"optional — {spec.filename} not installed (using color_histogram fallback)"
        if spec.optional
        else f"missing {spec.filename}"
    )
    return AssetStatus(spec.name, False, None, "missing", False, detail, optional=spec.optional)


def assets_ready(statuses: Mapping[str, AssetStatus]) -> bool:
    """Return True when all required assets are available (optional assets may be absent)."""
    return all(st.available or st.optional for st in statuses.values())


def check_assets(cache_dir: Path | None = None) -> dict[str, AssetStatus]:
    """Check availability of SITL and ONNX model assets without downloading anything."""
    explicit = cache_dir is not None or bool((os.environ.get("HORUS_ASSETS_DIR") or "").strip())
    root = cache_dir or default_cache_dir()
    specs = {spec.name: spec for spec in asset_manifest()}
    return {
        "sitl": _resolve_sitl(root, specs["sitl"], check_repo=not explicit),
        "yolov8n": _resolve_model(root, specs["yolov8n"], "HORUS_YOLO_MODEL", check_repo=not explicit),
        "reid": _resolve_model(root, specs["reid"], "HORUS_REID_MODEL", check_repo=not explicit),
    }


def _fetch_sha256_sums(client: httpx.Client, base_url: str) -> dict[str, str]:
    sums_url = f"{base_url.rstrip('/')}/SHA256SUMS.txt"
    try:
        response = client.get(sums_url, follow_redirects=True, timeout=15.0)
        if response.status_code != 200:
            return {}
    except httpx.HTTPError:
        return {}
    digests: dict[str, str] = {}
    for line in response.text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            filename = Path(parts[1].lstrip("*")).name
            digests[filename] = parts[0].lower()
    return digests


def download_file_atomic(
    client: httpx.Client,
    url: str,
    dest: Path,
    *,
    asset_name: str,
    expected_sha256: str | None = None,
    progress_cb: ProgressCallback | None = None,
) -> Path:
    """Stream ``url`` to ``dest.part``, verify SHA-256, and atomically rename to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f".{dest.name}.part")
    bytes_done = 0
    if progress_cb is not None:
        progress_cb(asset_name, 0, None, "downloading")
    try:
        with client.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
            response.raise_for_status()
            header = response.headers.get("content-length")
            total = int(header) if header and header.isdigit() else None
            digest = hashlib.sha256()
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(65536):
                    handle.write(chunk)
                    digest.update(chunk)
                    bytes_done += len(chunk)
                    if progress_cb is not None:
                        progress_cb(asset_name, bytes_done, total, "downloading")
        actual = digest.hexdigest().lower()
        if expected_sha256 and actual != expected_sha256.strip().lower():
            raise RuntimeError(
                f"SHA-256 mismatch for {asset_name}: expected {expected_sha256}, got {actual}"
            )
        if progress_cb is not None:
            progress_cb(asset_name, bytes_done, bytes_done, "verifying")
        os.replace(part, dest)
        if progress_cb is not None:
            progress_cb(asset_name, bytes_done, bytes_done, "ready")
        return dest
    except BaseException:
        part.unlink(missing_ok=True)
        if progress_cb is not None:
            progress_cb(asset_name, bytes_done, None, "error")
        raise


def ensure_assets(
    *,
    cache_dir: Path | None = None,
    progress_cb: ProgressCallback | None = None,
    client: httpx.Client | None = None,
    include_models: bool = True,
    include_sitl: bool = True,
) -> dict[str, AssetStatus]:
    """Download and SHA-256-verify any missing HORUS standalone assets into ``cache_dir``."""
    root = cache_dir or default_cache_dir()
    statuses = check_assets(root)
    needed: list[AssetSpec] = []
    specs = {spec.name: spec for spec in asset_manifest()}
    if include_sitl and not statuses["sitl"].available:
        needed.append(specs["sitl"])
    if include_models:
        for name in ("yolov8n", "reid"):
            if not statuses[name].available:
                needed.append(specs[name])
    if not needed:
        return statuses

    owns_client = client is None
    http = client or httpx.Client()
    try:
        base_url = os.environ.get("HORUS_ASSETS_BASE_URL", ASSETS_BASE_URL)
        remote_sums = _fetch_sha256_sums(http, base_url)
        for spec in needed:
            if spec.optional and remote_sums and spec.filename not in remote_sums:
                if progress_cb is not None:
                    progress_cb(spec.name, 0, 0, "optional")
                continue
            expected_sha = spec.sha256 or remote_sums.get(spec.filename)
            if spec.kind == "sitl_archive":
                archive_path = root / "downloads" / spec.filename
                download_file_atomic(
                    http,
                    spec.url,
                    archive_path,
                    asset_name=spec.name,
                    expected_sha256=expected_sha,
                    progress_cb=progress_cb,
                )
                sitl_dir = root / "sitl"
                extract_archive_safe(archive_path, sitl_dir)
                bin_path = sitl_dir / (spec.binary_relpath or _sitl_binary_name())
                if not bin_path.is_file():
                    raise RuntimeError(f"extracted SITL archive did not contain {bin_path.name}")
                if os.name == "posix":
                    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            else:
                model_path = _models_dir_for_root(root) / spec.filename
                try:
                    download_file_atomic(
                        http,
                        spec.url,
                        model_path,
                        asset_name=spec.name,
                        expected_sha256=expected_sha,
                        progress_cb=progress_cb,
                    )
                except httpx.HTTPStatusError as exc:
                    if spec.optional and exc.response.status_code == 404:
                        if progress_cb is not None:
                            progress_cb(spec.name, 0, 0, "optional")
                        continue
                    raise
    finally:
        if owns_client:
            http.close()
    return check_assets(root)


def resolve_sitl_binary(cache_dir: Path | None = None) -> tuple[Path, Path | None]:
    """Return ``(sitl_binary_path, default_parm_path_or_none)`` or raise ``FileNotFoundError``."""
    statuses = check_assets(cache_dir)
    sitl = statuses["sitl"]
    if not sitl.available or sitl.path is None:
        raise FileNotFoundError(
            "ArduCopter SITL binary not found. Run `horus setup` or set HORUS_SITL_BIN."
        )
    parm = sitl.path.parent / "copter-default.parm"
    return sitl.path, (parm if parm.is_file() else None)


def resolve_model_paths(cache_dir: Path | None = None) -> dict[str, Path | None]:
    """Return resolved paths for ``yolov8n`` and ``reid`` ONNX models."""
    statuses = check_assets(cache_dir)
    return {
        "yolov8n": statuses["yolov8n"].path,
        "reid": statuses["reid"].path,
    }


def preferred_linux_mujoco_gl() -> str | None:
    """Return the safest offscreen MuJoCo backend present on Linux ('egl', 'osmesa', or None)."""
    if not sys.platform.startswith("linux"):
        return None
    if ctypes.util.find_library("EGL") is not None:
        return "egl"
    if ctypes.util.find_library("OSMesa") is not None:
        return "osmesa"
    return None


def _check_mujoco_gl() -> dict[str, Any]:
    gl = preferred_linux_mujoco_gl()
    if gl is not None:
        os.environ.setdefault("MUJOCO_GL", gl)
    try:
        import mujoco
    except Exception as exc:
        return {"ok": False, "detail": f"mujoco import failed: {exc}"}
    try:
        model = mujoco.MjModel.from_xml_string(
            '<mujoco><worldbody><geom type="plane" size="1 1 0.1"/><camera name="c" pos="0 -1 1"/></worldbody></mujoco>'
        )
        data = mujoco.MjData(model)
        mujoco.mj_forward(model, data)
        renderer = mujoco.Renderer(model, height=32, width=32)
        renderer.update_scene(data, camera="c")
        renderer.render()
        renderer.close()
        return {"ok": True, "detail": f"mujoco {mujoco.__version__} offscreen GL ready"}
    except Exception as exc:
        return {"ok": False, "detail": f"mujoco {mujoco.__version__} offscreen GL unavailable: {exc}"}


def doctor_report(cache_dir: Path | None = None) -> dict[str, Any]:
    """Collect structured runtime diagnostics for ``horus doctor``."""
    statuses = check_assets(cache_dir)
    gl = _check_mujoco_gl()
    serial_ports: list[str] = []
    try:
        from serial.tools import list_ports

        serial_ports = [port.device for port in list_ports.comports()]
    except Exception:
        serial_ports = []

    ai_env = [
        name
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")
        if os.environ.get(name)
    ]
    ai_clis = [cmd for cmd in ("claude", "omp") if shutil.which(cmd)]

    return {
        "python": sys.version.split()[0],
        "platform": platform_key(),
        "storage_mode": storage_mode(),
        "cache_dir": str(cache_dir or default_cache_dir()),
        "config_dir": str(default_config_dir()),
        "runs_dir": str(default_runs_dir()),
        "mujoco": gl,
        "assets": {
            name: {
                "available": st.available,
                "optional": st.optional,
                "path": str(st.path) if st.path else None,
                "source": st.source,
                "detail": st.detail,
            }
            for name, st in statuses.items()
        },
        "serial_ports": serial_ports,
        "tailscale": shutil.which("tailscale") is not None,
        "ai_credentials": {"env": ai_env, "clis": ai_clis},
    }
