#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_DIR="$ROOT_DIR/models"
mkdir -p "$MODEL_DIR/.source"
model_python="${HORUS_PYTHON:-python3}"

if ! "$model_python" -c 'import onnx' >/dev/null 2>&1; then
    printf 'Install the model-tools extra before fetching: python -m pip install -e ".[model-tools]"\n' >&2
    exit 1
fi


fetch_model() {
    local name="$1"
    local url="$2"
    local expected_sha256="$3"
    local destination="$MODEL_DIR/$name"
    local temporary

    if [[ -f "$destination" ]] && printf '%s  %s\n' "$expected_sha256" "$destination" | sha256sum --check --status; then
        printf 'Verified %s\n' "$destination"
        return
    fi

    temporary="$(mktemp "$MODEL_DIR/.${name##*/}.XXXXXX")"
    trap 'rm -f "$temporary"' RETURN
    "$model_python" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$temporary"
    printf '%s  %s\n' "$expected_sha256" "$temporary" | sha256sum --check --status || {
        printf 'SHA256 mismatch for %s\n' "$name" >&2
        return 1
    }
    mv -- "$temporary" "$destination"
    trap - RETURN
    printf 'Fetched and verified %s\n' "$destination"
}

fetch_model \
    yolov8n.onnx \
    https://huggingface.co/Kalray/yolov8/resolve/9db90102a6903f95ec4a5d6a67517d8d4300654a/yolov8n.onnx \
    65158dad735be799c2466fa15e260c09558080bd530b42a8d0c3d1b419afd8b5
fetch_model \
    .source/osnet_x0_25_msmt17.onnx \
    https://huggingface.co/anriha/osnet_x0_25_msmt17/resolve/1e22b925c70caa5591e9fab3f5540af8484bcad8/osnet_x0_25_msmt17.onnx \
    e78604f4ccda49b8f41cd0f8f7303800ce75d2361895ebb0729513c1bf53d277

reid_temporary="$(mktemp "$MODEL_DIR/.osnet_x0_25_msmt17.XXXXXX")"
trap 'rm -f "$reid_temporary"' EXIT
"$model_python" "$ROOT_DIR/scripts/prepare_reid_onnx.py" \
    "$MODEL_DIR/.source/osnet_x0_25_msmt17.onnx" "$reid_temporary"
mv -- "$reid_temporary" "$MODEL_DIR/osnet_x0_25_msmt17.onnx"
trap - EXIT
printf 'Prepared flexible-batch %s\n' "$MODEL_DIR/osnet_x0_25_msmt17.onnx"
