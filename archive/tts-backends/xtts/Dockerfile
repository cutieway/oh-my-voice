# Pin the ROCm/Torch userspace so the host only needs the amdgpu driver and
# /dev/kfd + /dev/dri. Tag must support gfx1200 (RX 9060 XT): ROCm >= 7.0.
# Digest is the immutable part; the tag is for humans.
ARG BASE_IMAGE=rocm/pytorch:rocm7.2.4_ubuntu24.04_py3.12_pytorch_release_2.8.0@sha256:d909ec7b7251b2882340d66c0001e77f5e7c3848566d2cf81b4c08b34683cd31
FROM ${BASE_IMAGE}

# Torch 2.8 keeps the XTTS reference-loading path off torchcodec (the
# coqui-tts gate only fires on torch >= 2.9). Do not "upgrade" torch here:
# it would replace the image's ROCm build with a PyPI CUDA/CPU wheel.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COQUI_TOS_AGREED=1 \
    HOME=/cache/home \
    TTS_HOME=/cache/tts \
    HF_HOME=/cache/hf \
    TORCH_HOME=/cache/torch

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

COPY tts/requirements.txt /tmp/requirements.txt
RUN python -m pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# Fail the build if the image or a transitive dependency swapped in a
# non-ROCm torch, or if the XTTS reference loader lost torchaudio.
RUN python -c "import torch, torchaudio; assert torch.version.hip, torch.__version__; print('torch', torch.__version__, 'hip', torch.version.hip, 'torchaudio', torchaudio.__version__)"

# Writable by the compose user (UID/GID default 1000); the entrypoint creates
# the cache subdirectories because the /cache bind mount hides image contents.
COPY generate.py /opt/omv/generate.py
COPY tts/entrypoint.sh /usr/local/bin/omv-generate
RUN chmod 755 /usr/local/bin/omv-generate
WORKDIR /opt/omv
ENTRYPOINT ["/usr/local/bin/omv-generate"]
