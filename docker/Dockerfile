# AudioLab — CPU image (works on any x86_64/arm64 Docker host).
# Build from the repository root: docker build -f docker/Dockerfile .
# ffmpeg comes from the bundled imageio-ffmpeg wheel and soundfile ships libsndfile,
# so no apt packages are needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUDIOLAB_DATA_DIR=/data \
    AUDIOLAB_OUTPUT_DIR=/output \
    TORCH_HOME=/data/torch

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./

# Dependencies first (cached layer): a stub package lets pip resolve them from pyproject.toml,
# so code changes later rebuild in seconds instead of re-downloading torch.
# WITH_AI=1 adds torch (CPU build) + demucs for "Best" quality and stem splitting (~1 GB larger).
ARG WITH_AI=1
RUN mkdir audiolab && echo "__version__ = '0.0.0'" > audiolab/__init__.py \
 && if [ "$WITH_AI" = "1" ]; then \
      pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install ".[ai]"; \
    else \
      pip install .; \
    fi \
 && pip uninstall -y audiolab-app && rm -rf audiolab

COPY audiolab ./audiolab
RUN pip install --no-deps .

EXPOSE 8765
VOLUME ["/data", "/output"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/ping', timeout=3)"

# Binding to 0.0.0.0 turns on token protection: the token is printed in the logs
# (docker compose logs audiolab) unless AUDIOLAB_TOKEN is set.
CMD ["audiolab", "serve", "--host", "0.0.0.0", "--port", "8765"]
