from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import select

from harborbox.build_contexts import BuildContextError, BuildContextStore
from harborbox.db import session_factory
from harborbox.models import Sandbox, SandboxTemplate, utc_now
from harborbox.templates import TemplateSpec, render_dockerfile

if TYPE_CHECKING:
    from harborbox.config import Settings

logger = logging.getLogger(__name__)

# A registry serves whichever manifest media type the client says it accepts.
# Ask for all four: omitting them makes a v2 registry answer with a schema-1
# manifest whose digest does not match the one the image was pushed under, and
# the delete that follows would 404.
_MANIFEST_MEDIA_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)

MAX_ERROR_LENGTH = 4000
BUILD_LOG_TAIL_LINES = 20
TERMINAL_SANDBOX_STATES = ("killed", "failed")

# Resolved once at import time from the Harborbox API image's own PATH (the
# image installs docker-ce-cli via apt; see Dockerfile.api), not from
# caller-supplied input, so an absolute path is used wherever it can be
# found. `arguments` passed to `_run_docker` below come only from other
# methods on this class, never from request bodies.
_DOCKER_BIN = shutil.which("docker") or "docker"
# Same reasoning for buildctl, which the API image installs alongside it when a
# builder is configured. Neither path is caller-supplied.
_BUILDCTL_BIN = shutil.which("buildctl") or "buildctl"


class TemplateBuildError(RuntimeError):
    pass


def _split_reference(image: str) -> tuple[str, str]:
    """Split an image reference into its repository path and tag.

    Any registry host is dropped. A leading segment counts as a host only if it
    looks like one -- it carries a dot or a port, or is `localhost` -- which is
    the same rule Docker itself uses to tell `registry:5000/app` from a
    two-segment repository like `library/app`.
    """
    head, slash, rest = image.partition("/")
    looks_like_host = "." in head or ":" in head or head == "localhost"
    repository_and_tag = rest if slash and looks_like_host else image
    repository, _, tag = repository_and_tag.rpartition(":")
    return repository, tag


def build_log_tail(output: str) -> str:
    """Reduce a BuildKit log to something a caller can read in an API response.

    A failed build must leave a message, not a traceback: this keeps the last
    few log lines, which is where the failing `apt-get`/`npm` output lives.
    """
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "the image build failed without producing any output"
    return "\n".join(lines[-BUILD_LOG_TAIL_LINES:])[:MAX_ERROR_LENGTH]


