FROM python:3.12-slim-bookworm

ARG SANDBOX_REQUIREMENTS=sandbox/requirements.txt

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

COPY ${SANDBOX_REQUIREMENTS} /tmp/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY src/harborbox_agent /opt/harborbox_agent

WORKDIR /workspace
USER 10001:10001
EXPOSE 8080

ENTRYPOINT ["python", "-m", "uvicorn", "harborbox_agent.main:app", "--app-dir", "/opt", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
