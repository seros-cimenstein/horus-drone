"""Unified HORUS CLI entrypoint (`horus`, `horus studio`, `horus tui`, `horus mcp`, `horus setup`, `horus doctor`, `horus logs`)."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Sequence
import webbrowser

from horus.bootstrap import AssetStatus, assets_ready, check_assets, doctor_report, ensure_assets
from horus.commander.server import DEFAULT_HOST, DEFAULT_PORT, _GRACEFUL_SHUTDOWN_S, control_app_url, create_app
from horus.logging import (
    _RICH_AVAILABLE,
    add_logging_arguments,
    configure_logging,
    get_console,
    install_unhandled_exception_hooks,
    redact_text,
    render_error,
)

_SUBCOMMANDS = frozenset({"studio", "tui", "mcp", "setup", "doctor", "logs"})
_LOG_LEVEL_STYLES = {
    "CRITICAL": "bold white on red",
    "ERROR": "bold red",
    "WARNING": "bold yellow",
    "WARN": "bold yellow",
    "INFO": "bold green",
    "DEBUG": "dim cyan",
}

logger = logging.getLogger("horus.cli")


def _open_browser(url: str) -> None:
    """Open Studio URL without leaking child GUI stderr or virtualenv vars."""
    if sys.platform.startswith("linux"):
        opener = shutil.which("xdg-open")
        if opener:
            env = dict(os.environ)
            for key in (
                "PYTHONHOME",
                "PYTHONPATH",
                "LD_LIBRARY_PATH",
                "GTK_PATH",
                "GIO_MODULE_DIR",
                "GDK_PIXBUF_MODULE_FILE",
            ):
                env.pop(key, None)
            try:
                subprocess.Popen(
                    [opener, url],
                    env=env,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except OSError:
                pass
    try:
        webbrowser.open(url)
    except Exception as exc:
        logger.warning("Could not launch browser automatically: %s", exc)


def _is_interactive_tty(stream: object) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and not bool(
        os.environ.get("NO_COLOR", "").strip()
    )


def _run_studio(args: argparse.Namespace) -> int:
    import uvicorn

    from horus.commander.agent import HorusCommander
    from horus.commander.display import DisplayReader
    from horus.studio.lan_tls import detect_lan_ip, ensure_lan_tls_cert
    from horus.studio.link_shaper import LinkShaper
    from horus.studio.settings import StudioSettingsStore
    from horus.studio.supervisor import StudioSupervisor

    lan_mode = bool(getattr(args, "lan", False))
    token = os.environ.get("HORUS_COMMANDER_TOKEN") or secrets.token_urlsafe(24)
    phone_token = os.environ.get("HORUS_COMMANDER_PHONE_TOKEN") or (
        secrets.token_urlsafe(24) if (args.phone or lan_mode) else None
    )
    default_log = Path("runs/studio/studio.log")
    chosen_log = args.log_file if args.log_file is not None else default_log
    try:
        configure_logging(
            level=args.log_level,
            fmt=args.log_format,
            log_file=chosen_log,
            component="studio",
            extra_secrets=[token, phone_token],
        )
        active_log_path: Path | None = Path(chosen_log)
    except OSError as exc:
        configure_logging(
            level=args.log_level,
            fmt=args.log_format,
            log_file=None,
            component="studio",
            extra_secrets=[token, phone_token],
        )
        active_log_path = None
        logger.warning("Could not open studio log file %s: %s", chosen_log, exc)
    install_unhandled_exception_hooks("horus.studio")
    _maybe_auto_setup_for_studio(args)

    store = StudioSettingsStore()
    shaper = LinkShaper()
    cs, api_key, _ = store.commander_settings()
    commander = HorusCommander("", "", cs, api_key=api_key, link_shaper=shaper)
    display = DisplayReader()
    supervisor = StudioSupervisor(
        settings_store=store,
        link_shaper=shaper,
        commander=commander,
        display=display,
    )
    lan_ip = detect_lan_ip() if lan_mode else None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None
    bind_host = DEFAULT_HOST
    if lan_mode:
        cert_path, key_path = ensure_lan_tls_cert(store.config_dir, lan_ip)
        ssl_certfile = str(cert_path)
        ssl_keyfile = str(key_path)
        bind_host = "0.0.0.0"

    supervisor.set_control_endpoint(
        args.port,
        phone_token,
        lan_ip=lan_ip,
        tls_enabled=lan_mode,
    )
    app = create_app(commander, token, display=display, phone_token=phone_token, studio=supervisor)

    async def serve() -> None:
        config_kwargs: dict[str, object] = {
            "host": bind_host,
            "port": args.port,
            "log_level": "info",
            "log_config": None,
            "access_log": False,
            "timeout_graceful_shutdown": _GRACEFUL_SHUTDOWN_S,
        }
        if ssl_certfile and ssl_keyfile:
            config_kwargs["ssl_certfile"] = ssl_certfile
            config_kwargs["ssl_keyfile"] = ssl_keyfile
        server = uvicorn.Server(uvicorn.Config(app, **config_kwargs))
        loop = asyncio.get_running_loop()
        orig_handle_exit = server.handle_exit

        def _on_exit(sig: int, frame: object) -> object:
            try:
                loop.call_soon_threadsafe(app.begin_shutdown)
            except RuntimeError:
                pass
            return orig_handle_exit(sig, frame)

        server.handle_exit = _on_exit  # type: ignore[method-assign]

        def _on_sigterm(_signum: int, _frame: object) -> None:
            try:
                loop.call_soon_threadsafe(app.begin_shutdown)
            except RuntimeError:
                pass
            server.should_exit = True

        previous_sigterm = signal.signal(signal.SIGTERM, _on_sigterm)
        commander_task = asyncio.create_task(commander.run())
        display_task = asyncio.create_task(display.run())
        server_task = asyncio.create_task(server.serve())

        async def announce() -> None:
            while not server.started:
                await asyncio.sleep(0.05)
            scheme = "https" if lan_mode else "http"
            url = f"{scheme}://{DEFAULT_HOST}:{args.port}/#token={token}" if lan_mode else control_app_url(args.port, token)
            lan_phone_url = (
                f"https://{lan_ip}:{args.port}/?view=phone#token={phone_token}"
                if (lan_mode and lan_ip and phone_token)
                else None
            )
            out_console = get_console(stderr=False) if (_RICH_AVAILABLE and _is_interactive_tty(sys.stdout)) else None
            if out_console is not None:
                from rich.text import Text

                out_console.print(
                    Text.assemble(("HORUS Studio: ", "horus.brand"), (url, "horus.url")),
                    soft_wrap=True,
                )
                if lan_phone_url:
                    out_console.print(
                        Text.assemble(("LAN Phone PWA: ", "horus.ok"), (lan_phone_url, "horus.url")),
                        soft_wrap=True,
                    )
                elif phone_token:
                    out_console.print(
                        Text.assemble(("Phone token active ", "horus.ok"), (f"(#token={phone_token})", "horus.muted")),
                        soft_wrap=True,
                    )
            else:
                print(f"HORUS Studio: {url}", flush=True)
                if lan_phone_url:
                    print(f"LAN Phone PWA: {lan_phone_url}", flush=True)
                elif phone_token:
                    print(f"Phone token active (#token={phone_token})", flush=True)
            if _RICH_AVAILABLE and _is_interactive_tty(sys.stderr):
                from rich.panel import Panel
                from rich.table import Table

                console = get_console(stderr=True)
                if console is not None:
                    grid = Table.grid(padding=(0, 2))
                    grid.add_column(style="horus.key")
                    grid.add_column()
                    grid.add_row("Endpoint", f"{scheme}://{bind_host}:{args.port}")
                    grid.add_row("Log file", str(active_log_path) if active_log_path else "console only")
                    grid.add_row("Logs CLI", "horus logs --follow")
                    grid.add_row("Shutdown", "Press Ctrl+C for clean teardown")
                    console.print(
                        Panel(
                            grid,
                            title="[horus.brand]HORUS Studio Ready[/]",
                            border_style="cyan",
                            expand=False,
                        )
                    )
            if not args.no_browser:
                logger.info("Launching browser at %s://%s:%d ...", scheme, DEFAULT_HOST, args.port)
                _open_browser(url)

        announce_task = asyncio.create_task(announce())
        try:
            await server_task
        finally:
            app.begin_shutdown()
            server.should_exit = True
            await supervisor.close()
            await commander.stop()
            display_task.cancel()
            announce_task.cancel()
            commander_task.cancel()
            await asyncio.gather(
                commander_task, display_task, announce_task, return_exceptions=True
            )
            signal.signal(signal.SIGTERM, previous_sigterm)

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logger.info("HORUS Studio interrupted; shutdown complete")
        return 0
    except Exception as exc:
        logger.exception("HORUS Studio exited with an error")
        render_error("HORUS Studio failed", exc)
        return 1
    return 0


def _download_assets_with_progress() -> dict[str, AssetStatus]:
    """Download and verify missing standalone SITL and ONNX assets with progress output."""
    if _RICH_AVAILABLE and _is_interactive_tty(sys.stdout):
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            SpinnerColumn,
            TaskID,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        console = get_console(stderr=False)
        if console is not None:
            console.print("[horus.brand]Checking/downloading HORUS standalone assets...[/]")
            tasks_by_name: dict[str, TaskID] = {}
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.fields[asset]}[/]"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                TextColumn("{task.fields[state]}"),
                console=console,
            ) as progress_bar:

                def rich_progress(name: str, done: int, total: int | None, state: str) -> None:
                    task_id = tasks_by_name.get(name)
                    if task_id is None:
                        task_id = progress_bar.add_task(
                            name,
                            total=total or 1,
                            completed=done,
                            asset=name,
                            state=state,
                        )
                        tasks_by_name[name] = task_id
                    else:
                        progress_bar.update(
                            task_id,
                            total=total if total is not None else max(done, 1),
                            completed=done,
                            state=state,
                        )

                statuses = ensure_assets(progress_cb=rich_progress)
            for name, st in statuses.items():
                style = "horus.ok" if st.available else ("horus.muted" if st.optional else "horus.err")
                console.print(f"  [{style}]{name}[/]: {st.detail}")
            return statuses

    def progress(name: str, done: int, total: int | None, state: str) -> None:
        if state in {"verifying", "ready", "optional", "error"}:
            print(f"  {name}: {state} ({done} bytes)", flush=True)

    print("Checking/downloading HORUS standalone assets...", flush=True)
    statuses = ensure_assets(progress_cb=progress)
    for name, st in statuses.items():
        print(f"  {name}: {st.detail}")
    return statuses


def _maybe_auto_setup_for_studio(args: argparse.Namespace) -> None:
    """Automatically fetch missing required SITL/ONNX assets on first Studio launch."""
    if getattr(args, "no_auto_setup", False):
        return
    if os.environ.get("HORUS_SKIP_SETUP", "").strip().lower() in {"1", "true", "yes"}:
        return
    if assets_ready(check_assets()):
        return

    logger.info(
        "Missing required standalone assets; downloading automatically "
        "(pass --no-auto-setup or HORUS_SKIP_SETUP=1 to skip)..."
    )
    try:
        _download_assets_with_progress()
    except Exception as exc:
        logger.warning(
            "Automatic asset setup did not complete: %s. "
            "Continuing without cached SITL/models; run `horus setup` or use Studio's Download button later.",
            exc,
        )


def _run_setup(args: argparse.Namespace) -> int:
    configure_logging(
        level=args.log_level,
        fmt=args.log_format,
        log_file=args.log_file,
        component="setup",
    )
    if args.check:
        statuses = check_assets()
        ok = assets_ready(statuses)
        if _RICH_AVAILABLE and _is_interactive_tty(sys.stdout):
            from rich.table import Table

            console = get_console(stderr=False)
            if console is not None:
                table = Table(title="HORUS Asset Check", show_lines=False)
                table.add_column("Asset", style="horus.key")
                table.add_column("Status")
                table.add_column("Detail")
                for name, st in statuses.items():
                    if st.available:
                        badge = "[horus.ok][OK][/]"
                    elif st.optional:
                        badge = "[horus.muted][OPTIONAL][/]"
                    else:
                        badge = "[horus.err][MISSING][/]"
                    table.add_row(name, badge, st.detail)
                console.print(table)
                return 0 if ok else 1

        for name, st in statuses.items():
            mark = "OK" if st.available else ("OPTIONAL" if st.optional else "MISSING")
            print(f"[{mark}] {name}: {st.detail}")
        return 0 if ok else 1

    try:
        statuses = _download_assets_with_progress()
    except Exception as exc:
        if _RICH_AVAILABLE and _is_interactive_tty(sys.stdout):
            render_error("HORUS asset download failed", exc)
        else:
            print(f"Asset download failed: {exc}", file=sys.stderr)
        return 1

    if args.with_grounder:
        if _RICH_AVAILABLE and _is_interactive_tty(sys.stdout):
            console = get_console(stderr=False)
            if console is not None:
                console.print(
                    "[horus.muted]Optional local Grounder setup is available via "
                    "`scripts/setup_grounder_venv.sh`.[/]"
                )
        else:
            print("Optional local Grounder setup is available via `scripts/setup_grounder_venv.sh`.")
    return 0 if assets_ready(statuses) else 1


def _run_doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    configure_logging(
        level=args.log_level,
        fmt=args.log_format,
        log_file=args.log_file,
        component="doctor",
    )
    if _RICH_AVAILABLE:
        from rich.table import Table

        console = get_console(stderr=False)
        if console is not None:
            table = Table(
                title=f"HORUS Doctor ({report['platform']}, Python {report['python']})",
                show_lines=False,
            )
            table.add_column("Check", style="horus.key")
            table.add_column("Status")
            table.add_column("Details")
            table.add_row("Cache dir", "[horus.ok]OK[/]", str(report["cache_dir"]))
            mujoco_badge = "[horus.ok]OK[/]" if report["mujoco"]["ok"] else "[horus.warn]WARN[/]"
            table.add_row("MuJoCo GL", mujoco_badge, str(report["mujoco"]["detail"]))
            for name, info in report["assets"].items():
                badge = "[horus.ok]OK[/]" if info["available"] else "[horus.err]MISSING[/]"
                table.add_row(f"Asset {name}", badge, str(info["detail"]))
            ports = ", ".join(report["serial_ports"]) or "none detected"
            table.add_row("Serial", "[horus.muted]INFO[/]", ports)
            ts_badge = "[horus.ok]OK[/]" if report["tailscale"] else "[horus.muted]NONE[/]"
            table.add_row("Tailscale", ts_badge, "installed" if report["tailscale"] else "not found")
            creds = ", ".join([*report["ai_credentials"]["env"], *report["ai_credentials"]["clis"]]) or "none"
            cred_badge = "[horus.ok]OK[/]" if creds != "none" else "[horus.warn]NONE[/]"
            table.add_row("AI creds", cred_badge, creds)
            console.print(table)
            return 0

    print(f"HORUS Doctor ({report['platform']}, Python {report['python']})")
    print(f"  Cache dir:  {report['cache_dir']}")
    print(f"  MuJoCo GL:  {'OK' if report['mujoco']['ok'] else 'WARN'} — {report['mujoco']['detail']}")
    for name, info in report["assets"].items():
        print(f"  Asset {name:7s}: {'OK' if info['available'] else 'MISSING'} — {info['detail']}")
    ports = ", ".join(report["serial_ports"]) or "none detected"
    print(f"  Serial:     {ports}")
    print(f"  Tailscale:  {'installed' if report['tailscale'] else 'not found'}")
    creds = ", ".join([*report["ai_credentials"]["env"], *report["ai_credentials"]["clis"]]) or "none"
    print(f"  AI creds:   {creds}")
    return 0


def _discover_log_targets(runs_dir: Path, run: str, service: str) -> list[tuple[str, Path]]:
    """Locate log files for ``horus logs`` across studio runs and standalone MCP runs."""
    targets: list[tuple[str, Path]] = []
    studio_root = runs_dir / "studio"

    studio_log = studio_root / "studio.log"
    if service in {"all", "studio"} and studio_log.is_file():
        targets.append(("studio", studio_log))

    run_dir: Path | None = None
    if studio_root.is_dir():
        if run == "latest":
            candidates = [p for p in studio_root.iterdir() if p.is_dir()]
            if candidates:
                run_dir = max(candidates, key=lambda p: p.stat().st_mtime)
        else:
            candidate = studio_root / run
            if candidate.is_dir():
                run_dir = candidate

    service_files = (
        ("mcp", "mcp.log"),
        ("world", "world.log"),
        ("sitl", "sitl.log"),
        ("events", "mcp_run/events.jsonl"),
    )
    if run_dir is not None:
        for svc_name, rel_path in service_files:
            if service not in {"all", svc_name}:
                continue
            path = run_dir / rel_path
            if path.is_file():
                targets.append((svc_name, path))

    # Fallback for standalone `horus mcp` runs recorded in `runs/horus/current-run`.
    if service in {"all", "events"} and not any(name == "events" for name, _ in targets):
        pointer = runs_dir / "horus" / "current-run"
        if pointer.is_file():
            try:
                active_dir = Path(pointer.read_text(encoding="utf-8").strip()).expanduser()
                events_path = active_dir / "events.jsonl"
                if events_path.is_file():
                    targets.append(("events", events_path))
            except OSError:
                pass

    return targets


def _extract_log_level(line: str, service: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                if "level" in obj:
                    return str(obj["level"]).upper()
                if service == "events":
                    kind = str(obj.get("kind", "")).lower()
                    if "error" in kind or obj.get("status") == "error":
                        return "ERROR"
                    if kind in {"emergency", "link_lost", "auto_rth"}:
                        return "WARNING"
                    return "INFO"
        except ValueError:
            pass
    upper = stripped.upper()
    for lvl in ("CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
        if f" {lvl}" in upper or upper.startswith(lvl) or f"[{lvl}]" in upper:
            return "WARNING" if lvl == "WARN" else lvl
    return None


def _matches_min_level(line_level: str | None, min_level: str | None) -> bool:
    if not min_level:
        return True
    if line_level is None:
        return False
    threshold = logging.getLevelName(min_level.upper())
    actual = logging.getLevelName(line_level.upper())
    if not isinstance(threshold, int) or not isinstance(actual, int):
        return True
    return actual >= threshold


def _format_event_line(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return stripped
    try:
        obj = json.loads(stripped)
    except ValueError:
        return stripped
    if not isinstance(obj, dict):
        return stripped
    if "level" in obj and "message" in obj:
        ts = obj.get("ts", "")
        lvl = obj.get("level", "INFO")
        comp = obj.get("component", "horus")
        msg = obj.get("message", "")
        exc = obj.get("exception")
        base = f"{ts} {lvl:<8s} [{comp}] {msg}"
        return f"{base}\n{exc}" if exc else base
    ts = obj.get("timestamp") or obj.get("time") or ""
    kind = obj.get("kind") or obj.get("tool") or "event"
    summary = obj.get("summary") or obj.get("action") or obj.get("status") or obj.get("error") or ""
    return f"{ts} [{kind}] {summary}".strip()


def _print_log_line(console: object | None, svc: str, line: str, *, multi: bool) -> None:
    text = redact_text(_format_event_line(line) if line.lstrip().startswith("{") else line.rstrip("\r\n"))
    prefix = f"[{svc}] " if multi else ""
    if console is not None and _RICH_AVAILABLE:
        from rich.text import Text

        lvl = _extract_log_level(line, svc)
        style = _LOG_LEVEL_STYLES.get(lvl or "", "")
        rich_line = Text()
        if prefix:
            rich_line.append(prefix, style="horus.brand")
        rich_line.append(text, style=style)
        console.print(rich_line, highlight=False, soft_wrap=True)  # type: ignore[attr-defined]
    else:
        print(f"{prefix}{text}", flush=True)


def _run_logs(args: argparse.Namespace) -> int:
    configure_logging(
        level=args.log_level,
        fmt=args.log_format,
        log_file=args.log_file,
        component="logs",
    )
    runs_dir = Path(args.runs_dir)
    targets = _discover_log_targets(runs_dir, args.run, args.service)
    console = get_console(stderr=False) if _RICH_AVAILABLE else None

    if not targets:
        msg = f"No log files found under {runs_dir} (service={args.service}, run={args.run})."
        if console is not None:
            console.print(f"[horus.warn]{msg}[/]")
        else:
            print(msg)
        return 0

    if args.list:
        if console is not None and _RICH_AVAILABLE and _is_interactive_tty(sys.stdout):
            from rich.table import Table

            table = Table(title="HORUS Log Files")
            table.add_column("Service", style="horus.brand")
            table.add_column("Path", overflow="fold")
            table.add_column("Bytes", justify="right")
            table.add_column("Modified (UTC)")
            for svc, path in targets:
                st = path.stat()
                mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                table.add_row(svc, str(path), str(st.st_size), mtime)
            console.print(table)
        else:
            for svc, path in targets:
                st = path.stat()
                print(f"{svc:8s} {st.st_size:>10d}  {path}")
        return 0

    multi = len(targets) > 1
    offsets: dict[Path, int] = {}
    for svc, path in targets:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                if args.tail > 0:
                    lines = list(deque(fh, maxlen=args.tail))
                else:
                    lines = fh.readlines()
                offsets[path] = fh.tell()
        except OSError as exc:
            logger.warning("Failed reading %s: %s", path, exc)
            continue
        for line in lines:
            if _matches_min_level(_extract_log_level(line, svc), args.level):
                _print_log_line(console, svc, line, multi=multi)

    if not args.follow:
        return 0

    try:
        while True:
            had_output = False
            for svc, path in targets:
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    pos = offsets.get(path, 0)
                    if size < pos:
                        pos = 0
                    if size == pos:
                        continue
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        chunk = fh.readlines()
                        offsets[path] = fh.tell()
                except OSError:
                    continue
                for line in chunk:
                    if _matches_min_level(_extract_log_level(line, svc), args.level):
                        _print_log_line(console, svc, line, multi=multi)
                        had_output = True
            if not had_output:
                time.sleep(0.25)
    except KeyboardInterrupt:
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="horus",
        description="HORUS autonomous drone supervision Studio, TUI, Drone board, logs, and diagnostics.",
    )
    sub = parser.add_subparsers(dest="command")

    p_studio = sub.add_parser("studio", help="Start HORUS Studio web app (default)")
    p_studio.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_studio.add_argument("--no-browser", action="store_true")
    p_studio.add_argument("--phone", action="store_true")
    p_studio.add_argument(
        "--lan",
        action="store_true",
        help="Bind on LAN (0.0.0.0) with a local self-signed HTTPS cert for Wi-Fi Phone PWA",
    )
    p_studio.add_argument(
        "--no-auto-setup",
        action="store_true",
        help="Skip automatic download of missing SITL/ONNX assets on startup (env: HORUS_SKIP_SETUP=1)",
    )
    add_logging_arguments(p_studio)

    sub.add_parser("tui", help="Start HORUS terminal control loop (REPL)", add_help=False)
    sub.add_parser("mcp", help="Run the Drone board MCP server", add_help=False)

    p_setup = sub.add_parser("setup", help="Download and SHA-256-verify SITL + ONNX models")
    p_setup.add_argument("--check", action="store_true", help="Check asset presence without downloading")
    p_setup.add_argument("--with-grounder", action="store_true", help="Also print/check optional Grounder info")
    add_logging_arguments(p_setup)

    p_doc = sub.add_parser("doctor", help="Inspect Python, MuJoCo GL, SITL, models, serial, and AI creds")
    p_doc.add_argument("--json", action="store_true", help="Output JSON report")
    add_logging_arguments(p_doc)

    p_logs = sub.add_parser("logs", help="Inspect and tail HORUS Studio, Drone board, SITL, and event logs")
    p_logs.add_argument(
        "--service",
        choices=("all", "studio", "mcp", "world", "sitl", "events"),
        default="all",
        help="Service log stream to display (default: all)",
    )
    p_logs.add_argument("--run", default="latest", help="Studio run ID or 'latest' (default: latest)")
    p_logs.add_argument("--tail", type=int, default=100, help="Number of trailing lines to show (0 for all)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Follow appended log lines until Ctrl+C")
    p_logs.add_argument(
        "--level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=None,
        help="Filter to records at or above this severity",
    )
    p_logs.add_argument("--list", action="store_true", help="List discovered log files and sizes")
    p_logs.add_argument("--runs-dir", type=Path, default=Path("runs"), help="Root runs directory (default: runs)")
    add_logging_arguments(p_logs)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # Backwards compatibility: `horus --server-url ...` or `horus --config ...` dispatches to `tui`.
    if raw and raw[0] not in _SUBCOMMANDS and raw[0] not in {"-h", "--help"}:
        from horus.repl.app import main as repl_main

        repl_main(raw)
        return 0
    if not raw:
        raw = ["studio"]

    parser = _build_parser()
    args, rest = parser.parse_known_args(raw)
    if args.command == "tui":
        from horus.repl.app import main as repl_main

        repl_main(rest)
        return 0
    if args.command == "mcp":
        from horus.mcp_server.server import main as mcp_main

        mcp_main(rest)
        return 0
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.command == "studio":
        return _run_studio(args)
    if args.command == "setup":
        return _run_setup(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "logs":
        return _run_logs(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
