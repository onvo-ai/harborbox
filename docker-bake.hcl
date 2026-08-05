variable "TEMPLATE_IMAGE_PREFIX" {
  default = "harborbox-sandbox"
}

variable "TEMPLATE_VERSION" {
  default = "local"
}

# The static base templates only. Derived templates (POST /v1/templates) are
# built at runtime by the API from a generated Dockerfile that FROMs one of the
# images below, so they are deliberately not bake targets. Rebuilding a base
# here does not propagate to already-built derived images; bump TEMPLATE_VERSION
# to force that.
group "harborbox-templates" {
  targets = ["relaydeck", "onvo-pro", "onvo-lite"]
}

target "template-base" {
  context = "."
  dockerfile = "sandbox/Dockerfile"
}

target "relaydeck" {
  context = "."
  dockerfile = "sandbox/Dockerfile.relaydeck"
  tags = ["${TEMPLATE_IMAGE_PREFIX}-relaydeck:${TEMPLATE_VERSION}"]
}

target "onvo-pro" {
  inherits = ["template-base"]
  args = {
    SANDBOX_REQUIREMENTS = "sandbox/requirements-onvo-pro.txt"
  }
  tags = ["${TEMPLATE_IMAGE_PREFIX}-onvo-pro:${TEMPLATE_VERSION}"]
}

target "onvo-lite" {
  inherits = ["template-base"]
  args = {
    SANDBOX_REQUIREMENTS = "sandbox/requirements-onvo-lite.txt"
  }
  tags = ["${TEMPLATE_IMAGE_PREFIX}-onvo-lite:${TEMPLATE_VERSION}"]
}
