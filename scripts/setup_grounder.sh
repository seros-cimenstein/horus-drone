#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ! -x /opt/horus-venv/bin/python ]]; then
  printf '%s\n' 'HORUS simulation Python is unavailable at /opt/horus-venv/bin/python.' >&2
  exit 1
fi

model_root=/horus/models
venv="${model_root}/grounder-venv"
if [[ -n "${HORUS_GROUNDER_HF_CACHE:-}" ]]; then
  # Offline path: a HuggingFace cache copied from a machine that already downloaded the
  # checkpoints. It must be HF_HOME-shaped (a directory containing hub/), like the cache
  # this script writes to /horus/models/hf; the service reads the same variable.
  if [[ ! -d "${HORUS_GROUNDER_HF_CACHE}" || ! -d "${HORUS_GROUNDER_HF_CACHE}/hub" ]]; then
    printf 'HORUS_GROUNDER_HF_CACHE=%s is not an HF_HOME-style cache (expected a directory containing hub/); copy the cache again or unset it to download here.\n' \
      "${HORUS_GROUNDER_HF_CACHE}" >&2
    exit 1
  fi
  export HF_HOME="${HORUS_GROUNDER_HF_CACHE}"
  export HF_HUB_OFFLINE=1
  printf 'Using the copied grounder cache at %s with HF_HUB_OFFLINE=1.\n' "${HF_HOME}"
else
  export HF_HOME="${model_root}/hf"
  if [[ -n "${HF_ENDPOINT:-}" ]]; then
    printf 'Using HF_ENDPOINT=%s for the grounder checkpoints.\n' "${HF_ENDPOINT}"
  fi
fi
mkdir -p -- "${HF_HOME}"

/opt/horus-venv/bin/python -m venv --system-site-packages "${venv}"
python="${venv}/bin/python"
"${python}" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
"${python}" -m pip install --disable-pip-version-check --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.5.1+cpu' 'torchvision==0.20.1+cpu'
"${python}" -m pip install --disable-pip-version-check --no-cache-dir \
  --index-url https://pypi.org/simple \
  'numpy<2' 'transformers==4.46.3' 'scipy==1.10.1' 'Pillow==10.4.0'

if ! PYTHONPATH=/horus HF_HOME="${HF_HOME}" "${python}" - <<'PY'
import gc

from horus.grounder.backends import MODEL_IDS, create_backend

for model in ("grounding-dino-tiny", "owlv2-base-patch16-ensemble"):
    backend = create_backend(model, torch_threads=6)
    print(f"Downloading {model}: {MODEL_IDS[model]}", flush=True)
    backend.load(warmup=False)
    del backend
    gc.collect()
print("Grounder model weights are cached.", flush=True)
PY
then
  printf '%s\n' \
    'Grounder checkpoint download failed.' \
    'HF_ENDPOINT, when set, must point at a mirror that serves the full Hub file metadata (Content-Length and X-Repo-Commit) huggingface_hub 0.36.2 requires:' \
    '  docker exec -e HF_ENDPOINT=<mirror-url> horus-sim bash /horus/scripts/setup_grounder.sh' \
    'The reliable option is a copied HF_HOME cache (a directory containing hub/):' \
    '  docker exec -e HORUS_GROUNDER_HF_CACHE=/horus/models/hf horus-sim bash /horus/scripts/setup_grounder.sh' >&2
  exit 1
fi
printf 'Grounder venv ready at %s; model cache at %s.\n' "${venv}" "${HF_HOME}"
