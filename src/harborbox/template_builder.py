from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import timedelta

from sqlalchemy import select

from harborbox.config import Settings
from harborbox.db import session_factory
from harborbox.models import Sandbox, SandboxTemplate, utc_now
from harborbox.templates import TemplateSpec, render_dockerfile

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 4000
BUILD_LOG_TAIL_LINES = 20
TERMINAL_SANDBOX_STATES = ("killed", "failed")


class TemplateBuildError(RuntimeError):
    pass


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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule_build(self, name: str) -> None:
        task = asyncio.create_task(
            self._build(name), name=f"harborbox-template-build-{name}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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
            image = template.image

        try:
            base_image = self.settings.image_for_template(spec.base)
        except KeyError:
            await self._record_failure(name, f"unknown base template: {spec.base}")
            return

        dockerfile = render_dockerfile(base_image=base_image, spec=spec)
        try:
            await asyncio.to_thread(self._build_sync, dockerfile, image)
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
        try:
            completed = subprocess.run(
                ["docker", *arguments],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.settings.template_build_timeout_seconds,
                env=self._docker_env(),
                check=False,
            )
        except FileNotFoundError as exc:
            message = "the docker CLI is not installed in the Harborbox API image"
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

    def _build_sync(self, dockerfile: str, image: str) -> None:
        """Build through BuildKit, with the Dockerfile on stdin and no context.

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
        await asyncio.to_thread(self._remove_image_sync, image)

    def _remove_image_sync(self, image: str) -> None:
        try:
            self._run_docker(["image", "rm", "--force", image])
        except TemplateBuildError as exc:
            # A retained image is a wasted layer, not a correctness problem; the
            # row is still dropped so the sweep does not stall behind it.
            logger.warning("Could not remove template image %s: %s", image, exc)
