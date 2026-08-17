from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from harborbox.models import SandboxTemplate, utc_now

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from harborbox.config import Settings

TemplateStatus = str

SANDBOX_USER = "10001:10001"


# What POST /v1/build-contexts hands back: a sha256 hex digest.
CONTEXT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# Appended to every caller-supplied Dockerfile. See render_raw_dockerfile.
CONFORMANCE_LAYER = f"""
# ---- harborbox runtime contract (appended; overrides any trailing USER) ----
USER root
RUN (getent group 10001 || groupadd --gid 10001 sandbox) \\
 && (getent passwd 10001 || useradd --uid 10001 --gid 10001 \\
       --home-dir /workspace --no-create-home sandbox) \\
 && mkdir -p /workspace && chown 10001:10001 /workspace
WORKDIR /workspace
USER {SANDBOX_USER}"""


class TemplateSpecError(ValueError):
    """A caller-supplied template spec that must never reach a Dockerfile."""


class UnknownTemplateError(LookupError):
    pass


class TemplateNotReadyError(RuntimeError):
    def __init__(self, name: str, status: str, error: str | None) -> None:
        detail = f"template {name} is {status}"
        if error:
            detail = f"{detail}: {error}"
        super().__init__(detail)
        self.name = name
        self.status = status
        self.error = error


