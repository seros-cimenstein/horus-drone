#!/usr/bin/env bash
# Cross-compile the native GTA V pieces (Windows x64, MinGW) in Docker:
#   gta/build/horus_capture.asi          renderer frame capture (gta/capture)
#   gta/build/horus_capture_selftest.exe D3D12 stand-in for checking the capture plugin
#   gta/build/ScriptHookV.dll            HORUS Script Hook, our Script Hook V implementation (gta/hook)
#   gta/build/horus_hook_tests.exe       the hook's unit tests (run under wine, see gta/hook/README.md)
#   gta/build/horus_api_probe.asi        in-game API probe plugin (gta_mods.sh install --api-probe)
#   gta/build/horus_texture_selftest.exe createTexture's decode/upload path on a D3D12 device (no game)
set -Eeuo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
image="horus-gta-capture-build"

docker build --network host -q -t "${image}" "${here}" >/dev/null
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp -v "${here}:/src" -w /src "${image}" bash -c '
  cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE=mingw-w64.cmake
  cmake --build build
'
ls -l "${here}/build/horus_capture.asi" "${here}/build/horus_capture_selftest.exe" "${here}/build/ScriptHookV.dll" \
  "${here}/build/horus_hook_tests.exe" "${here}/build/horus_api_probe.asi" "${here}/build/horus_texture_selftest.exe"
