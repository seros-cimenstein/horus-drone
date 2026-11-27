"""Hybrid filesystem path resolution for HORUS (project checkout vs. installed package)."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir

APP_NAME = "horus"
APP_AUTHOR = "horus-drone"


def is_horus_project_root(path: Path) -> bool:
    """Return True when ``path`` is a writable HORUS source checkout (not site-packages)."""
    try:
        if not path.is_dir():
            return False
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            return False
        if not (path / "pyproject.toml").is_file():
            return False
        if not (path / "horus" / "__init__.py").is_file():
            return False
        return os.access(path, os.W_OK)
    except OSError:
        return False


def detect_project_root(
    cwd: Path | None = None,
    *,
    include_package_root: bool = True,
) -> Path | None:
    """Locate a writable HORUS source checkout from ``cwd`` (or an editable package install)."""
    base = (cwd if cwd is not None else Path.cwd()).resolve()
    for candidate in (base, *base.parents):
        if is_horus_project_root(candidate):
            return candidate
    if include_package_root:
        pkg_root = Path(__file__).resolve().parent.parent
        if is_horus_project_root(pkg_root):
            return pkg_root
    return None


def storage_mode(*, cwd: Path | None = None) -> str:
    """Return ``'project'`` when running from a writable checkout, else ``'user'``."""
    return "project" if detect_project_root(cwd=cwd, include_package_root=True) is not None else "user"


def default_assets_dir(*, cwd: Path | None = None) -> Path:
    """Resolve directory for SITL binaries and ONNX/HF models.

    Precedence:
    1. ``HORUS_ASSETS_DIR`` environment variable
    2. ``<project_root>/models`` when running from a writable HORUS checkout
    3. OS user cache directory (``user_cache_dir('horus', 'horus-drone') / 'assets'``)
    """
    override = os.environ.get("HORUS_ASSETS_DIR")
    if override and override.strip():
        return Path(override).expanduser()
    project_root = detect_project_root(cwd=cwd, include_package_root=True)
    if project_root is not None:
        return project_root / "models"
    return Path(user_cache_dir(APP_NAME, APP_AUTHOR)) / "assets"


def default_runs_dir(*, cwd: Path | None = None) -> Path:
    """Resolve root directory for session logs, SITL workdirs, and runtime artifacts.

    Precedence:
    1. ``HORUS_RUNS_DIR`` environment variable
    2. ``<project_root>/runs`` when running from a writable HORUS checkout
    3. OS user data directory (``user_data_dir('horus', 'horus-drone') / 'runs'``)
    """
    override = os.environ.get("HORUS_RUNS_DIR")
    if override and override.strip():
        return Path(override).expanduser()
    project_root = detect_project_root(cwd=cwd, include_package_root=True)
    if project_root is not None:
        return project_root / "runs"
    return Path(user_data_dir(APP_NAME, APP_AUTHOR)) / "runs"


def default_config_dir() -> Path:
    """Resolve directory for Studio settings, ``secrets.json`` (0600), and local TLS certs.

    Always stays in OS user config storage (or ``HORUS_CONFIG_DIR``), never inside a git checkout.
    """
    override = os.environ.get("HORUS_CONFIG_DIR")
    if override and override.strip():
        return Path(override).expanduser()
    return Path(user_config_dir(APP_NAME, APP_AUTHOR))


def default_grounder_hf_dir(*, cwd: Path | None = None) -> Path:
    """Resolve Hugging Face cache directory for local ``horus-grounder`` weights."""
    for env_name in ("HORUS_GROUNDER_HF_CACHE", "HF_HOME"):
        override = os.environ.get(env_name)
        if override and override.strip():
            return Path(override).expanduser()
    return default_assets_dir(cwd=cwd) / "hf"
