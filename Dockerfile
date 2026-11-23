# syntax=docker/dockerfile:1
#
# PixieVeil container image.
#
#   docker build -t pixieveil .                          # no defacing (slim)
#   docker build -t pixieveil:cpu  --build-arg VARIANT=cpu  .   # + nnU-Net, CPU torch
#   docker build -t pixieveil:cuda --build-arg VARIANT=cu126 .  # + nnU-Net, CUDA 12 torch
#   (cu118 / cu130 also work; wheel pins mirror install.py)
#
# Config lives in /app/config and everything else (studies, export, logs,
# nnU-Net model) under /app/data — mount both as volumes (see docker-compose.yml).
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG VARIANT=none
# Keep in sync with _TORCH_VERSION / _TORCHVISION_VERSION in install.py.
ARG TORCH_VERSION=2.14.1
ARG TORCHVISION_VERSION=0.29.1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 pixieveil

WORKDIR /app

# Dependencies first (cacheable layer), then the code.
COPY pyproject.toml README.md LICENSE ./
COPY pixieveil ./pixieveil
RUN pip install .

# Optional defacing stack: torch from the matching wheel index, then the extra.
RUN case "$VARIANT" in \
      none) echo "defacing stack not installed" ;; \
      cpu)  pip install "torch==${TORCH_VERSION}+cpu" "torchvision==${TORCHVISION_VERSION}+cpu" \
                 --index-url https://download.pytorch.org/whl/cpu \
            && pip install ".[deface]" ;; \
      cu*)  pip install "torch==${TORCH_VERSION}+${VARIANT}" "torchvision==${TORCHVISION_VERSION}+${VARIANT}" \
                 --index-url "https://download.pytorch.org/whl/${VARIANT}" \
            && pip install ".[deface]" ;; \
      *)    echo "unknown VARIANT=$VARIANT" >&2; exit 1 ;; \
    esac

COPY pixieveil.py install.py ./
COPY config ./config
COPY integrations ./integrations
RUN rm -f config/settings.yaml config/users.yaml config/protocols.yaml \
          config/rhythm_protocols.yaml config/.session_secret config/.setup_* config/*.bak* \
 && mkdir -p data && chown -R pixieveil:pixieveil /app/config /app/data

USER pixieveil
VOLUME ["/app/config", "/app/data"]
EXPOSE 4070 8070

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8070/health', timeout=4).status==200 else 1)"

# tini forwards SIGTERM so StorageManager.stop() / Defacer.cancel_all() run.
ENTRYPOINT ["tini", "--"]
CMD ["python", "pixieveil.py"]
