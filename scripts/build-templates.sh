#!/usr/bin/env bash
set -euo pipefail

export TEMPLATE_IMAGE_PREFIX="${HARBORBOX_TEMPLATE_IMAGE_PREFIX:-harborbox-sandbox}"
export TEMPLATE_VERSION="${HARBORBOX_TEMPLATE_VERSION:-local}"

docker buildx bake harborbox-templates "$@"
