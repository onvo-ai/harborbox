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

# Local runs keep the PKI in the checkout rather than /data, which needs root.
# Both compose files read this variable, so exporting it here is what makes the
# two projects agree on where the certificates are.
export HARBORBOX_BUILDKIT_TLS_DIR="${HARBORBOX_BUILDKIT_TLS_DIR:-$PWD/.buildkit-tls}"   # read by gen-buildkit-certs.sh only

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

say "2. The two things both Compose projects share"
# The builder is its own project (compose.builder.yaml) and the network it
# meets the registry on therefore belongs to neither, so it is created out of
# band and declared external in both files. Same for the mutual-TLS material
# the API needs to drive buildkitd over TCP: one half goes to each project.
docker network create harborbox-build >/dev/null 2>&1 || true
ok "network harborbox-build"
./scripts/gen-buildkit-certs.sh | sed 's/^/  /'

say "3. Start the builder, in its own project"
# Its own project because a build step runs inside buildkitd's network
# namespace: every network the builder container joins is one a caller's `RUN`
# can reach, including the one an orchestrator appends to every service of an
# application. Keeping it alone means that appended network reaches nothing.
docker compose -f compose.builder.yaml -f compose.builder.local.yaml up -d --wait
ok "rootless builder, on harborbox-build only"

say "4. Start the rest of the stack"
HARBORBOX_REGISTRY_PASSWORD="$REGISTRY_PASS" docker compose up -d registry buildkit-gateway postgres opensandbox
ok "registry, buildkit gateway, postgres, opensandbox"

say "5. Build the base image and push it to the local registry"
# Pushed, not just built: the builder resolves a product's FROM over its own
# network and cannot see the host daemon's image store.
# HARBORBOX_REGISTRY_PASSWORD, on a command that builds no registry: bake reads
# compose.yaml as a bake definition alongside docker-bake.hcl, so it has to
# interpolate registry-auth's `${HARBORBOX_REGISTRY_PASSWORD:?}` before it can
# decide the file holds no target it was asked for. Without it this dies on
# "required variable ... is missing a value" from a step that only builds the
# base image.
HARBORBOX_REGISTRY_PASSWORD="$REGISTRY_PASS" TEMPLATE_REGISTRY=127.0.0.1:5050 \
  docker buildx bake harborbox-templates --load >/dev/null
CFG=$(mktemp -d)
printf '{"auths":{"127.0.0.1:5050":{"auth":"%s"}}}' \
  "$(printf '%s:%s' "$REGISTRY_USER" "$REGISTRY_PASS" | base64)" > "$CFG/config.json"
# DOCKER_CONFIG holds the CLI's *context* store as well as its credentials, so
# pointing it at a throwaway directory also loses the active context and the
# CLI falls back to unix:///var/run/docker.sock -- which OrbStack does not
# create. Pin the endpoint the active context resolves to.
DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}')" \
  DOCKER_CONFIG="$CFG" docker push -q 127.0.0.1:5050/harborbox-sandbox-base:local
rm -rf "$CFG"
ok "$(curl -s -u "$REGISTRY_USER:$REGISTRY_PASS" http://localhost:5050/v2/_catalog)"

say "6. Build and start the API"
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

say "7. Prove it: a Dockerfile of your own, end to end"
HARBORBOX_API_KEY="$API_KEY" ./scripts/try-custom-template.sh "$BASE"

say "Done"
cat <<EOF
  The stack is still up.

    $BASE/docs        the OpenAPI page
    docker compose logs -f api
    docker compose down -v && docker compose -f compose.builder.yaml -f compose.builder.local.yaml down -v
                      tear both projects down, volumes included

  The certificate volumes and the harborbox-build network survive that on
  purpose -- they are shared between the projects and are not either one's to
  delete. Remove them by hand if you want a genuinely clean slate:

    rm -rf .buildkit-tls   # the certificates; ./scripts/gen-buildkit-certs.sh reissues them
    docker network rm harborbox-build

  To see what a caller's build step can actually reach -- the property the
  two-project split exists for, measured rather than read off a compose file:

    HARBORBOX_API_KEY=$API_KEY uv run pytest -m e2e tests/e2e_build_isolation.py

  To keep an image warm, put its template name in HARBORBOX_WARM_POOL:
    HARBORBOX_WARM_POOL='{"base":1,"custom-<hash>":2}'
  Measured on a laptop that takes first-command latency from ~1.5s to ~0.3s.
EOF
