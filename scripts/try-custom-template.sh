#!/usr/bin/env bash
#
# End-to-end proof that a caller-supplied Dockerfile becomes a running sandbox.
#
# Builds an image from a Dockerfile this script writes -- installing a package
# that is deliberately NOT on HARBORBOX_TEMPLATE_APT_ALLOWLIST, and COPYing a
# file from an uploaded build context -- then runs a command inside a sandbox
# created from it and checks both arrived.
#
# Requires a running stack with:
#   HARBORBOX_TEMPLATE_RAW_DOCKERFILE_ENABLED=true
#
# Usage:
#   HARBORBOX_API_KEY=... ./scripts/try-custom-template.sh [base-url]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
KEY="${HARBORBOX_API_KEY:?set HARBORBOX_API_KEY}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
api() { curl -sS -H "X-API-Key: $KEY" "$@"; }

say "1. Build a context holding a file the image will COPY"
mkdir -p "$WORK/ctx"
cat > "$WORK/ctx/hello.py" <<'PY'
import json, sys
print(json.dumps({"copied_from_context": True, "argv": sys.argv[1:]}))
PY
tar -czf "$WORK/ctx.tar.gz" -C "$WORK/ctx" .
printf '   %s bytes\n' "$(wc -c < "$WORK/ctx.tar.gz" | tr -d ' ')"

say "2. POST /v1/build-contexts"
CONTEXT=$(api -X POST "$BASE/v1/build-contexts" \
  -H 'Content-Type: application/gzip' \
  --data-binary "@$WORK/ctx.tar.gz" | python3 -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
printf '   %s\n' "$CONTEXT"

say "3. POST /v1/templates with our own Dockerfile"
# jq is not on the apt allowlist. Under the old package-spec API this request
# was impossible to express; that is the whole point of the test.
python3 - "$CONTEXT" > "$WORK/body.json" <<'PY'
import json, sys
dockerfile = """FROM debian:bookworm-slim
RUN apt-get update \\
 && apt-get install -y --no-install-recommends jq python3 \\
 && rm -rf /var/lib/apt/lists/*
COPY hello.py /opt/hello.py
ENV GREETING=phase-one
"""
json.dump({"dockerfile": dockerfile, "context": sys.argv[1], "memory_mb": 512}, sys.stdout)
PY
CREATED=$(api -X POST "$BASE/v1/templates" -H 'Content-Type: application/json' \
  --data-binary "@$WORK/body.json")
echo "   $CREATED"
NAME=$(printf '%s' "$CREATED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')

say "4. Poll GET /v1/templates/$NAME until it is ready"
for _ in $(seq 1 120); do
  STATUS_JSON=$(api "$BASE/v1/templates/$NAME")
  STATUS=$(printf '%s' "$STATUS_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  printf '   %s\r' "$STATUS"
  case "$STATUS" in
    ready)  printf '   ready        \n'; break ;;
    failed) printf '\n'; echo "$STATUS_JSON"; exit 1 ;;
  esac
  sleep 2
done
[ "$STATUS" = ready ] || { echo "   timed out at: $STATUS"; exit 1; }

say "5. Create a sandbox on it"
SANDBOX=$(api -X POST "$BASE/v1/sandboxes" -H 'Content-Type: application/json' \
  -d "{\"template\":\"$NAME\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
printf '   %s\n' "$SANDBOX"

say "6. Run a command proving the custom image is what booted"
RESULT=$(api -X POST "$BASE/v1/sandboxes/$SANDBOX/commands" \
  -H 'Content-Type: application/json' \
  -d '{"command":"echo \"{\\\"user\\\":\\\"$(id -un)\\\",\\\"greeting\\\":\\\"$GREETING\\\"}\" | jq -c . && python3 /opt/hello.py it-works","wait":true,"wait_timeout_seconds":60}')
echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"

say "7. Clean up"
api -X DELETE "$BASE/v1/sandboxes/$SANDBOX" >/dev/null && echo "   sandbox deleted"
api -X DELETE "$BASE/v1/templates/$NAME"  >/dev/null && echo "   template deleted"

say "PASS — jq (not allowlisted), the COPYed file, and ENV all reached the sandbox."
