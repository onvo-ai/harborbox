from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harborbox_agent.files import (
    FileTooLarge,
    UnsafePath,
    list_files,
    read_file,
    safe_path,
    write_file,
    write_file_stream,
)


def test_safe_path_stays_under_workspace(tmp_path: Path) -> None:
    assert safe_path(tmp_path, "a/b.txt") == tmp_path / "a" / "b.txt"


def test_safe_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(UnsafePath):
        safe_path(tmp_path, "../../etc/passwd")


def test_safe_path_preserves_supported_absolute_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    temp_root = tmp_path / "tmp"
    assert (
        safe_path(workspace, "/workspace/data.csv", temp_root=temp_root)
        == workspace / "data.csv"
    )
    assert (
        safe_path(workspace, "/tmp/script.py", temp_root=temp_root)
        == temp_root / "script.py"
    )
    with pytest.raises(UnsafePath):
        safe_path(workspace, "/etc/passwd", temp_root=temp_root)


def test_file_round_trip_and_listing(tmp_path: Path) -> None:
    written = write_file(tmp_path, "folder/hello.txt", "hello", "utf-8")
    assert written["content"] == "hello"
    assert read_file(tmp_path, "folder/hello.txt")["content"] == "hello"
    listing = list_files(tmp_path, "folder")
    assert listing["entries"] == [
        {
            "name": "hello.txt",
            "path": "folder/hello.txt",
            "type": "file",
            "size": 5,
        }
    ]


async def byte_chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_streaming_file_write_is_atomic_and_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    temp_root = tmp_path / "tmp"
    result = await write_file_stream(
        workspace,
        "/tmp/data.bin",
        byte_chunks(b"abc", b"123"),
        max_bytes=6,
        temp_root=temp_root,
    )
    assert result == {"path": "/tmp/data.bin", "size": 6}
    assert (temp_root / "data.bin").read_bytes() == b"abc123"

    with pytest.raises(FileTooLarge):
        await write_file_stream(
            workspace,
            "/tmp/too-large.bin",
            byte_chunks(b"1234", b"5678"),
            max_bytes=7,
            temp_root=temp_root,
        )
    assert not (temp_root / "too-large.bin").exists()
