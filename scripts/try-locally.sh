#!/usr/bin/env bash
#
# Bring up the new Harborbox and prove it works, from nothing.
#
#   ./scripts/try-locally.sh
#
# Starts the stack, builds and pushes the base image, then runs a template
# built from a Dockerfile with a build context and executes a command in a
# sandbox created from it. Prints every API call and what it cost.
#
# Leaves the stack running so you can poke at it; `docker compose down -v`
# when you are done.
set -euo pipefail

cd "$(dirname "$0")/.."

REGISTRY_USER="${HARBORBOX_REGISTRY_USERNAME:-harborbox}"
REGISTRY_PASS="${HARBORBOX_REGISTRY_PASSWORD:-change-me-registry}"
API_KEY="${HARBORBOX_API_KEY:-change-me}"
PORT="${HARBORBOX_PORT:-8000}"
BASE="http://localhost:${PORT}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }

say "1. Registry credentials"
if [ ! -f auth/htpasswd ]; then
  mkdir -p auth
  docker run --rm httpd:2-alpine htpasswd -Bbn "$REGISTRY_USER" "$REGISTRY_PASS" > auth/htpasswd
  ok "wrote auth/htpasswd"
else
  ok "auth/htpasswd already present"
fi

say "2. Start the stack"
HARBORBOX_REGISTRY_PASSWORD="$REGISTRY_PASS" docker compose up -d registry builder postgres opensandbox
ok "registry, rootless builder, postgres, opensandbox"

say "3. Build the base image and push it to the local registry"
# Pushed, not just built: the builder resolves a product's FROM over its own
# network and cannot see the host daemon's image store.
TEMPLATE_REGISTRY=127.0.0.1:5050 docker buildx bake harborbox-templates --load >/dev/null
CFG=$(mktemp -d)
printf '{"auths":{"127.0.0.1:5050":{"auth":"%s"}}}' \
  "$(printf '%s:%s' "$REGISTRY_USER" "$REGISTRY_PASS" | base64)" > "$CFG/config.json"
DOCKER_CONFIG="$CFG" docker push -q 127.0.0.1:5050/harborbox-sandbox-base:local
rm -rf "$CFG"
ok "$(curl -s -u "$REGISTRY_USER:$REGISTRY_PASS" http://localhost:5050/v2/_catalog)"

say "4. Build and start the API"
HARBORBOX_REGISTRY_PASSWORD="$REGISTRY_PASS" docker compose up -d --build api
healthy=""
for _ in $(seq 1 60); do
  if [ "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health" || true)" = 200 ]; then
    healthy=yes
    break
  fi
  sleep 2
done
# Say so, rather than printing a tick and failing three lines later on a
# connection refused -- which is exactly what the first version of this did.
if [ -z "$healthy" ]; then
  printf '\n\033[31mThe API never became healthy.\033[0m Last 20 log lines:\n\n'
  docker compose logs --tail 20 api
  exit 1
fi
ok "healthy at $BASE  (docs at $BASE/docs)"

say "5. Prove it: a Dockerfile of your own, end to end"
HARBORBOX_API_KEY="$API_KEY" ./scripts/try-custom-template.sh "$BASE"

say "Done"
cat <<EOF
  The stack is still up.

    $BASE/docs        the OpenAPI page
    docker compose logs -f api
    docker compose down -v   tear it all down, volumes included

  To keep an image warm, put its template name in HARBORBOX_WARM_POOL:
    HARBORBOX_WARM_POOL='{"base":1,"custom-<hash>":2}'
  Measured on a laptop that takes first-command latency from ~1.5s to ~0.3s.
EOF
