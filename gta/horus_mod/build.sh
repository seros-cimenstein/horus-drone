#!/usr/bin/env bash
# Build HorusGta.dll (ScriptHookVDotNet v3 script, .NET Framework 4.8) with the .NET 8 SDK in
# Docker, after running the headless core tests. ScriptHookVDotNet Enhanced is downloaded into the
# gitignored .cache/ as a compile reference only; it is never committed or shipped.
# Output: gta/horus_mod/build/{HorusGta.dll,HorusGta.ini}, and build/physics_sim/horus-gta-physics-sim
# (the same quad physics without GTA, a self-contained linux-x64 binary; README "Physics").
set -Eeuo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
image="mcr.microsoft.com/dotnet/sdk:8.0"
shvdn_url="https://github.com/Chiheb-Bacha/ScriptHookVDotNetEnhanced/releases/download/v1.1.0.6/ScriptHookVDotNetEnhanced-v1.1.0.6.zip"
shvdn_sha256="f10da8819fa6814fb0a04553567d2516934eac96b16af06ad8f315f0da00473a"
cache="${here}/.cache"
zip="${cache}/ScriptHookVDotNetEnhanced-v1.1.0.6.zip"

mkdir -p -- "${cache}/shvdn" "${cache}/nuget"
if ! printf '%s  %s\n' "${shvdn_sha256}" "${zip}" | sha256sum --check --status 2>/dev/null; then
  curl -fsSL -o "${zip}.part" "${shvdn_url}"
  if ! printf '%s  %s\n' "${shvdn_sha256}" "${zip}.part" | sha256sum --check --status; then
    rm -f -- "${zip}.part"
    printf 'ScriptHookVDotNet download does not match sha256 %s\n' "${shvdn_sha256}" >&2
    exit 1
  fi
  mv -- "${zip}.part" "${zip}"
fi
unzip -q -o -j "${zip}" ScriptHookVDotNet3.dll -d "${cache}/shvdn"

rm -rf -- "${here}/build"
# The SDK's named mutexes need a passwd entry whose home exists, so the host uid gets one (home /tmp).
printf 'root:x:0:0::/root:/bin/sh\nbuilder:x:%s:%s::/tmp:/bin/sh\n' "$(id -u)" "$(id -g)" > "${cache}/passwd"
docker run --rm --network host --user "$(id -u):$(id -g)" -v "${cache}/passwd:/etc/passwd:ro" \
  -e HOME=/tmp -e DOTNET_CLI_HOME=/tmp -e NUGET_PACKAGES=/src/.cache/nuget \
  -e DOTNET_CLI_TELEMETRY_OPTOUT=1 -e DOTNET_NOLOGO=1 -e DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=1 \
  -v "${here}:/src" -w /src "${image}" bash -c '
  set -e
  dotnet run --project tests -c Release
  dotnet build src -c Release -o build -p:ShvdnDir=/src/.cache/shvdn
  dotnet publish physics_sim -c Release -r linux-x64 --self-contained -p:PublishSingleFile=true -o build/physics_sim
'
cp -- "${here}/HorusGta.ini" "${here}/build/HorusGta.ini"
ls -l "${here}/build/HorusGta.dll" "${here}/build/HorusGta.ini" "${here}/build/physics_sim/horus-gta-physics-sim"
