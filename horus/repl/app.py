"""Terminal control loop; ``--mission`` hands a YAML file to :mod:`horus.repl.missions`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from typing import Any

from horus.commander import HorusCommander
from horus.commander.intents import emergency_action
from horus.config import HorusConfig, load_config
from horus.mcp_client import DEFAULT_MCP_URL, call_json, open_session
from horus.repl.missions import EMERGENCY_TOOLS, run_mission


def _interactive_arguments(line: str) -> tuple[str, dict[str, Any]] | None:
    words = line.split()
    if words and not words[0].startswith("/"):
        # Plain text is an emergency only when it is a short imperative ("stop", "land now",
        # "return home") — the same grammar as the commander server; any other plain text
        # (including apostrophes that shlex would reject) goes to the commander unchanged.
        action = emergency_action(line)
        return (action, {}) if action is not None else None
    try:
        parts = shlex.split(line)
    except ValueError as exc:
        raise ValueError(f"invalid command quoting: {exc}") from exc
    if not parts:
        return None
    command = parts[0].lower()
    if command in ("/quit", "/exit"):
        return "__quit__", {}
    if command == "/state":
        return "get_state", {}
    if len(parts) == 1 and command.removeprefix("/") in EMERGENCY_TOOLS:
        # /hold /rth /land are documented; the bare words stay accepted. A longer line such as
        # "land next to the hiker" is operator free text for the commander, not an emergency.
        return command.removeprefix("/"), {}
    if command == "/goto":
        if len(parts) != 4:
            raise ValueError("usage: /goto NORTH_M EAST_M ALTITUDE_M")
        return "goto", {
            "pose": {
                "north_m": float(parts[1]),
                "east_m": float(parts[2]),
                "up_m": float(parts[3]),
            },
        }
    if command == "/take_off":
        if len(parts) > 2:
            raise ValueError("usage: /take_off [ALTITUDE_M]")
        return "take_off", {"altitude_m": float(parts[1]) if len(parts) == 2 else 2.0, "ttl_s": 30.0}
    if command in ("/approach", "/follow", "/orbit"):
        if len(parts) < 2 or len(parts) > 3:
            raise ValueError(f"usage: {command} TRACKER_ID [DISTANCE_M]")
        key = "radius_m" if command == "/orbit" else "dist_m"
        args = {"id": int(parts[1]), "ttl_s": 30.0}
        if len(parts) == 3:
            args[key] = float(parts[2])
        return command[1:], args
    return None


async def _commander_turns(commander: HorusCommander, lines: asyncio.Queue[str]) -> None:
    """Send free text one request at a time, off the input loop, so emergencies stay typeable."""
    while True:
        line = await lines.get()
        try:
            print(await commander.submit_task(line))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
        finally:
            lines.task_done()


def _discard_queued(lines: asyncio.Queue[str]) -> int:
    discarded = 0
    while True:
        try:
            lines.get_nowait()
        except asyncio.QueueEmpty:
            return discarded
        lines.task_done()
        discarded += 1


async def interactive(server_url: str, token: str, config: HorusConfig) -> None:
    commander: HorusCommander | None = None
    commander_task: asyncio.Task | None = None
    turns_task: asyncio.Task | None = None
    lines: asyncio.Queue[str] = asyncio.Queue()
    print("HORUS REPL. /state /goto N E ALT /hold /rth /land /take_off [ALT]; free text goes to commander.")
    try:
        async with open_session(server_url, token) as session:
            quit_requested = False
            while True:
                try:
                    line = await asyncio.to_thread(input, "horus> ")
                except EOFError:
                    break
                if not line.strip():
                    continue
                try:
                    parsed = _interactive_arguments(line)
                    if parsed is None:
                        if commander is None:
                            commander = HorusCommander(server_url, token, config.commander)
                            commander_task = asyncio.create_task(commander.run())
                            turns_task = asyncio.create_task(_commander_turns(commander, lines))
                        lines.put_nowait(line)
                    elif parsed[0] == "__quit__":
                        quit_requested = True
                        break
                    elif parsed[0] in EMERGENCY_TOOLS:
                        # Free text typed before the emergency must not reach the model after it.
                        discarded = _discard_queued(lines)
                        if commander is None:
                            result = await call_json(session, parsed[0])
                            print(json.dumps(result, ensure_ascii=False, indent=2))
                        else:
                            # Interrupts the active model turn; uses this session until the
                            # commander's own MCP session is connected.
                            print(await commander.emergency(parsed[0], session=session))
                        if discarded:
                            print(f"discarded {discarded} queued commander request(s)", file=sys.stderr)
                    else:
                        result = await call_json(session, parsed[0], parsed[1])
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                except Exception as exc:
                    print(f"error: {exc}", file=sys.stderr)
            if turns_task is not None and commander_task is not None and not quit_requested:
                # End of input: let requests already typed finish, unless the commander died.
                drained = asyncio.create_task(lines.join())
                await asyncio.wait({drained, commander_task}, return_when=asyncio.FIRST_COMPLETED)
                drained.cancel()
                await asyncio.gather(drained, return_exceptions=True)
    finally:
        if turns_task is not None:
            turns_task.cancel()
            await asyncio.gather(turns_task, return_exceptions=True)
        if commander is not None:
            await commander.stop()
        if commander_task is not None:
            try:
                await commander_task
            except Exception:
                pass


async def _run(args: argparse.Namespace, config: HorusConfig) -> None:
    server_url = args.server_url or os.environ.get("HORUS_MCP_URL", DEFAULT_MCP_URL)
    token = os.environ.get(config.server.auth_token_env)
    if not token:
        raise RuntimeError(f"{config.server.auth_token_env} must be set")
    if args.mission:
        async with open_session(server_url, token) as session:
            await run_mission(
                session,
                args.mission,
                args.repeat,
                server_url=server_url,
                bearer_token=token,
                commander_settings=config.commander,
            )
    else:
        await interactive(server_url, token, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="HORUS terminal REPL and skill/commander mission runner")
    parser.add_argument("--server-url", help=f"MCP streamable HTTP URL (default: {DEFAULT_MCP_URL})")
    parser.add_argument("--config", help="HORUS YAML config file")
    parser.add_argument("--mission", help="run a missions/*.yaml file and exit")
    parser.add_argument("--repeat", type=int, default=1, help="repeat mission N times")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args, load_config(args.config)))
    except (RuntimeError, ValueError, OSError, TimeoutError) as exc:
        parser.exit(2, f"horus: {exc}\n")


if __name__ == "__main__":
    main()
