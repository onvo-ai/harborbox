from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from harborbox.models import SandboxTemplate, utc_now

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from harborbox.config import Settings

TemplateStatus = str

SANDBOX_USER = "10001:10001"

# Debian source/binary package names: lowercase alphanumerics plus `+`, `.` and
# `-`, starting with an alphanumeric. Deliberately narrower than policy allows.
APT_PACKAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,99}$")
# npm package names, optionally scoped. The version suffix is split off before
# this is applied, so it never has to tolerate an embedded `@`.
NPM_PACKAGE_PATTERN = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,63}$"
)
NPM_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")

# Rejected before the patterns above ever run. `@`, `/`, `.`, `+` and `-` are
# absent because they are legitimate in package names; everything a shell, the
# Dockerfile parser, or apt's own option parser could act on is here.
FORBIDDEN_CHARACTERS = frozenset("\"'`$&;|<>()[]{}\\*?!#~%^=,: \t\r\n")

MAX_PACKAGE_LENGTH = 214


class TemplateSpecError(ValueError):
    """A caller-supplied template spec that must never reach a Dockerfile."""


class UnknownTemplate(LookupError):
    pass


class TemplateNotReady(RuntimeError):
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
    """A validated, canonicalised requirement set."""

    base: str
    apt: tuple[str, ...] = ()
    npm: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.apt or self.npm or self.env)

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "base": self.base,
                "apt": list(self.apt),
                "npm": list(self.npm),
                "env": dict(self.env),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def spec_hash(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return digest[:12]

    @property
    def name(self) -> str:
        return self.base if self.is_empty else f"{self.base}-{self.spec_hash}"

    def as_json(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "apt": list(self.apt),
            "npm": list(self.npm),
            "env": dict(self.env),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> TemplateSpec:
        return cls(
            base=str(payload["base"]),
            apt=tuple(str(item) for item in payload.get("apt", [])),
            npm=tuple(str(item) for item in payload.get("npm", [])),
            env={str(key): str(value) for key, value in payload.get("env", {}).items()},
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


def _reject_metacharacters(kind: str, value: str) -> None:
    if not value:
        message = f"{kind} package name cannot be empty"
        raise TemplateSpecError(message)
    if len(value) > MAX_PACKAGE_LENGTH:
        message = f"{kind} package name is longer than {MAX_PACKAGE_LENGTH}"
        raise TemplateSpecError(message)
    for character in value:
        if not ("\x21" <= character <= "\x7e"):
            message = f"{kind} package name contains a control or non-ASCII character"
            raise TemplateSpecError(message)
        if character in FORBIDDEN_CHARACTERS:
            message = f"{kind} package name contains the forbidden character {character!r}"
            raise TemplateSpecError(message)


def split_npm_package(specifier: str) -> tuple[str, str | None]:
    """Split `@scope/name@1.2.3` into its package name and version."""
    start = 1 if specifier.startswith("@") else 0
    separator = specifier.find("@", start)
    if separator == -1:
        return specifier, None
    return specifier[:separator], specifier[separator + 1 :]


def _validate_apt(settings: Settings, packages: list[str]) -> tuple[str, ...]:
    if len(packages) > settings.template_max_apt_packages:
        message = f"at most {settings.template_max_apt_packages} apt packages are allowed"
        raise TemplateSpecError(message)
    for package in packages:
        _reject_metacharacters("apt", package)
        if not APT_PACKAGE_PATTERN.fullmatch(package):
            message = f"invalid apt package name: {package}"
            raise TemplateSpecError(message)
        if package not in settings.template_apt_allowlist:
            message = f"apt package is not allowlisted: {package}"
            raise TemplateSpecError(message)
    return tuple(sorted(set(packages)))


def _validate_npm(settings: Settings, packages: list[str]) -> tuple[str, ...]:
    if len(packages) > settings.template_max_npm_packages:
        message = f"at most {settings.template_max_npm_packages} npm packages are allowed"
        raise TemplateSpecError(message)
    for specifier in packages:
        _reject_metacharacters("npm", specifier)
        name, version = split_npm_package(specifier)
        if not NPM_PACKAGE_PATTERN.fullmatch(name):
            message = f"invalid npm package name: {specifier}"
            raise TemplateSpecError(message)
        if version is not None and not NPM_VERSION_PATTERN.fullmatch(version):
            message = f"invalid npm version specifier: {specifier}"
            raise TemplateSpecError(message)
        if name not in settings.template_npm_allowlist:
            message = f"npm package is not allowlisted: {name}"
            raise TemplateSpecError(message)
    return tuple(sorted(set(packages)))


def _validate_env(settings: Settings, env: dict[str, str]) -> dict[str, str]:
    if len(env) > settings.template_max_env_vars:
        message = f"at most {settings.template_max_env_vars} environment variables are allowed"
        raise TemplateSpecError(message)
    validated: dict[str, str] = {}
    for name, value in env.items():
        if not ENV_NAME_PATTERN.fullmatch(name):
            message = f"invalid environment variable name: {name}"
            raise TemplateSpecError(message)
        if len(value) > settings.template_max_env_value_length:
            message = (
                f"environment variable {name} exceeds "
                f"{settings.template_max_env_value_length} characters"
            )
            raise TemplateSpecError(message)
        for character in value:
            if not ("\x20" <= character <= "\x7e"):
                message = f"environment variable {name} contains a control or non-ASCII character"
                raise TemplateSpecError(message)
        validated[name] = value
    return validated


def validate_template_spec(
    settings: Settings,
    *,
    base: str,
    apt: list[str],
    npm: list[str],
    env: dict[str, str],
) -> TemplateSpec:
    """Validate a caller-supplied spec and canonicalise it.

    Raises `TemplateSpecError` for anything that must not reach the build host.
    """
    if base not in settings.template_images:
        message = f"unknown base template: {base}"
        raise TemplateSpecError(message)
    return TemplateSpec(
        base=base,
        apt=_validate_apt(settings, apt),
        npm=_validate_npm(settings, npm),
        env=_validate_env(settings, env),
    )


def dockerfile_value(value: str) -> str:
    """Quote an env value for a Dockerfile `ENV` instruction.

    JSON quoting handles `"` and `\\`; `$` is then escaped so the Dockerfile
    parser does not expand it into a build argument at image build time.
    """
    return json.dumps(value).replace("$", "\\$")


def render_dockerfile(*, base_image: str, spec: TemplateSpec) -> str:
    """Generate the derived image's Dockerfile.

    Every interpolated value has already passed `validate_template_spec`, and
    the base image runs as uid 10001, so root is reacquired only for the install
    layers and dropped again at the end.

    `ENV` is emitted before both install layers, not after. Install steps read
    the environment: `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` only suppresses
    Playwright's browser download if it is already set when `npm install -g`
    runs. Declaring it afterwards would silently do nothing and leave hundreds
    of megabytes of browser binaries in the image. Setting it first also keeps
    it in the running container, so there is no case where a later ENV is
    preferable.
    """
    lines = [f"FROM {base_image}", "USER root"]
    for name in sorted(spec.env):
        lines.append(f"ENV {name}={dockerfile_value(spec.env[name])}")
    if spec.apt:
        packages = " \\\n      ".join(spec.apt)
        lines.append(
            "RUN DEBIAN_FRONTEND=noninteractive apt-get update \\\n"
            "    && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--no-install-recommends \\\n"
            f"      {packages} \\\n"
            "    && rm -rf /var/lib/apt/lists/*"
        )
    if spec.npm:
        packages = " \\\n      ".join(spec.npm)
        lines.append(
            "RUN npm install --global --no-audit --no-fund \\\n"
            f"      {packages} \\\n"
            "    && npm cache clean --force"
        )
    lines.append(f"USER {SANDBOX_USER}")
    return "\n".join(lines) + "\n"


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
    """Look a template up by name across static config and the derived registry."""
    if name in settings.template_images:
        return static_template(settings, name)
    if settings.base_of_derived_template(name) is None:
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
        raise UnknownTemplate(message)
    if resolved.status != "ready":
        template = await session.get(SandboxTemplate, name)
        raise TemplateNotReady(
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
