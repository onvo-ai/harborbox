# The one image Harborbox ships.
#
# It is deliberately almost empty. Harborbox used to build a tailored image per
# product -- onvo-pro, onvo-lite, relaydeck -- each pinning that product's own
# Python dependencies in this repository. That put every product's dependency
# upgrades behind a Harborbox release, and it meant this repo had to know what
# pandas version Onvo wanted. Products now own a Dockerfile and POST it; this
# image exists to be something worth starting FROM, and to give the warm pool
# somewhere to live.
#
# What is here is exactly the runtime contract a sandbox needs and nothing
# else: uid/gid 10001, a writable /workspace, and the certificates any build
# step will want the moment it fetches anything. Adding a language runtime here
# would be re-creating the per-product images this replaced.
#
# `render_dockerfile` appends the same user/workspace layer to every template,
# so a product Dockerfile that starts somewhere else entirely still ends up
# conformant. This image only makes that free rather than a rebuild.
FROM debian:bookworm-slim

ENV HOME=/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --home-dir /workspace --no-create-home sandbox \
    && mkdir -p /workspace \
    && chown 10001:10001 /workspace

WORKDIR /workspace
USER 10001:10001

# Tells Coolify's nightly cleanup to leave this image alone. `CleanupDocker`
# runs a plain `docker rmi` over every image that is neither named after an
# application repo nor carrying this label. Less load-bearing than it was --
# images come back from the registry now -- but a pointless re-pull is still a
# pointless re-pull.
LABEL coolify.managed=true

CMD ["tail", "-f", "/dev/null"]
