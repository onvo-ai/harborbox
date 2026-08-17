"""A caller-supplied tarball is hostile input, and it is unpacked on our disk.

Everything here is about the gap between "the archive says this path" and
"where that path actually lands". `tarfile` will happily write outside the
destination if asked, so the checks are on the members, before extraction, not
on the result afterwards.
"""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import pytest

from harborbox.build_contexts import (
    BuildContextError,
    BuildContextStore,
    digest_of,
)
from harborbox.config import Settings

if TYPE_CHECKING:
    from pathlib import Path


def tar_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def tar_with_member(info: tarfile.TarInfo) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(info)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> BuildContextStore:
    return BuildContextStore(Settings(), root=tmp_path)


def test_a_context_is_addressed_by_the_digest_of_its_bytes(
    store: BuildContextStore,
) -> None:
    """Content addressing is what makes the template hash mean anything.

    If two uploads with different contents could share a digest, two different
    images would share a name.
    """
    payload = tar_bytes({"app.py": b"print(1)"})

    digest = store.save(payload)
    again = store.save(payload)
    other = store.save(tar_bytes({"app.py": b"print(2)"}))

    assert digest == again == digest_of(payload)
    assert digest.startswith("sha256:")
    assert digest != other


def test_an_unknown_digest_is_reported_not_silently_ignored(
    store: BuildContextStore, tmp_path: Path
) -> None:
    """A build that silently loses its context would fail at a confusing COPY."""
    with pytest.raises(BuildContextError, match="not found"):
        store.extract("sha256:" + "0" * 64, tmp_path / "out")


def test_a_context_extracts_the_files_it_declared(
    store: BuildContextStore, tmp_path: Path
) -> None:
    digest = store.save(tar_bytes({"app.py": b"print(1)", "lib/util.py": b"x = 1"}))
    destination = tmp_path / "out"

    store.extract(digest, destination)

    assert (destination / "app.py").read_bytes() == b"print(1)"
    assert (destination / "lib" / "util.py").read_bytes() == b"x = 1"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("../escape.py", "traversal"),
        ("../../etc/passwd", "traversal"),
        ("/etc/passwd", "absolute"),
        ("nested/../../escape.py", "traversal"),
    ],
)
def test_paths_that_leave_the_destination_are_refused(
    store: BuildContextStore, name: str, reason: str
) -> None:
    """The classic tarball bug. Refused at upload, before anything is written."""
    with pytest.raises(BuildContextError, match=reason):
        store.save(tar_bytes({name: b"x"}))


def test_a_symlink_pointing_outside_the_context_is_refused(
    store: BuildContextStore,
) -> None:
    """A link is a write primitive too: COPY would follow it off the tree."""
    info = tarfile.TarInfo("link")
    info.type = tarfile.SYMTYPE
    info.linkname = "../../../etc/passwd"

    with pytest.raises(BuildContextError, match="symlink"):
        store.save(tar_with_member(info))


def test_a_device_node_is_refused(store: BuildContextStore) -> None:
    """Nothing in a build context needs to be a character device."""
    info = tarfile.TarInfo("dev")
    info.type = tarfile.CHRTYPE

    with pytest.raises(BuildContextError, match="regular files"):
        store.save(tar_with_member(info))


def test_an_oversized_context_is_refused(tmp_path: Path) -> None:
    store = BuildContextStore(
        Settings(template_max_context_bytes=1024), root=tmp_path
    )

    with pytest.raises(BuildContextError, match="bytes"):
        store.save(tar_bytes({"big.bin": b"x" * 4096}))


def test_a_context_with_too_many_files_is_refused(tmp_path: Path) -> None:
    store = BuildContextStore(Settings(template_max_context_files=3), root=tmp_path)

    with pytest.raises(BuildContextError, match="files"):
        store.save(tar_bytes({f"f{index}": b"x" for index in range(10)}))


def test_a_declared_size_that_exceeds_the_limit_is_refused_before_reading(
    tmp_path: Path,
) -> None:
    """A zip bomb declares its size in the header; believe it and refuse early.

    The compressed upload is small, so the byte cap on the request body does
    not catch this -- only the sum of the members' declared sizes does.
    """
    store = BuildContextStore(
        Settings(template_max_context_bytes=4096), root=tmp_path
    )
    payload = tar_bytes({"bomb": b"\0" * 100_000})

    with pytest.raises(BuildContextError, match="bytes"):
        store.save(payload)


def test_something_that_is_not_a_tarball_is_refused(store: BuildContextStore) -> None:
    with pytest.raises(BuildContextError, match="tar"):
        store.save(b"this is not a tarball")