@dataclass(frozen=True)
class TemplateSpec:
    """A validated Dockerfile, and optionally the context it copies from.

    This is the only way to describe a template. Harborbox used to generate
    Dockerfiles from an allowlisted `{base, apt, npm, env}` spec; products now
    own their images and send the real thing.
    """

    dockerfile: str
    context_digest: str | None = None

    def canonical_json(self) -> str:
        """Serialise the identity of this spec, and nothing else.

        `context` is omitted when absent rather than emitted as null, so adding
        a context to a Dockerfile that never had one is the only thing that
        changes its digest.
        """
        payload: dict[str, Any] = {"dockerfile": self.dockerfile}
        if self.context_digest is not None:
            payload["context"] = self.context_digest
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @property
    def spec_hash(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return digest[:12]

    @property
    def name(self) -> str:
        """The template's name, which is also how its image is addressed.

        `custom-` is its own namespace, deliberately separate from the base
        template: sizing and readiness come from the row, never from anything
        this name is lexically derived from.
        """
        return f"custom-{self.spec_hash}"

    def as_json(self) -> dict[str, Any]:
        return {"dockerfile": self.dockerfile, "context": self.context_digest}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> TemplateSpec:
        context = payload.get("context")
        return cls(
            dockerfile=str(payload["dockerfile"]),
            context_digest=str(context) if context is not None else None,
        )


@dataclass(frozen=True)
class ResolvedTemplate:
    name: str
    base: str
    image: str
    memory_mb: int
    cpu: float
    status: TemplateStatus
    derived: bool
    spec_hash: str | None = None







def validate_template_spec(
    settings: Settings, *, dockerfile: str, context: str | None = None
) -> TemplateSpec:
    """Validate a caller-supplied Dockerfile and canonicalise it.

    The only way to describe a template. There is nothing to allowlist about
    what a build installs -- `RUN` can install anything -- so what is checked
    is the shape of the build: how big the file is, how many instructions it
    has, and above all which registries its `FROM` lines may name. That last
    one is the supply-chain control, and it is the difference between "any
    Dockerfile" and "any Dockerfile starting from an image we vetted".

    Raises `TemplateSpecError` for anything that must not reach the builder.
    """
    if not dockerfile.strip():
        message = "a dockerfile is required"
        raise TemplateSpecError(message)
    size = len(dockerfile.encode("utf-8"))
    if size > settings.template_max_dockerfile_bytes:
        message = (
            f"dockerfile is {size} bytes, over the "
            f"{settings.template_max_dockerfile_bytes} byte limit"
        )
        raise TemplateSpecError(message)
    instructions = _dockerfile_instructions(dockerfile)
    if len(instructions) > settings.template_max_dockerfile_instructions:
        message = (
            f"dockerfile has {len(instructions)} instructions, over the "
            f"{settings.template_max_dockerfile_instructions} limit"
        )
        raise TemplateSpecError(message)
    _validate_from_lines(settings, instructions)
    if context is not None and not CONTEXT_DIGEST_PATTERN.fullmatch(context):
        message = f"invalid build context digest: {context}"
        raise TemplateSpecError(message)
    return TemplateSpec(dockerfile=dockerfile, context_digest=context)


def _dockerfile_instructions(dockerfile: str) -> list[str]:
    """Flatten a Dockerfile into one logical instruction per entry.

    Comments and blanks go; a trailing backslash joins the next line, so a
    wrapped `RUN` counts once rather than once per line. This is not a full
    parser and does not need to be -- it exists to count instructions and to
    find every `FROM`, and both are line-oriented.
    """
    instructions: list[str] = []
    pending = ""
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        instructions.append((pending + line).strip())
        pending = ""
    if pending.strip():
        instructions.append(pending.strip())
    return instructions


def _normalise_image_reference(reference: str) -> str:
    """Expand a reference to the form an allowlist entry is written in.

    Docker's implicit prefixes are the whole point: `debian` means
    `docker.io/library/debian`, and `someuser/debian` means
    `docker.io/someuser/debian`. Matching the text as typed would let the short
    form walk past an allowlist that spells the long one out.
    """
    head = reference.split("/", 1)[0]
    if "/" not in reference:
        return f"docker.io/library/{reference}"
    if "." not in head and ":" not in head and head != "localhost":
        return f"docker.io/{reference}"
    return reference


def _validate_from_lines(settings: Settings, instructions: list[str]) -> None:
    allowlist = settings.effective_from_allowlist
    stages: set[str] = set()
    seen_from = False
    for instruction in instructions:
        parts = instruction.split()
        if not parts or parts[0].upper() != "FROM":
            continue
        seen_from = True
        arguments = [part for part in parts[1:] if not part.startswith("--")]
        if not arguments:
            message = "a FROM instruction names no image"
            raise TemplateSpecError(message)
        reference = arguments[0]
        # `FROM build` refers to an earlier stage in this same file; there is
        # nothing to pull and nothing to allowlist.
        if reference not in stages:
            normalised = _normalise_image_reference(reference)
            repository = normalised.rsplit(":", 1)[0].rsplit("@", 1)[0]
            if not any(
                repository == entry or repository.startswith(f"{entry}/")
                for entry in allowlist
            ):
                message = (
                    f"FROM image is not allowlisted: {reference}. "
                    f"Allowed prefixes: {', '.join(sorted(allowlist))}"
                )
                raise TemplateSpecError(message)
        if len(arguments) >= 3 and arguments[1].upper() == "AS":  # noqa: PLR2004
            stages.add(arguments[2])
    if not seen_from:
        message = "a dockerfile must contain at least one FROM instruction"
        raise TemplateSpecError(message)


def render_dockerfile(dockerfile: str) -> str:
    """Emit a caller's Dockerfile, then append the sandbox runtime contract.

    This is the analogue of E2B injecting `envd` into every template: the
    caller decides what the image contains, Harborbox guarantees that whatever
    they built can still be run as a sandbox. The contract is small, because
    opensandbox injects execd itself -- uid/gid 10001 exists, `/workspace` is
    theirs and is the working directory, and the image ends as that user.

    Appended rather than merged, so a caller's own trailing `USER` does not get
    to decide who the sandbox runs as. Document that, because it is the one
    place their file is overruled.

    `getent` guards both `groupadd` and `useradd`: our own static bases already
    carry uid 10001, and those commands exit non-zero when the account exists,
    which would make "built on a Harborbox base" a build failure.
    """
    return dockerfile.rstrip("\n") + "\n" + CONFORMANCE_LAYER



def static_template(settings: Settings, name: str) -> ResolvedTemplate:
    memory_mb, cpu = settings.template_resources[name]
    return ResolvedTemplate(
        name=name,
        base=name,
        image=settings.template_images[name],
        memory_mb=memory_mb,
        cpu=cpu,
        status="ready",
        derived=False,
    )


def derived_template(template: SandboxTemplate) -> ResolvedTemplate:
    return ResolvedTemplate(
        name=template.name,
        base=template.base,
        image=template.image,
        memory_mb=template.memory_mb,
        cpu=template.cpu,
        status=template.status,
        derived=True,
        spec_hash=template.spec_hash,
    )


async def find_template(
    session: AsyncSession, settings: Settings, name: str
) -> ResolvedTemplate | None:
    """Look a template up by name across static config and the template registry."""
    if name in settings.template_images:
        return static_template(settings, name)
    if not settings.is_custom_template(name):
        return None
    template = await session.get(SandboxTemplate, name)
    return None if template is None else derived_template(template)


async def resolve_template(
    session: AsyncSession, settings: Settings, name: str
) -> ResolvedTemplate:
    """Resolve a template for sandbox creation.

    This is the single registry-aware seam. Everything downstream of it works
    from the resolved image and the sizing already persisted on the sandbox row,
    so no synchronous caller ever has to read the database.
    """
    resolved = await find_template(session, settings, name)
    if resolved is None:
        message = f"unknown sandbox template: {name}"
        raise UnknownTemplateError(message)
    if resolved.status != "ready":
        template = await session.get(SandboxTemplate, name)
        raise TemplateNotReadyError(
            name, resolved.status, template.error if template else None
        )
    return resolved


async def list_derived_templates(session: AsyncSession) -> list[SandboxTemplate]:
    result = await session.scalars(
        select(SandboxTemplate).order_by(SandboxTemplate.created_at)
    )
    return list(result)


async def mark_template_used(session: AsyncSession, resolved: ResolvedTemplate) -> None:
    """Record derived-template usage so image GC can tell live sets from dead ones."""
    if not resolved.derived:
        return
    template = await session.get(SandboxTemplate, resolved.name)
    if template is not None:
        template.last_used_at = utc_now()
