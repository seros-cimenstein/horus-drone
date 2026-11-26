"""Unified HORUS CLI entrypoint (`horus`, `horus studio`, `horus tui`, `horus mcp`, `horus setup`, `horus doctor`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import webbrowser
from typing import Sequence

from horus.bootstrap import check_assets, doctor_report, ensure_assets
from horus.commander.server import DEFAULT_HOST, DEFAULT_PORT, _GRACEFUL_SHUTDOWN_S, control_app_url, create_app

_SUBCOMMANDS = frozenset({"studio", "tui", "mcp", "setup", "doctor"})


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
    except Exception:
        pass


def _run_studio(args: argparse.Namespace) -> int:
    import uvicorn

    from horus.commander.agent import HorusCommander
    from horus.commander.display import DisplayReader
    from horus.studio.link_shaper import LinkShaper
    from horus.studio.settings import StudioSettingsStore
    from horus.studio.supervisor import StudioSupervisor

    store = StudioSettingsStore()
    shaper = LinkShaper()
    cs, api_key, _ = store.commander_settings()
    token = os.environ.get("HORUS_COMMANDER_TOKEN") or secrets.token_urlsafe(24)
    phone_token = os.environ.get("HORUS_COMMANDER_PHONE_TOKEN") or (
        secrets.token_urlsafe(24) if args.phone else None
    )
    commander = HorusCommander("", "", cs, api_key=api_key, link_shaper=shaper)
    display = DisplayReader()
    supervisor = StudioSupervisor(
        settings_store=store,
        link_shaper=shaper,
        commander=commander,
        display=display,
    )
    supervisor.set_control_endpoint(args.port, phone_token)
    app = create_app(commander, token, display=display, phone_token=phone_token, studio=supervisor)

    async def serve() -> None:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=DEFAULT_HOST,
                port=args.port,
                log_level="warning",
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_S,
            )
        )
        previous_sigterm = signal.signal(
            signal.SIGTERM, lambda *_: setattr(server, "should_exit", True)
        )
        commander_task = asyncio.create_task(commander.run())
        display_task = asyncio.create_task(display.run())
        server_task = asyncio.create_task(server.serve())

        async def announce() -> None:
            while not server.started:
                await asyncio.sleep(0.05)
            url = control_app_url(args.port, token)
            print(f"HORUS Studio: {url}", flush=True)
            if phone_token:
                print(f"Phone token active (#token={phone_token})", flush=True)
            if not args.no_browser:
                _open_browser(url)

        announce_task = asyncio.create_task(announce())
        try:
            await server_task
        finally:
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
        return 0
    return 0


def _run_setup(args: argparse.Namespace) -> int:
    if args.check:
        statuses = check_assets()
        ok = True
        for name, st in statuses.items():
            mark = "OK" if st.available else "MISSING"
            print(f"[{mark}] {name}: {st.detail}")
            ok &= st.available
        return 0 if ok else 1

    def progress(name: str, done: int, total: int | None, state: str) -> None:
        if state in {"verifying", "ready", "error"}:
            print(f"  {name}: {state} ({done} bytes)", flush=True)

    print("Checking/downloading HORUS standalone assets...", flush=True)
    statuses = ensure_assets(progress_cb=progress)
    for name, st in statuses.items():
        print(f"  {name}: {st.detail}")
    if args.with_grounder:
        print("Optional local Grounder setup is available via `scripts/setup_grounder_venv.sh`.")
    return 0 if all(st.available for st in statuses.values()) else 1


def _run_doctor(args: argparse.Namespace) -> int:
    report = doctor_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="horus",
        description="HORUS autonomous drone supervision Studio, TUI, Drone board, and diagnostics.",
    )
    sub = parser.add_subparsers(dest="command")

    p_studio = sub.add_parser("studio", help="Start HORUS Studio web app (default)")
    p_studio.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_studio.add_argument("--no-browser", action="store_true")
    p_studio.add_argument("--phone", action="store_true")

    sub.add_parser("tui", help="Start HORUS terminal control loop (REPL)", add_help=False)
    sub.add_parser("mcp", help="Run the Drone board MCP server", add_help=False)

    p_setup = sub.add_parser("setup", help="Download and SHA-256-verify SITL + ONNX models")
    p_setup.add_argument("--check", action="store_true", help="Check asset presence without downloading")
    p_setup.add_argument("--with-grounder", action="store_true", help="Also print/check optional Grounder info")

    p_doc = sub.add_parser("doctor", help="Inspect Python, MuJoCo GL, SITL, models, serial, and AI creds")
    p_doc.add_argument("--json", action="store_true", help="Output JSON report")
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
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