class TemplateBuilder:
    """Builds derived template images and reclaims the unused ones.

    Builds run detached from the request that triggered them; the caller polls
    `GET /v1/templates/{name}` for the persisted status. A build interrupted by
    a control plane restart is not recovered here — a cancelled task cannot
    reliably write its own epitaph — but by `Scheduler._recover_interrupted_jobs`
    on the next startup, alongside interrupted executions.
    """

    def __init__(
        self, settings: Settings, context_store: BuildContextStore | None = None
    ) -> None:
        self.settings = settings
        # Optional so the package-spec path, which never has a context, can
        # still construct a builder without one.
        self.context_store = context_store
        self._tasks: set[asyncio.Task[None]] = set()
        # Builds queue rather than all starting at once. This matters more
        # since caller-supplied Dockerfiles: an allowlisted apt install is
        # bounded work, an arbitrary RUN is not.
        self._slots = asyncio.Semaphore(settings.template_max_concurrent_builds)

    def schedule_build(self, name: str) -> None:
        task = asyncio.create_task(
            self._build_when_a_slot_frees(name),
            name=f"harborbox-template-build-{name}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _build_when_a_slot_frees(self, name: str) -> None:
        """Hold the template at `building` until a build slot is free.

        The row already says `building`, and the caller is already polling, so
        queueing costs nothing legible -- whereas starting every requested
        build at once is how one caller starves the host.
        """
        async with self._slots:
            await self._build(name)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _build(self, name: str) -> None:
        async with session_factory() as session:
            template = await session.get(SandboxTemplate, name)
            if template is None:
                return
            spec = TemplateSpec.from_json(template.spec)
        # Not `template.image`: the row stores the reference opensandbox pulls,
        # which the builder cannot reach from its own network.
        image = self.settings.push_image_for_template(name)

        # The caller wrote the Dockerfile; we only append the runtime contract.
        # Their `FROM` was allowlisted at validation time.
        dockerfile = render_dockerfile(spec.dockerfile)
        try:
            await asyncio.to_thread(
                self._build_sync, dockerfile, image, spec.context_digest
            )
        except BuildContextError as exc:
            # The context was swept, or never uploaded. Say so: as a bare COPY
            # failure this is very hard to diagnose from the build log.
            logger.warning("Template build failed for %s: %s", name, exc)
            await self._record_failure(name, str(exc))
            return
        except TemplateBuildError as exc:
            logger.warning("Template build failed for %s: %s", name, exc)
            await self._record_failure(name, str(exc))
            return
        except Exception as exc:
            # A detached build task must always leave a readable status behind,
            # so anything unforeseen still lands on the row rather than in a
            # traceback nobody polls.
            logger.exception("Unexpected template build failure for %s", name)
            await self._record_failure(name, f"{type(exc).__name__}: {exc}")
            return

        async with session_factory() as session:
            template = await session.get(SandboxTemplate, name)
            if template is not None:
                template.status = "ready"
                template.error = None
                template.updated_at = utc_now()
                await session.commit()
        logger.info("Template %s built as %s", name, image)

    async def _record_failure(self, name: str, error: str) -> None:
        async with session_factory() as session:
            template = await session.get(SandboxTemplate, name)
            if template is None:
                return
            template.status = "failed"
            template.error = error[:MAX_ERROR_LENGTH]
            template.updated_at = utc_now()
            await session.commit()

    def _docker_env(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self.settings.docker_base_url:
            environment["DOCKER_HOST"] = self.settings.docker_base_url
        return environment

    def _run_docker(self, arguments: list[str], stdin: str | None = None) -> str:
        return self._run_tool(_DOCKER_BIN, arguments, stdin=stdin)

    def _run_tool(
        self,
        binary: str,
        arguments: list[str],
        stdin: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> str:
        try:
            # `arguments` is always a literal list built by the build/remove
            # methods on this class, never a caller-supplied string passed
            # through as-is (and shell=True is not used), so there is no
            # shell-injection surface here.
            completed = subprocess.run(  # noqa: S603
                [binary, *arguments],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.settings.template_build_timeout_seconds,
                env=self._docker_env() | (env_overrides or {}),
                check=False,
            )
        except FileNotFoundError as exc:
            tool = Path(binary).name
            message = f"the {tool} CLI is not installed in the Harborbox API image"
            raise TemplateBuildError(message) from exc
        except subprocess.TimeoutExpired as exc:
            timeout = self.settings.template_build_timeout_seconds
            message = f"the image build exceeded {timeout:.0f} seconds"
            raise TemplateBuildError(message) from exc
        except OSError as exc:
            raise TemplateBuildError(str(exc)[:MAX_ERROR_LENGTH]) from exc
        output = f"{completed.stdout}\n{completed.stderr}"
        if completed.returncode != 0:
            raise TemplateBuildError(build_log_tail(output))
        return output

    def _build_sync(
        self, dockerfile: str, image: str, context_digest: str | None = None
    ) -> None:
        """Build the derived image, through the builder if one is configured.

        With `builder_address` set the build runs on a rootless BuildKit daemon
        that holds no Docker socket, and the result is pushed to the registry
        rather than landing in a local image store. Without it, the original
        local-daemon path applies.
        """
        if self.settings.builder_address:
            self._build_with_buildkit(dockerfile, image, context_digest)
            return
        if context_digest is not None:
            message = (
                "a build context requires the rootless builder; "
                "set HARBORBOX_BUILDER_ADDRESS"
            )
            raise TemplateBuildError(message)
        self._build_with_local_daemon(dockerfile, image)

    def _build_with_local_daemon(self, dockerfile: str, image: str) -> None:
        """Build through the local daemon's BuildKit, Dockerfile on stdin, no context.

        Not docker-py: its `images.build` posts to the daemon's classic builder,
        which was removed in Docker Engine 29, where the call hangs rather than
        failing. `docker build -` is also the stronger option — a build with no
        context at all cannot `COPY` or `ADD` anything off the build host, which
        matters for a Dockerfile generated from API input.
        """
        self._run_docker(
            [
                "build",
                "--progress=plain",
                "--pull=false",
                "--tag",
                image,
                "-",
            ],
            stdin=dockerfile,
        )

    def _build_with_buildkit(
        self, dockerfile: str, image: str, context_digest: str | None = None
    ) -> None:
        """Build on the rootless builder and push straight to the registry.

        `buildctl` has no stdin equivalent of `docker build -`, so the
        Dockerfile is written to its own directory and the context is a
        separate directory that is left empty. Empty is the point: it preserves
        what `docker build -` gave for free, namely that a Dockerfile generated
        from API input has nothing on the build host it could `COPY`.
        """
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            dockerfile_dir = root / "dockerfile"
            context_dir = root / "context"
            dockerfile_dir.mkdir()
            context_dir.mkdir()
            (dockerfile_dir / "Dockerfile").write_text(dockerfile)
            if context_digest is not None:
                if self.context_store is None:
                    message = "this deployment stores no build contexts"
                    raise TemplateBuildError(message)
                # The only case where the context is not empty. It holds
                # exactly what the caller uploaded, so the guarantee that a
                # build cannot COPY off the *build host* still holds.
                self.context_store.extract(context_digest, context_dir)
            output = f"type=image,name={image},push=true"
            if self.settings.registry_insecure:
                output = f"{output},registry.insecure=true"
            self._run_tool(
                _BUILDCTL_BIN,
                [
                    "--addr",
                    self.settings.builder_address or "",
                    *self._builder_tls_arguments(),
                    "build",
                    "--progress=plain",
                    "--frontend",
                    "dockerfile.v0",
                    "--local",
                    f"context={context_dir}",
                    "--local",
                    f"dockerfile={dockerfile_dir}",
                    "--output",
                    output,
                ],
                env_overrides=self._registry_credentials(root),
            )

    def _builder_tls_arguments(self) -> list[str]:
        """Return the buildctl flags that authenticate this client to buildkitd.

        Empty for a `unix://` builder, which needs no credentials because
        reaching the socket already required a shared mount namespace.

        For a `tcp://` one they are mandatory, and `Settings` refuses to
        construct without them, so the `if` below is about the socket case
        rather than about tolerating an unauthenticated TCP builder. The
        certificate does two jobs at once: it proves this process may build
        (buildkitd verifies it against its CA), and it proves the daemon
        answering is the one we meant to reach -- which matters more than usual
        here, because the address is a service name resolved by Docker's
        embedded DNS on a network that a caller's build step is also on.
        """
        if not self.settings.builder_is_tcp:
            return []
        arguments = [
            "--tlscacert",
            self.settings.builder_tls_ca_cert or "",
            "--tlscert",
            self.settings.builder_tls_cert or "",
            "--tlskey",
            self.settings.builder_tls_key or "",
        ]
        if self.settings.builder_tls_server_name:
            arguments += ["--tlsservername", self.settings.builder_tls_server_name]
        return arguments

    def _registry_credentials(self, workspace: Path) -> dict[str, str]:
        """Write a throwaway docker config for buildctl, and point it there.

        buildkitd keeps no credentials of its own: the client forwards them
        over the build session, reading them from `DOCKER_CONFIG`. Without
        this the build itself succeeds and the *push* fails, which reads as a
        broken registry rather than a missing password.

        The file holds the password in plaintext, so it lives in the build's
        own temporary directory and goes when that does -- writing it to the
        API image's `~/.docker` would leave it there for the process lifetime.
        """
        if not (self.settings.registry_username and self.settings.registry_password):
            return {}
        secret = f"{self.settings.registry_username}:{self.settings.registry_password}"
        config = {
            "auths": {
                self.settings.registry_push_endpoint or "": {
                    "auth": base64.b64encode(secret.encode()).decode()
                }
            }
        }
        (workspace / "config.json").write_text(json.dumps(config))
        return {"DOCKER_CONFIG": str(workspace)}

    async def collect_unused_templates(self) -> int:
        """Delete derived templates and images no sandbox has used recently."""
        cutoff = utc_now() - timedelta(days=self.settings.template_gc_max_idle_days)
        removed = 0
        async with session_factory() as session:
            stale = list(
                await session.scalars(
                    select(SandboxTemplate).where(
                        SandboxTemplate.status != "building",
                        SandboxTemplate.last_used_at < cutoff,
                    )
                )
            )
            if not stale:
                return 0
            # Metadata is a portable JSON column rather than JSONB, so the
            # in-use set is computed in Python instead of with a dialect-specific
            # JSON path predicate.
            in_use: set[str] = set()
            for metadata in await session.scalars(
                select(Sandbox.metadata_).where(
                    Sandbox.status.not_in(TERMINAL_SANDBOX_STATES)
                )
            ):
                name = (metadata or {}).get("template")
                if name:
                    in_use.add(name)

            for template in stale:
                if template.name in in_use:
                    continue
                await asyncio.to_thread(self._remove_image_sync, template.image)
                await session.delete(template)
                removed += 1
                logger.info(
                    "Garbage collected derived template %s (%s)",
                    template.name,
                    template.image,
                )
            await session.commit()
        return removed

    async def remove_image(self, image: str) -> None:
        if self.settings.registry_push_endpoint:
            await self._remove_from_registry(image)
            return
        await asyncio.to_thread(self._remove_image_sync, image)

    async def _remove_from_registry(self, image: str) -> None:
        """Delete a derived image's manifest from the registry.

        The registry API only deletes by digest, so the tag is resolved first
        with a HEAD. Note this reclaims the manifest, not the blobs underneath
        it: the registry frees those on its own `garbage-collect` pass, which
        is a registry-side concern rather than something Harborbox drives.

        A failure here is logged and swallowed. A retained manifest is wasted
        storage, and stalling the sweep behind it would leave rows for images
        the collector has already decided are dead -- the same call the
        local-daemon path makes for `docker image rm`.

        Only the repository path and tag are taken from `image`; the endpoint
        comes from configuration. `SandboxTemplate.image` is written unqualified
        by `derived_template_image`, while a caller may hand over a pull
        reference, whose host is loopback *on the Docker host* and addresses
        nothing from inside this container. Either way the host is the part to
        discard, and it is discarded rather than assumed present -- reading the
        repository as "everything after the first slash" turns the unqualified
        form into an empty path and a `/v2//manifests/` request.
        """
        repository, tag = _split_reference(image)
        endpoint = self.settings.registry_push_endpoint
        scheme = "http" if self.settings.registry_insecure else "https"
        base = f"{scheme}://{endpoint}/v2/{repository}/manifests"
        auth = (
            (self.settings.registry_username, self.settings.registry_password)
            if self.settings.registry_username and self.settings.registry_password
            else None
        )
        try:
            async with httpx.AsyncClient(
                auth=auth, timeout=self.settings.registry_timeout_seconds
            ) as client:
                head = await client.request(
                    "HEAD",
                    f"{base}/{tag}",
                    headers={"Accept": ", ".join(_MANIFEST_MEDIA_TYPES)},
                )
                head.raise_for_status()
                digest = head.headers.get("Docker-Content-Digest")
                if not digest:
                    logger.warning(
                        "Registry did not report a digest for %s; manifest retained",
                        image,
                    )
                    return
                deleted = await client.request("DELETE", f"{base}/{digest}")
                deleted.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Could not remove template image %s: %s", image, exc)

    def _remove_image_sync(self, image: str) -> None:
        try:
            self._run_docker(["image", "rm", "--force", image])
        except TemplateBuildError as exc:
            # A retained image is a wasted layer, not a correctness problem; the
            # row is still dropped so the sweep does not stall behind it.
            logger.warning("Could not remove template image %s: %s", image, exc)
