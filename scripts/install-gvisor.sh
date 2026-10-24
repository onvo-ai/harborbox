#!/usr/bin/env bash
# Install gVisor's runsc on a Debian or Ubuntu Docker host and register it with
# Docker as the `runsc` runtime, which OPENSANDBOX_GVISOR=1 makes OpenSandbox
# use for every sandbox. Run as root, once per host. Safe to re-run.
#
# runsc's default platform, systrap, needs no KVM, so this works on ordinary
# cloud VMs such as Hetzner Cloud.
#
# --overlay2=none is not optional for Harborbox. runsc's default keeps a
# sandbox's root filesystem writes in its own memory-backed file store, which
# `docker commit` cannot see. Cold pause snapshots the container with
# `docker commit`, so under the default a snapshot reports READY and then
# restores none of the sandbox's writes. With overlay2 off, writes land in the
# container's normal writable layer, where the snapshot finds them.
# OpenSandbox 1.2 refuses that snapshot outright; 1.1.1, which we pin, does
# not, so this flag is the only thing standing between cold pause and silent
# data loss. tests/e2e_smoke.py's cold-pause check is what catches it.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root: it writes /etc/apt and /etc/docker/daemon.json" >&2
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo "this script supports Debian and Ubuntu hosts only" >&2
  exit 1
fi
if ! command -v docker >/dev/null; then
  echo "Docker is not installed" >&2
  exit 1
fi

# gVisor's own apt repository, signed with its published key.
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://gvisor.dev/archive.key \
  | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  > /etc/apt/sources.list.d/gvisor.list
apt-get update -qq
apt-get install -y -qq runsc

# `runsc install` merges a `runsc` entry into daemon.json and leaves every
# other key alone. Arguments after `--` become the runtime's runtimeArgs.
runsc install --runtime=runsc -- --overlay2=none
systemctl restart docker

# Prove it: gVisor's kernel answers `dmesg` with its own boot lines.
if docker run --rm --runtime=runsc busybox dmesg | grep -q gVisor; then
  echo "runsc is registered and working: $(runsc --version | head -1)"
else
  echo "runsc is installed but a test container did not run under gVisor" >&2
  exit 1
fi
