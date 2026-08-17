"""Content-addressed storage for caller-supplied build contexts.

A build context is the only way a caller-supplied Dockerfile can `COPY`
anything, and it is a tarball uploaded over the API, so it is hostile input
that gets unpacked onto our disk. Every check here runs against the archive's
declared members *before* extraction, because `tarfile` will write wherever a
member says to.

Contexts are addressed by the digest of their compressed bytes, which is what
lets `TemplateSpec` fold the context into the template hash: same Dockerfile
plus same context means same image, and a changed file means a new template.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harborbox.config import Settings


class BuildContextError(ValueError):
    """An upload that must not be stored, or a digest that is not on disk."""


def digest_of(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class BuildContextStore:
    """Stores uploaded build contexts as verbatim tarballs, keyed by digest.

    The archive is kept compressed and unpacked per build rather than expanded
    once at upload: the digest has to stay the digest of exactly what the
    caller sent, and a stored tree would let the two drift.
    """

    def __init__(self, settings: Settings, root: Path) -> None:
        self.settings = settings
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        # The digest reaches this only after CONTEXT_DIGEST_PATTERN, so it is
        # `sha256:` plus hex and cannot contain a path separator.
        return self.root / f"{digest.replace(':', '-')}.tar.gz"

    def save(self, payload: bytes) -> str:
        """Validate an uploaded tarball and store it. Returns its digest."""
        self._inspect(payload)
        digest = digest_of(payload)
        destination = self.path_for(digest)
        if not destination.exists():
            # Same bytes, same digest: an existing file is already this content,
            # so re-uploading a context is free and idempotent.
            partial = destination.with_suffix(".partial")
            partial.write_bytes(payload)
            partial.replace(destination)
        return digest

    def extract(self, digest: str, destination: Path) -> None:
        """Unpack a stored context into `destination`.

        Re-validated on the way out. The stored bytes were checked at upload,
        but this is the call that actually writes to disk, and it is cheap
        insurance against anything that reached the directory another way.
        """
        source = self.path_for(digest)
        if not source.exists():
            message = f"build context not found: {digest}"
            raise BuildContextError(message)
        payload = source.read_bytes()
        self._inspect(payload)
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            # `data` refuses absolute paths, traversal, links out of the tree,
            # and special files. _inspect has already rejected all of those with
            # a readable message; this is the belt to its braces.
            archive.extractall(destination, filter="data")

    def remove(self, digest: str) -> None:
        self.path_for(digest).unlink(missing_ok=True)

    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def _inspect(self, payload: bytes) -> None:
        """Reject anything that must not be unpacked, with a readable reason."""
        if len(payload) > self.settings.template_max_context_bytes:
            message = (
                f"build context is {len(payload)} bytes, over the "
                f"{self.settings.template_max_context_bytes} byte limit"
            )
            raise BuildContextError(message)
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                self._inspect_members(archive)
        except tarfile.TarError as exc:
            message = f"build context is not a gzipped tar archive: {exc}"
            raise BuildContextError(message) from exc

    def _inspect_members(self, archive: tarfile.TarFile) -> None:
        members = archive.getmembers()
        if len(members) > self.settings.template_max_context_files:
            message = (
                f"build context holds {len(members)} files, over the "
                f"{self.settings.template_max_context_files} file limit"
            )
            raise BuildContextError(message)
        total = 0
        for member in members:
            self._inspect_member(member)
            total += member.size
            # Checked against the *declared* sizes, so an archive that
            # compresses to nothing and expands to gigabytes is refused
            # without ever being expanded.
            if total > self.settings.template_max_context_bytes:
                message = (
                    f"build context expands to over "
                    f"{self.settings.template_max_context_bytes} bytes"
                )
                raise BuildContextError(message)

    def _inspect_member(self, member: tarfile.TarInfo) -> None:
        name = member.name
        if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
            message = f"build context entry has an absolute path: {name}"
            raise BuildContextError(message)
        if ".." in Path(name).parts:
            message = f"build context entry escapes the tree by traversal: {name}"
            raise BuildContextError(message)
        if member.issym() or member.islnk():
            target = Path(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                message = (
                    f"build context symlink points outside the context: "
                    f"{name} -> {member.linkname}"
                )
                raise BuildContextError(message)
            return
        if not (member.isfile() or member.isdir()):
            message = (
                f"build context may contain only regular files, directories and "
                f"internal symlinks: {name}"
            )
            raise BuildContextError(message)
