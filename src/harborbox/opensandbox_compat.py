from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harborbox.config import Settings
from harborbox.models import Sandbox, utc_now

INTERNAL_PREFIX = "harborbox.opensandbox."


class OpenSandboxImage(BaseModel):
    uri: str = Field(min_length=1, max_length=1024)


class OpenSandboxCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    image: OpenSandboxImage | None = None
    snapshot_id: str | None = Field(default=None, alias="snapshotId")
    entrypoint: list[str] = Field(default_factory=list, max_length=64)
    timeout: int | None = Field(default=None, ge=1)
    resource_limits: dict[str, str] = Field(
        default_factory=dict, alias="resourceLimits"
    )
    env: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)
    extensions: dict[str, str] = Field(default_factory=dict)
    platform: dict[str, str] | None = None
    volumes: list[dict[str, Any]] = Field(default_factory=list)
    network_policy: dict[str, Any] | None = Field(
        default=None, alias="networkPolicy"
    )
    secure_access: bool = Field(default=False, alias="secureAccess")

    @model_validator(mode="after")
    def supported_startup_source(self) -> OpenSandboxCreate:
        if self.snapshot_id is not None:
            raise ValueError("snapshot restore is not supported by the Docker provider")
        if self.image is None and not self.extensions.get("templateRef"):
            raise ValueError("image or extensions.templateRef is required")
        if self.env:
            raise ValueError(
                "persistent sandbox environment is not supported; pass secrets per execution"
            )
        if self.volumes:
            raise ValueError(
                "custom volumes are not supported; /workspace is managed automatically"
            )
        if self.network_policy is not None:
            raise ValueError(
                "networkPolicy requires an OpenSandbox or Kubernetes runtime provider"
            )
        if self.secure_access:
            raise ValueError(
                "secureAccess requires an OpenSandbox or Kubernetes runtime provider"
            )
        return self


class OpenSandboxStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    state: str
    reason: str
    message: str
    last_transition_at: datetime = Field(alias="lastTransitionAt")


class OpenSandboxResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    status: OpenSandboxStatus
    metadata: dict[str, str]
    extensions: dict[str, str]
    platform: dict[str, str] | None = None
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    created_at: datetime = Field(alias="createdAt")
    entrypoint: list[str]
    image: OpenSandboxImage | None = None


class OpenSandboxPagination(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    page: int
    page_size: int = Field(alias="pageSize")
    total_items: int = Field(alias="totalItems")
    total_pages: int = Field(alias="totalPages")
    has_next_page: bool = Field(alias="hasNextPage")


class OpenSandboxListResponse(BaseModel):
    items: list[OpenSandboxResponse]
    pagination: OpenSandboxPagination


class OpenSandboxRenew(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    expires_at: datetime = Field(alias="expiresAt")


class OpenSandboxRenewResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    expires_at: datetime = Field(alias="expiresAt")


def parse_memory_mb(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]i?|)\s*", value, re.IGNORECASE)
    if not match:
        raise ValueError(f"invalid memory resource limit: {value}")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "": 1 / (1024 * 1024),
        "k": 1 / 1024,
        "ki": 1 / 1024,
        "m": 1,
        "mi": 1,
        "g": 1024,
        "gi": 1024,
        "t": 1024 * 1024,
        "ti": 1024 * 1024,
    }
    return max(1, math.ceil(amount * factors[unit]))


def parse_cpu(value: str) -> float:
    normalized = value.strip().lower()
    if normalized.endswith("m"):
        return float(normalized[:-1]) / 1000
    return float(normalized)


def template_for(body: OpenSandboxCreate, settings: Settings) -> str:
    requested = body.extensions.get("templateRef") or body.extensions.get("template")
    if requested:
        # Well-formedness only. Whether a derived template exists and is ready is
        # decided by the registry in `create_sandbox`.
        if not settings.is_known_template_name(requested):
            raise ValueError(f"unknown sandbox template: {requested}")
        return requested
    assert body.image is not None
    for template, image in settings.template_images.items():
        if body.image.uri == image:
            return template
    raise ValueError(
        "a registered Harborbox templateRef or template image is required"
    )


def create_metadata(
    body: OpenSandboxCreate,
    *,
    template: str,
    now: datetime | None = None,
) -> dict[str, str]:
    if any(key.startswith(INTERNAL_PREFIX) for key in body.metadata):
        raise ValueError(f"metadata keys beginning with {INTERNAL_PREFIX} are reserved")
    created = now or utc_now()
    metadata = dict(body.metadata)
    metadata[f"{INTERNAL_PREFIX}image"] = body.image.uri if body.image else ""
    metadata[f"{INTERNAL_PREFIX}entrypoint"] = json.dumps(body.entrypoint)
    metadata[f"{INTERNAL_PREFIX}extensions"] = json.dumps(body.extensions)
    metadata[f"{INTERNAL_PREFIX}platform"] = json.dumps(body.platform)
    if body.timeout is not None:
        metadata[f"{INTERNAL_PREFIX}expires_at"] = (
            created + timedelta(seconds=body.timeout)
        ).isoformat()
    metadata["template"] = template
    return metadata


def expiration(sandbox: Sandbox) -> datetime | None:
    value = sandbox.metadata_.get(f"{INTERNAL_PREFIX}expires_at")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def public_metadata(sandbox: Sandbox) -> dict[str, str]:
    return {
        key: value
        for key, value in sandbox.metadata_.items()
        if not key.startswith(INTERNAL_PREFIX) and key != "template"
    }


def _json_metadata(sandbox: Sandbox, key: str, fallback: Any) -> Any:
    try:
        return json.loads(sandbox.metadata_.get(f"{INTERNAL_PREFIX}{key}", ""))
    except (TypeError, ValueError):
        return fallback


def lifecycle_status(sandbox: Sandbox) -> OpenSandboxStatus:
    states = {
        "created": ("Pending", "awaiting_capacity", "Sandbox is ready to be provisioned."),
        "starting": ("Pending", "container_starting", "Sandbox is starting."),
        "running": ("Running", "container_running", "Sandbox is ready."),
        "paused_memory": ("Paused", "memory_paused", "Sandbox state is retained in memory."),
        "paused_cold": (
            "Paused",
            "cold_paused",
            "Workspace retained; compute and memory released.",
        ),
        "killed": ("Terminated", "user_delete", "Sandbox was terminated."),
        "failed": ("Failed", "runtime_error", "Sandbox runtime failed."),
    }
    state, reason, message = states.get(
        sandbox.status,
        ("Failed", "unknown_state", f"Unknown Harborbox state: {sandbox.status}"),
    )
    return OpenSandboxStatus(
        state=state,
        reason=reason,
        message=message,
        lastTransitionAt=sandbox.updated_at,
    )


def response_for(sandbox: Sandbox, settings: Settings) -> OpenSandboxResponse:
    template = sandbox.metadata_.get("template")
    image_uri = sandbox.metadata_.get(f"{INTERNAL_PREFIX}image")
    if not image_uri:
        image_uri = settings.image_for_template(template)
    return OpenSandboxResponse(
        id=sandbox.id,
        status=lifecycle_status(sandbox),
        metadata=public_metadata(sandbox),
        extensions=_json_metadata(sandbox, "extensions", {}),
        platform=_json_metadata(sandbox, "platform", None),
        expiresAt=expiration(sandbox),
        createdAt=sandbox.created_at,
        entrypoint=_json_metadata(sandbox, "entrypoint", []),
        image=OpenSandboxImage(uri=image_uri),
    )
