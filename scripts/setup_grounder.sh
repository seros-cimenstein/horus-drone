#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ! -x /opt/horus-venv/bin/python ]]; then
  printf '%s\n' 'HORUS simulation Python is unavailable at /opt/horus-venv/bin/python.' >&2
  exit 1
fi

model_root=/horus/models
venv="${model_root}/grounder-venv"
export HF_HOME="${model_root}/hf"
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

PYTHONPATH=/horus HF_HOME="${HF_HOME}" "${python}" - <<'PY'
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
printf 'Grounder venv ready at %s; model cache at %s.\n' "${venv}" "${HF_HOME}"
