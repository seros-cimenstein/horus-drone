#!/usr/bin/env bash
set -euo pipefail

# HORUS one-line installer for Linux and macOS.
# Usage: curl -LsSf https://seros-cimenstein.github.io/horus-drone/install.sh | sh

HORUS_PKG="${HORUS_PKG:-git+https://github.com/seros-cimenstein/horus-drone.git}"
HORUS_PYTHON="${HORUS_PYTHON:-3.12}"

printf '==> Installing HORUS (%s, Python %s)\n' "${HORUS_PKG}" "${HORUS_PYTHON}"

if ! command -v uv >/dev/null 2>&1; then
  printf '==> Bootstrapping uv...\n'
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  printf 'error: uv was not found on PATH after installation.\n' >&2
  exit 1
fi

printf '==> Installing %s via uv tool install...\n' "${HORUS_PKG}"
uv tool install --upgrade --python "${HORUS_PYTHON}" "${HORUS_PKG}"

export PATH="${HOME}/.local/bin:${PATH}"

if [ "${HORUS_SKIP_SETUP:-0}" != "1" ]; then
  printf '==> Fetching ArduCopter SITL and vision models (horus setup)...\n'
  horus setup
else
  printf '==> Skipping asset download (HORUS_SKIP_SETUP=1).\n'
fi

horus --help >/dev/null
printf '\nHORUS installed successfully! Run horus to launch HORUS Studio.\n'
