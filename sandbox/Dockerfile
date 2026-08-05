FROM python:3.12-slim-bookworm

ARG SANDBOX_REQUIREMENTS=sandbox/requirements.txt
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

WORKDIR /workspace
USER 10001:10001

# No agent and no entrypoint of our own: opensandbox injects execd and runs the
# code, which is why the requirements files dropped fastapi and uvicorn. This
# matches Dockerfile.relaydeck.
#
# Rebasing 0.2.0 onto main briefly left main's `COPY src/harborbox_agent` and
# forkrun.py here alongside this CMD. The image built fine and started fine —
# it just sat doing nothing, because the agent was present and never launched,
# and every execution failed with "sandbox agent did not become ready".
CMD ["tail", "-f", "/dev/null"]
