variable "TEMPLATE_IMAGE_PREFIX" {
  default = "harborbox-sandbox"
}

# The registry these get pushed to, and the reason it is separate from
# TEMPLATE_IMAGE_PREFIX rather than folded into it: Settings builds the same
# reference as <pull endpoint>/<prefix>-<template>:<version>, so the host part
# has to be its own field on both sides or the two spellings drift. Empty
# builds unqualified local names, which is the no-registry fallback.
#
# Static bases must reach the registry, not just the local daemon: the API's
# builder resolves a derived template's FROM over its own network and cannot
# see the host daemon's image store.
variable "TEMPLATE_REGISTRY" {
  default = ""
}

# "127.0.0.1:5050/" or "".
TEMPLATE_REGISTRY_PREFIX = notequal("", TEMPLATE_REGISTRY) ? "${TEMPLATE_REGISTRY}/" : ""

variable "TEMPLATE_VERSION" {
  default = "local"
}

# The one image this repository builds. Product templates (POST /v1/templates)
# are built at runtime by the API from a Dockerfile the product sent, so they
# are deliberately not bake targets. Rebuilding this base does not propagate to
# templates already built on it; bump TEMPLATE_VERSION to force that.
group "harborbox-templates" {
  targets = ["base"]
}

target "base" {
  context = "."
  dockerfile = "sandbox/Dockerfile"
  tags = ["${TEMPLATE_REGISTRY_PREFIX}${TEMPLATE_IMAGE_PREFIX}-base:${TEMPLATE_VERSION}"]
}
