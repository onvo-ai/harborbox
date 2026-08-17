#!/usr/bin/env bash
set -euo pipefail

export TEMPLATE_IMAGE_PREFIX="${HARBORBOX_TEMPLATE_IMAGE_PREFIX:-harborbox-sandbox}"
export TEMPLATE_VERSION="${HARBORBOX_TEMPLATE_VERSION:-local}"
# Same endpoint the API stores in template rows and hands to opensandbox, so
# what this pushes is exactly what a sandbox create later pulls. Unset builds
# unqualified local names.
export TEMPLATE_REGISTRY="${HARBORBOX_REGISTRY_PULL_ENDPOINT:-}"

docker buildx bake harborbox-templates "$@"
