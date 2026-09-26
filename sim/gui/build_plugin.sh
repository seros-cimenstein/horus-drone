#!/usr/bin/env bash
set -euo pipefail

source_root=${1:?usage: build_plugin.sh GUI_SOURCE_DIR}
plugin_dir="${source_root}/horus_commander"
layout="${source_root}/horus_demo.config"
install_prefix=/opt/horus-gui
build_dir=/tmp/horus-commander-build

if [[ ! -f "${plugin_dir}/CMakeLists.txt" || ! -f "${layout}" ]]; then
  echo "GUI plugin source or layout missing under ${source_root}" >&2
  exit 1
fi

cmake -S "${plugin_dir}" -B "${build_dir}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DCMAKE_INSTALL_PREFIX="${install_prefix}"
cmake --build "${build_dir}" --parallel 4
cmake --install "${build_dir}"
test -f "${install_prefix}/lib/libHorusCommander.so"
install -m 0644 "${layout}" "${install_prefix}/horus_demo.config"

rm -rf "${build_dir}"
