from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import httpx

from horus import bootstrap, paths


def _make_fake_checkout(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'horus-drone'\n", encoding="utf-8")
    (root / "horus").mkdir(exist_ok=True)
    (root / "horus" / "__init__.py").write_text("", encoding="utf-8")
    return root


def _tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_detect_project_root_finds_checkout_and_rejects_site_packages(tmp_path: Path) -> None:
    repo = _make_fake_checkout(tmp_path / "horus-drone")
    nested = repo / "sub" / "dir"
    nested.mkdir(parents=True)

    assert paths.detect_project_root(cwd=repo, include_package_root=False) == repo.resolve()
    assert paths.detect_project_root(cwd=nested, include_package_root=False) == repo.resolve()
    assert paths.detect_project_root(cwd=tmp_path, include_package_root=False) is None

    sp_repo = _make_fake_checkout(tmp_path / "venv" / "lib" / "python3.12" / "site-packages")
    assert paths.is_horus_project_root(sp_repo) is False


def test_hybrid_paths_project_vs_installed_and_overrides(tmp_path: Path, monkeypatch) -> None:
    for var in ("HORUS_ASSETS_DIR", "HORUS_RUNS_DIR", "HORUS_CONFIG_DIR", "HORUS_GROUNDER_HF_CACHE", "HF_HOME"):
        monkeypatch.delenv(var, raising=False)

    fake_cache = tmp_path / "os-cache"
    fake_data = tmp_path / "os-data"
    fake_cfg = tmp_path / "os-config"
    monkeypatch.setattr(paths, "user_cache_dir", lambda app, author: str(fake_cache))
    monkeypatch.setattr(paths, "user_data_dir", lambda app, author: str(fake_data))
    monkeypatch.setattr(paths, "user_config_dir", lambda app, author: str(fake_cfg))

    # 1. Project checkout mode: models & runs stay in repo; config stays in OS user config dir
    repo = _make_fake_checkout(tmp_path / "checkout")
    monkeypatch.setattr(paths, "detect_project_root", lambda cwd=None, include_package_root=True: repo)
    assert paths.storage_mode() == "project"
    assert paths.default_assets_dir() == repo / "models"
    assert paths.default_runs_dir() == repo / "runs"
    assert paths.default_grounder_hf_dir() == repo / "models" / "hf"
    assert paths.default_config_dir() == fake_cfg

    # 2. Installed package mode: assets/runs/config all go to OS user directories
    monkeypatch.setattr(paths, "detect_project_root", lambda cwd=None, include_package_root=True: None)
    assert paths.storage_mode() == "user"
    assert paths.default_assets_dir() == fake_cache / "assets"
    assert paths.default_runs_dir() == fake_data / "runs"
    assert paths.default_grounder_hf_dir() == fake_cache / "assets" / "hf"
    assert paths.default_config_dir() == fake_cfg

    # 3. Environment overrides always win
    custom_assets = tmp_path / "custom-assets"
    custom_runs = tmp_path / "custom-runs"
    custom_cfg = tmp_path / "custom-cfg"
    custom_hf = tmp_path / "custom-hf"
    monkeypatch.setenv("HORUS_ASSETS_DIR", str(custom_assets))
    monkeypatch.setenv("HORUS_RUNS_DIR", str(custom_runs))
    monkeypatch.setenv("HORUS_CONFIG_DIR", str(custom_cfg))
    monkeypatch.setenv("HORUS_GROUNDER_HF_CACHE", str(custom_hf))
    assert paths.default_assets_dir() == custom_assets
    assert paths.default_runs_dir() == custom_runs
    assert paths.default_config_dir() == custom_cfg
    assert paths.default_grounder_hf_dir() == custom_hf


def test_bootstrap_avoids_models_models_nesting_and_respects_explicit_dir(tmp_path: Path, monkeypatch) -> None:
    repo = _make_fake_checkout(tmp_path / "repo")
    models_root = repo / "models"
    cache_root = tmp_path / "cache" / "assets"

    assert bootstrap._models_dir_for_root(models_root) == models_root
    assert bootstrap._models_dir_for_root(cache_root) == cache_root / "models"

    monkeypatch.chdir(repo)
    monkeypatch.setenv("HORUS_ASSETS_BASE_URL", "https://assets.example.test/release")
    monkeypatch.delenv("HORUS_SITL_BIN", raising=False)
    monkeypatch.delenv("HORUS_YOLO_MODEL", raising=False)
    monkeypatch.delenv("HORUS_REID_MODEL", raising=False)

    sitl_archive = _tar_gz_bytes({"arducopter": b"ELF-MOCK", "copter-default.parm": b"FRAME_CLASS 1\n"})
    yolo_bytes = b"ONNX-YOLO-MOCK"
    reid_bytes = b"ONNX-REID-MOCK"
    files = {
        "/release/arducopter-4.7.1-linux-x86_64.tar.gz": sitl_archive,
        "/release/arducopter-4.7.1-linux-aarch64.tar.gz": sitl_archive,
        "/release/yolov8n.onnx": yolo_bytes,
        "/release/osnet_x0_25_msmt17.onnx": reid_bytes,
    }
    sums = "\n".join(
        f"{hashlib.sha256(content).hexdigest()}  {Path(path).name}"
        for path, content in files.items()
    )
    files["/release/SHA256SUMS.txt"] = sums.encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-length": str(len(body))})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        statuses = bootstrap.ensure_assets(cache_dir=models_root, client=client)

    assert statuses["yolov8n"].path == models_root / "yolov8n.onnx"
    assert statuses["reid"].path == models_root / "osnet_x0_25_msmt17.onnx"
    assert (models_root / "sitl").is_dir()
    assert not (models_root / "models").exists()

    # Explicit HORUS_ASSETS_DIR pointing to an empty directory must not be shadowed by repo models/
    empty_dir = tmp_path / "empty-assets"
    empty_dir.mkdir()
    monkeypatch.setenv("HORUS_ASSETS_DIR", str(empty_dir))
    explicit_statuses = bootstrap.check_assets()
    assert explicit_statuses["yolov8n"].available is False
