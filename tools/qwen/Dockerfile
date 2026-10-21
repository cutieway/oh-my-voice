# Qwen3-TTS (Base, voice clone) on ROCm. The host only needs the amdgpu
# driver; do not let pip replace the base image's torch/torchaudio with
# PyPI CUDA/CPU wheels.
ARG BASE_IMAGE=rocm/pytorch:rocm7.2.4_ubuntu22.04_py3.10_pytorch_release_2.8.0@sha256:880e126d83370e3502b069a39f85cbd7b1f6f7dbfbceb00b6fefbac03c5da091
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/cache/home \
    HF_HOME=/cache/hf \
    TORCH_HOME=/cache/torch \
    OMV_QWEN_MODEL_DIR=/models/qwen3-tts/Qwen3-TTS-12Hz-1.7B-Base

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 sox git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# qwen-tts is pure Python. Install it without deps first so pip cannot pull a
# PyPI torch/torchaudio over the ROCm pair, then install its runtime deps.
RUN python -m pip install --no-cache-dir --no-deps qwen-tts==0.1.1
RUN python -m pip install --no-cache-dir \
      "transformers==4.57.3" "accelerate==1.12.0" \
      librosa soundfile sox onnxruntime einops

# Fail the build if torch was swapped for a CUDA/CPU wheel or the inference
# module cannot import.
RUN python -c "import torch, torchaudio; assert torch.version.hip, torch.__version__; from qwen_tts import Qwen3TTSModel; print('torch', torch.__version__, 'hip', torch.version.hip, 'torchaudio', torchaudio.__version__)"

COPY generate.py /opt/omv/generate.py
COPY entrypoint.sh /usr/local/bin/omv-generate
RUN chmod 755 /usr/local/bin/omv-generate
WORKDIR /models
ENTRYPOINT ["/usr/local/bin/omv-generate"]
