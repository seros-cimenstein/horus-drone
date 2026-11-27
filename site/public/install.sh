#!/usr/bin/env sh
set -eu

# HORUS one-line installer for Linux and macOS (zero Python/uv/git prerequisites).
# Usage: curl -LsSf https://seros-cimenstein.github.io/horus-drone/install.sh | sh

HORUS_REF="${HORUS_REF:-main}"
HORUS_PKG="${HORUS_PKG:-https://github.com/seros-cimenstein/horus-drone/archive/refs/heads/${HORUS_REF}.tar.gz}"
HORUS_PYTHON="${HORUS_PYTHON:-3.12}"

printf '==> Installing HORUS (%s, Python %s)\n' "${HORUS_PKG}" "${HORUS_PYTHON}"

if ! command -v uv >/dev/null 2>&1; then
  printf '==> Bootstrapping uv (and managed Python %s)...\n' "${HORUS_PYTHON}"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    printf 'error: either curl or wget is required to bootstrap uv.\n' >&2
    exit 1
  fi
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  printf 'error: uv was not found on PATH after installation.\n' >&2
  exit 1
fi

printf '==> Installing %s via uv tool install...\n' "${HORUS_PKG}"
uv tool install --force --upgrade --reinstall-package horus-drone --python "${HORUS_PYTHON}" "${HORUS_PKG}"

export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
uv tool update-shell >/dev/null 2>&1 || true

if [ "${HORUS_SKIP_SETUP:-0}" != "1" ]; then
  printf '==> Fetching ArduCopter SITL and vision models (horus setup)...\n'
  horus setup
else
  printf '==> Skipping asset download (HORUS_SKIP_SETUP=1).\n'
fi

horus --help >/dev/null

if [ "${HORUS_NO_START:-0}" != "1" ] && [ -z "${CI:-}" ]; then
  if [ -t 0 ]; then
    printf '\n==> Starting HORUS Studio...\n'
    exec horus
  elif [ -r /dev/tty ]; then
    printf '\n==> Starting HORUS Studio...\n'
    exec horus </dev/tty
  fi
fi

printf '\nHORUS installed successfully! Run horus to launch HORUS Studio.\n'
