from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
import zipfile

import httpx
import pytest

from horus.bootstrap import (
    assets_ready,
    check_assets,
    doctor_report,
    ensure_assets,
    extract_archive_safe,
    resolve_sitl_binary,
)


def _tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_archive_safe_rejects_path_traversal(tmp_path: Path) -> None:
    evil_tar = tmp_path / "evil.tar.gz"
    evil_tar.write_bytes(_tar_gz_bytes({"../escape.txt": b"nope"}))
    with pytest.raises(ValueError, match="unsafe path"):
        extract_archive_safe(evil_tar, tmp_path / "out")

    evil_zip = tmp_path / "evil.zip"
    evil_zip.write_bytes(_zip_bytes({"../../escape.txt": b"nope"}))
    with pytest.raises(ValueError, match="unsafe path"):
        extract_archive_safe(evil_zip, tmp_path / "out")


def test_ensure_assets_downloads_verifies_sha256_and_reports_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HORUS_ASSETS_BASE_URL", "https://assets.example.test/release")
    monkeypatch.delenv("HORUS_SITL_BIN", raising=False)
    monkeypatch.delenv("HORUS_YOLO_MODEL", raising=False)
    monkeypatch.delenv("HORUS_REID_MODEL", raising=False)

    sitl_archive = _tar_gz_bytes({"arducopter": b"ELF-MOCK", "copter-default.parm": b"FRAME_CLASS 1\n"})
    sitl_zip = _zip_bytes({"ArduCopter.exe": b"PE-MOCK", "copter-default.parm": b"FRAME_CLASS 1\n"})
    yolo_bytes = b"ONNX-YOLO-MOCK"
    reid_bytes = b"ONNX-REID-MOCK"

    files = {
        "/release/arducopter-4.7.1-linux-x86_64.tar.gz": sitl_archive,
        "/release/arducopter-4.7.1-linux-aarch64.tar.gz": sitl_archive,
        "/release/arducopter-4.7.1-windows-x86_64.zip": sitl_zip,
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

    events: list[tuple[str, int, int | None, str]] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        statuses = ensure_assets(
            cache_dir=tmp_path,
            progress_cb=lambda name, done, total, state: events.append((name, done, total, state)),
            client=client,
        )

    assert all(st.available for st in statuses.values())
    assert statuses["yolov8n"].path == tmp_path / "models" / "yolov8n.onnx"
    assert statuses["reid"].path == tmp_path / "models" / "osnet_x0_25_msmt17.onnx"
    bin_path, default_parm = resolve_sitl_binary(cache_dir=tmp_path)
    assert bin_path.is_file()
    assert default_parm is not None and default_parm.is_file()
    assert any(state == "ready" for _, _, _, state in events)


def test_ensure_assets_rejects_corrupted_digest_without_committing_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HORUS_ASSETS_BASE_URL", "https://assets.example.test/release")
    monkeypatch.delenv("HORUS_YOLO_MODEL", raising=False)
    monkeypatch.delenv("HORUS_REID_MODEL", raising=False)

    bad_yolo = b"CORRUPTED"
    sums = f"{'0' * 64}  yolov8n.onnx\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=sums)
        return httpx.Response(200, content=bad_yolo)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
            ensure_assets(cache_dir=tmp_path, client=client, include_sitl=False)

    assert not (tmp_path / "models" / "yolov8n.onnx").exists()
    assert check_assets(tmp_path)["yolov8n"].available is False


def test_doctor_report_returns_structured_fields(tmp_path: Path) -> None:
    report = doctor_report(cache_dir=tmp_path)
    assert "python" in report and "mujoco" in report and "assets" in report
    assert set(report["assets"]) == {"sitl", "yolov8n", "reid"}


def test_ensure_assets_skips_optional_reid_when_missing_from_release(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HORUS_ASSETS_BASE_URL", "https://assets.example.test/release")
    monkeypatch.delenv("HORUS_SITL_BIN", raising=False)
    monkeypatch.delenv("HORUS_YOLO_MODEL", raising=False)
    monkeypatch.delenv("HORUS_REID_MODEL", raising=False)

    sitl_archive = _tar_gz_bytes({"arducopter": b"ELF-MOCK", "copter-default.parm": b"FRAME_CLASS 1\n"})
    sitl_zip = _zip_bytes({"ArduCopter.exe": b"PE-MOCK", "copter-default.parm": b"FRAME_CLASS 1\n"})
    yolo_bytes = b"ONNX-YOLO-MOCK"

    files = {
        "/release/arducopter-4.7.1-linux-x86_64.tar.gz": sitl_archive,
        "/release/arducopter-4.7.1-linux-aarch64.tar.gz": sitl_archive,
        "/release/arducopter-4.7.1-windows-x86_64.zip": sitl_zip,
        "/release/yolov8n.onnx": yolo_bytes,
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

    events: list[tuple[str, int, int | None, str]] = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        statuses = ensure_assets(
            cache_dir=tmp_path,
            progress_cb=lambda name, done, total, state: events.append((name, done, total, state)),
            client=client,
        )

    assert statuses["sitl"].available is True
    assert statuses["yolov8n"].available is True
    assert statuses["reid"].available is False
    assert statuses["reid"].optional is True
    assert assets_ready(statuses) is True
    assert any(name == "reid" and state == "optional" for name, _, _, state in events)
