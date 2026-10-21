# IndexTTS-2.5 on ROCm. The upstream project resolves CUDA torch via
# pyproject.toml and requires Python 3.10-3.11; this base already ships
# Python 3.10 + torch 2.8.0+rocm7.2.4, which is the version pair IndexTTS-2.5
# pins. Do not let pip replace torch with a CUDA/CPU wheel.
ARG BASE_IMAGE=rocm/pytorch:rocm7.2.4_ubuntu22.04_py3.10_pytorch_release_2.8.0@sha256:880e126d83370e3502b069a39f85cbd7b1f6f7dbfbceb00b6fefbac03c5da091
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/cache/home \
    HF_HOME=/cache/hf \
    TORCH_HOME=/cache/torch \
    OMV_INDEXTTS_MODEL_DIR=/models/checkpoints

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ffmpeg libsndfile1 ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Pin the upstream release rather than main. v2.5.0 is the Aug 2026 release.
RUN git clone --branch v2.5.0 --depth 1 https://github.com/index-tts/index-tts.git /opt/index-tts

# The package itself is pure Python; install it without dependencies first,
# then install upstream's dependency list minus torch/torchaudio. This is the
# AMD workaround: the base image's ROCm torch must remain installed.
RUN python -m pip install --no-cache-dir --no-deps -e /opt/index-tts

RUN python -m pip install --no-cache-dir tomli

RUN python - <<'PY' > /tmp/indextts-deps.txt
import tomli as tomllib

with open("/opt/index-tts/pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)

for dep in project["project"]["dependencies"]:
    name = dep.split(";", 1)[0].strip()
    name = name.split("==", 1)[0].split("[", 1)[0]
    name = name.split(">", 1)[0].split("<", 1)[0].split("~", 1)[0]
    if name.strip().lower() in {"torch", "torchaudio"}:
        continue
    print(dep)
PY
RUN python -m pip install --no-cache-dir -r /tmp/indextts-deps.txt

# front.py builds its TN cache inside the installed package. The container
# runs as UID 1000, so pre-create this writable cache; otherwise first
# inference dies with PermissionError on /opt/index-tts/.../tagger_cache.
RUN mkdir -p /opt/index-tts/indextts/utils/tagger_cache \
 && chmod 777 /opt/index-tts/indextts/utils/tagger_cache

# Compile the pynini grammars once at build time. Otherwise every container
# start pays that cost and needs a writable cache inside the package.
RUN python - <<'PY'
from indextts.utils.front import TextNormalizer
TextNormalizer().load()
print("text normalizer cache ready")
PY

# Fail the build if a dependency swapped in a non-ROCm torch or the v2.5
# inference module cannot import. This is the same guard the XTTS image uses.
RUN python -c "import torch, torchaudio; assert torch.version.hip, torch.__version__; import indextts.infer_v2_5; print('torch', torch.__version__, 'hip', torch.version.hip, 'torchaudio', torchaudio.__version__)"

COPY generate.py /opt/omv/generate.py
COPY tts-indextts/entrypoint.sh /usr/local/bin/omv-generate
RUN chmod 755 /usr/local/bin/omv-generate
WORKDIR /models
ENTRYPOINT ["/usr/local/bin/omv-generate"]
