from __future__ import annotations

import base64
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4


class UnsafePath(ValueError):
    pass


class FileTooLarge(ValueError):
    pass


def safe_path(
    workspace: Path,
    requested: str,
    *,
    temp_root: Path = Path("/tmp"),
) -> Path:
    workspace_root = workspace.resolve()
    temp_root = temp_root.resolve()
    if requested == "/tmp" or requested.startswith("/tmp/"):
        root = temp_root
        relative = requested.removeprefix("/tmp").lstrip("/")
    elif requested == "/workspace" or requested.startswith("/workspace/"):
        root = workspace_root
        relative = requested.removeprefix("/workspace").lstrip("/")
    elif requested.startswith("/"):
        raise UnsafePath("absolute paths are limited to /workspace and /tmp")
    else:
        root = workspace_root
        relative = requested

    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafePath("path escapes the sandbox workspace") from exc
    return candidate


def read_file(workspace: Path, requested: str) -> dict[str, str]:
    path = safe_path(workspace, requested)
    raw = path.read_bytes()
    try:
        return {
            "path": requested,
            "content": raw.decode("utf-8"),
            "encoding": "utf-8",
        }
    except UnicodeDecodeError:
        return {
            "path": requested,
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
        }


def write_file(
    workspace: Path, requested: str, content: str, encoding: str
) -> dict[str, str]:
    path = safe_path(workspace, requested)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(content, validate=True) if encoding == "base64" else content.encode()
    path.write_bytes(raw)
    return read_file(workspace, requested)


async def write_file_stream(
    workspace: Path,
    requested: str,
    chunks: AsyncIterator[bytes],
    *,
    max_bytes: int,
    temp_root: Path = Path("/tmp"),
) -> dict[str, str | int]:
    path = safe_path(workspace, requested, temp_root=temp_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.upload-{uuid4().hex}")
    size = 0
    try:
        with staging.open("wb") as handle:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLarge(f"file exceeds the {max_bytes} byte upload limit")
                handle.write(chunk)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return {"path": requested, "size": size}


def list_files(workspace: Path, requested: str) -> dict[str, Any]:
    path = safe_path(workspace, requested)
    entries: list[dict[str, Any]] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        stat = child.lstat()
        is_directory = child.is_dir() and not child.is_symlink()
        entries.append(
            {
                "name": child.name,
                "path": (
                    child.name
                    if requested in {"", "."}
                    else f"{requested.rstrip('/')}/{child.name}"
                ),
                "type": "directory" if is_directory else "file",
                "size": None if is_directory else stat.st_size,
            }
        )
    return {"path": requested, "entries": entries}


def remove_file(workspace: Path, requested: str) -> None:
    path = safe_path(workspace, requested)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
