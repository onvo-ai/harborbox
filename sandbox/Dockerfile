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

# Register the kernelspec, for the images that have a kernel to register.
# Installing ipykernel provides the kernel but does not advertise it; without
# this the Jupyter server starts clean and execd retries "no kernel specs
# found" until the execution times out. `--sys-prefix` writes it inside the
# venv, so it survives the ephemeral JUPYTER_DATA_DIR set below.
RUN if /opt/venv/bin/python -c "import ipykernel" 2>/dev/null; then \
      /opt/venv/bin/python -m ipykernel install --sys-prefix --name python3 --display-name "Python 3"; \
      /opt/venv/bin/python -m ipykernel install --sys-prefix --name python --display-name "Python"; \
    fi

WORKDIR /workspace
USER 10001:10001

# No harborbox agent and no fastapi/uvicorn: opensandbox injects execd, which is
# what serves code now. What execd does *not* bring is a Python kernel — it runs
# bash directly but proxies Python to a Jupyter server it is told about through
# JUPYTER_HOST/JUPYTER_TOKEN, and it sets neither itself. Nor does the
# opensandbox server. So the image has to run one.
#
# Without it everything looks healthy: the container starts, execd starts and
# listens, the sandbox passes its health check, `/code` answers 200 for bash —
# and every Python execution returns 500. execd's only hint is one line at
# startup, "Jupyter server host is: " with nothing after it.
#
# Bound to loopback deliberately. opensandbox publishes execd's port to the
# host; this one is reachable only from inside the sandbox, which is why a
# fixed token is not a secret worth rotating. Jupyter reads JUPYTER_TOKEN
# itself, so the same variable configures the server and tells execd.
# JUPYTER_TOKEN is set but Jupyter's own auth is off, and both halves are
# needed. execd treats an empty token as "no Python runtime configured" and
# fails with `language runtime server not configured` before it ever connects;
# but given a token it still calls /api/kernelspecs unauthenticated, collects
# Jupyter's 403 and reports `no kernel specs found`, which reads like a missing
# kernel and is really a rejected request. So execd gets a token to satisfy its
# own check, and the server is told not to require one.
#
# The flags below override this variable — traitlets prefers the command line
# over the environment, which is what makes the pair work.
#
# Not a hole: the server listens on loopback inside the sandbox and is
# reachable only by execd in that same container, which is the trust boundary
# execd already sits on.
ENV JUPYTER_HOST=http://127.0.0.1:8888 \
    JUPYTER_TOKEN=harborbox-sandbox-local \
    JUPYTER_RUNTIME_DIR=/tmp/jupyter-runtime \
    JUPYTER_DATA_DIR=/tmp/jupyter-data

CMD ["jupyter", "server", \
     "--ip=127.0.0.1", \
     "--port=8888", \
     "--no-browser", \
     "--IdentityProvider.token=", \
     "--ServerApp.token=", \
     "--ServerApp.password=", \
     "--ServerApp.disable_check_xsrf=True"]
