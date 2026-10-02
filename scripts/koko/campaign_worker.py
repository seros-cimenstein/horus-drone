#!/usr/bin/env python3
"""Run one detached campaign on Koko and persist its exit status."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


def _write_status(path: Path, code: int) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(f"{code}\n", encoding="ascii")
    os.replace(temporary, path)


def main() -> int:
    if len(sys.argv) != 2:
        print("campaign worker expects one base64 JSON payload", file=sys.stderr)
        return 2
    try:
        payload: dict[str, Any] = json.loads(base64.b64decode(sys.argv[1], validate=True))
        project = Path(payload["project"]).resolve(strict=True)
        output = str(payload["output"])
        arguments = payload["arguments"]
        image = str(payload["image"])
        gpu = str(payload["gpu"])
        container = str(payload["container"])
        if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
            raise ValueError("campaign arguments must be a list of strings")
        if not output.startswith("runs/campaign/") or ".." in Path(output).parts:
            raise ValueError("campaign output must be under runs/campaign")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid campaign payload: {exc}", file=sys.stderr)
        return 2

    destination = project / output
    destination.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "sim/scripts/procedural_campaign.py", *arguments, "--out", output]
    environment = dict(os.environ)
    environment.update({
        "HORUS_SIM_IMAGE": image,
        "HORUS_SIM_GPU": gpu,
        "HORUS_SIM_CONTAINER_NAME": container,
        "PYTHONUNBUFFERED": "1",
    })
    status_file = destination / ".campaign-exit-code"
    try:
        with (destination / "campaign-host.log").open("ab", buffering=0) as log:
            result = subprocess.run(command, cwd=project, env=environment, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, check=False)
        code = result.returncode
    except BaseException as exc:
        with (destination / "campaign-host.log").open("ab", buffering=0) as log:
            log.write(f"campaign worker failed: {type(exc).__name__}: {exc}\n".encode())
        code = 1
    _write_status(status_file, code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
