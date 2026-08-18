#!/usr/bin/env bash
#
# Generate the mutual-TLS material the API needs to drive the rootless builder.
#
#   ./scripts/gen-buildkit-certs.sh [--force] [--days 3650] [--san name,...]
#
# Since the builder moved into its own Compose project (compose.builder.yaml)
# the API can no longer reach buildkitd over a unix socket in a shared volume:
# a socket needs one project and one host. It dials TCP instead, and a
# buildkitd on TCP with no authentication would be strictly worse than the
# socket it replaces -- a caller's build step runs in buildkitd's own network
# namespace, so it can reach the gateway that fronts that port. A client
# certificate is what stops reachability from being the same as access.
#
# Three volumes, because who holds which half matters:
#
#   harborbox-buildkit-ca          ca.pem + ca-key.pem. Mounted by nothing. The
#                                  CA key never enters a running container, so
#                                  neither the builder nor the API can mint a
#                                  new client certificate.
#   harborbox-buildkit-tls-server  ca.pem, cert.pem, key.pem for buildkitd.
#                                  Mounted read-only by `builder`, owned by uid
#                                  1000 because rootless buildkitd runs as that
#                                  user.
#   harborbox-buildkit-tls-client  ca.pem, cert.pem, key.pem for the API.
#                                  Mounted read-only by `api`.
#
# Volumes rather than a bind mount of a generated directory, for the reason the
# registry's htpasswd stopped being one: a bind mount of a path that does not
# exist makes Docker create an empty *directory*, and the service then dies on
# a confusing error about a file it cannot read. A missing volume fails the
# `up` by name instead.
#
# Nothing this writes is in the repository, and nothing it writes should be.
# Re-running is a no-op unless --force; --force reissues the whole PKI, which
# means restarting both `builder` and `api` (the old client certificate stops
# verifying the moment the CA changes).
set -euo pipefail

FORCE=0
DAYS=3650
# The names a client may dial buildkitd by. `buildkit-gateway` is the service
# in the Harborbox project that fronts the TCP port; `builder` is the service
# itself, for anything on the build network; localhost covers a port-forwarded
# debug session. Go verifies the name the client dialled against these, so a
# name missing here fails the handshake rather than the authorization.
SANS="DNS:buildkit-gateway,DNS:builder,DNS:harborbox-builder,DNS:localhost,IP:127.0.0.1"
# The image is the API's own base, so it is normally already pulled. Debian
# slim carries the openssl CLI; the alpine images this stack uses do not, and
# generating on the host instead would depend on whichever openssl the
# developer's machine ships (macOS ships LibreSSL, whose -addext support has
# varied).
IMAGE="${HARBORBOX_CERT_IMAGE:-python:3.12-slim-bookworm}"

while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1; shift ;;
    --days) DAYS="$2"; shift 2 ;;
    --san) SANS="$SANS,$2"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Directories on the host, not Docker volumes. Coolify rewrites a service's
# named-volume reference to `<app-uuid>_<name>` and creates that volume empty,
# so the builder mounted an empty `/certs` and died on
# `open /certs/cert.pem: no such file or directory`. Neither `external: true`
# nor an explicit `name:` on the declaration prevents it. Bind mounts are
# passed through untouched -- the same reason the docker socket reaches
# opensandbox -- so the PKI lives at a path both applications mount.
#
# Must be absolute: Coolify deploys from an ephemeral checkout under
# /artifacts/<uuid>, so a relative path would resolve inside a directory that
# is deleted after the build. Local runs override it -- see
# scripts/try-locally.sh.
TLS_DIR="${HARBORBOX_BUILDKIT_TLS_DIR:-/data/harborbox/buildkit-tls}"

case "$TLS_DIR" in
  /*) ;;
  *) TLS_DIR="$(cd "$(dirname "$TLS_DIR")" && pwd)/$(basename "$TLS_DIR")" ;;
esac

mkdir -p "$TLS_DIR/ca" "$TLS_DIR/server" "$TLS_DIR/client"
# The CA private key is the one thing here that must not be world-readable;
# the server and client directories are mounted read-only by their services.
chmod 700 "$TLS_DIR/ca"

docker run --rm \
  -e FORCE="$FORCE" -e DAYS="$DAYS" -e SANS="$SANS" \
  -v "$TLS_DIR/ca:/ca" \
  -v "$TLS_DIR/server:/server" \
  -v "$TLS_DIR/client:/client" \
  "$IMAGE" sh -eu -c '
    if [ -s /server/cert.pem ] && [ -s /client/cert.pem ] && [ "$FORCE" != 1 ]; then
      echo "certificates already present; pass --force to reissue"
      openssl x509 -in /server/cert.pem -noout -subject -enddate -ext subjectAltName
      exit 0
    fi

    work=$(mktemp -d)
    cd "$work"

    if [ ! -s /ca/ca-key.pem ] || [ "$FORCE" = 1 ]; then
      openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS" -nodes \
        -keyout /ca/ca-key.pem -out /ca/ca.pem \
        -subj "/CN=harborbox-buildkit-ca" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
      chmod 600 /ca/ca-key.pem
      echo "issued a new CA"
    fi

    printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n" \
      "$SANS" > server.ext
    printf "extendedKeyUsage=clientAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n" \
      > client.ext

    openssl req -newkey rsa:4096 -nodes -keyout server-key.pem -out server.csr \
      -subj "/CN=harborbox-buildkit" 2>/dev/null
    openssl x509 -req -in server.csr -CA /ca/ca.pem -CAkey /ca/ca-key.pem \
      -CAcreateserial -days "$DAYS" -sha256 -extfile server.ext \
      -out server.pem 2>/dev/null

    openssl req -newkey rsa:4096 -nodes -keyout client-key.pem -out client.csr \
      -subj "/CN=harborbox-api" 2>/dev/null
    openssl x509 -req -in client.csr -CA /ca/ca.pem -CAkey /ca/ca-key.pem \
      -CAcreateserial -days "$DAYS" -sha256 -extfile client.ext \
      -out client.pem 2>/dev/null

    install -m 0644 /ca/ca.pem   /server/ca.pem
    install -m 0644 server.pem   /server/cert.pem
    install -m 0600 server-key.pem /server/key.pem
    # Rootless buildkitd runs as uid 1000 and reads these before it drops into
    # its user namespace; root-owned 0600 key would leave it unable to start
    # its TCP listener at all.
    chown -R 1000:1000 /server

    install -m 0644 /ca/ca.pem   /client/ca.pem
    install -m 0644 client.pem   /client/cert.pem
    install -m 0600 client-key.pem /client/key.pem

    rm -rf "$work"
    echo "issued server and client certificates"
    openssl x509 -in /server/cert.pem -noout -subject -enddate -ext subjectAltName
  '

cat <<'EOF'

  Written under $TLS_DIR:

    ca/       the CA and its key, mounted by no service, mode 700
    server/   mounted read-only at /certs by `builder`
    client/   mounted read-only at /certs/buildkit by `api`

  Both applications bind-mount those paths, so the same directory must exist
  on whichever host runs them. Set HARBORBOX_BUILDKIT_TLS_DIR to move it, and
  set it identically for both applications.

  Reissuing (--force) invalidates the running pair: restart both projects.
EOF
