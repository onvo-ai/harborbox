FROM python:3.12-slim-bookworm

ARG SANDBOX_REQUIREMENTS=sandbox/requirements-onvo-pro.txt
ARG SANDBOX_APT_PACKAGES=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/workspace \
    PATH=/opt/venv/bin:$PATH

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --home-dir /workspace --no-create-home sandbox \
    && mkdir -p /workspace \
    && chown 10001:10001 /workspace

RUN if [ -n "$SANDBOX_APT_PACKAGES" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends $SANDBOX_APT_PACKAGES \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY ${SANDBOX_REQUIREMENTS} /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The pre-warmed script runner. Onvo does not use a kernel at all — it
# uploads a script and runs it through /v1/sandboxes/{id}/commands — so this,
# not the kernel, is what makes a batch fast: `import pandas` costs ~1.5s and a
# batch runs eight scripts, which forkrun pays once by forking each run from a
# daemon that has already imported. Dropping it in the 0.2.0 rebase is why
# widgets got slower; the client's `[ -f /opt/forkrun.py ]` guard meant they
# still ran, just paying the import every time.
COPY sandbox/forkrun.py /opt/forkrun.py

WORKDIR /workspace
USER 10001:10001

# Tells Coolify's nightly cleanup to leave this image alone.
#
# `CleanupDocker` runs a plain `docker rmi` over every image that is neither
# named after an application repo nor carrying this label. An image built on the
# host has no registry to come back from, so what follows is a sandbox create
# reporting `404 pull access denied` for a name that never existed upstream.
#
# This is the second of two guards and the sturdier one. The keeper container in
# `compose.internal-tools.yaml` also protects the image, but only for as long as
# it is running — `docker stop` on it and the next 00:00 UTC cleanup wins. This
# label holds regardless of container state.
LABEL coolify.managed=true

# opensandbox discards the image CMD and runs whatever the create request
# passes (see Settings.entrypoint_for_template), so this only matters when the
# image is run directly. Idle either way: nothing in the sandbox needs a
# long-lived process of its own.
CMD ["tail", "-f", "/dev/null"]
