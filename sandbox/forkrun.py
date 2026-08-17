"""Runs a widget script in a process that already has pandas imported.

`import pandas` costs ~1.5s in this image, and a dashboard refresh runs eight
scripts in one sandbox, so two thirds of a batch was spent importing the same
library eight times.

A fresh `python script.py` per widget was chosen deliberately: scripts must not
share a namespace, because widget code is customer-authored and a global left by
one widget silently changing the next is a bug nobody can reproduce outside a
batch. This keeps that property and drops the import cost, by forking each run
from a daemon that has done the imports once and nothing else:

  * the child is a copy-on-write snapshot of a pristine interpreter, so a widget
    that monkeypatches pandas, leaks memory or corrupts module state affects
    only itself — stronger isolation than reusing one interpreter, not weaker;
  * a crash or `os._exit` kills the child, not the batch;
  * stdout, stderr and the exit code are relayed unchanged, so the caller sees
    exactly what `python script.py` produced.

Falls back to running the script in-process whenever anything about the daemon
is unavailable. The fallback is the old behaviour, so the worst case of a broken
forkserver is the speed we had before it.

Usage:  python forkrun.py <script.py>
"""

import contextlib
import json
import os
import runpy
import select
import socket
import struct
import sys
import time
import traceback
from pathlib import Path

# Runs only inside the single-tenant sandbox container's tmpfs /tmp (see
# DockerRuntime._start_sandbox_sync in src/harborbox/runtime.py); a fixed
# path is fine because nothing outside this one container ever sees it.
#
# Overridable so tests can run a daemon of their own without colliding with a
# real one. Not a security boundary either way: anything that can set this
# variable is already running code inside the sandbox.
SOCKET_PATH = os.environ.get(
    "HARBORBOX_FORKRUN_SOCKET",
    "/tmp/.harborbox-forkrun.sock",  # noqa: S108
)

# Imported once in the daemon and inherited by every child. Kept to what widget
# templates actually import: anything else is memory every child pays for.
PRELOAD = ("pandas", "numpy", "json", "datetime", "math")

_READY_TIMEOUT_S = 30.0
_EXPECTED_ARGC = 2


def _apply_request(path: str, argv: "list[str]", env: "dict[str, str] | None") -> None:
    """Give the child the argv and environment the *client* had, not the daemon's.

    A forked child inherits the daemon's `os.environ`, and the daemon was
    started by whichever call happened to be first. Without this, a per-call
    environment -- the credentials `/v1/sandboxes/{id}/commands` injects, or the
    code path and result sentinel `coderun.py` reads -- is silently invisible to
    the script, which then runs against whatever the first caller happened to
    have. Replacing the mapping wholesale is what makes a forked run match
    `python script.py`.
    """
    sys.argv = [path, *argv]
    if env is not None:
        os.environ.clear()
        os.environ.update(env)


def _run_in_process(path: str) -> int:
    """Run the fallback path, and what the forked child ends up calling."""
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    except BaseException:  # noqa: BLE001 -- widget code is customer-authored and
        # arbitrary; anything it raises, including KeyboardInterrupt/GeneratorExit,
        # must be turned into a normal (code, stdout, stderr) result rather than
        # propagate, or a single bad widget kills the whole daemon/batch.
        traceback.print_exc()
        return 1
    return 0


def _encode_request(path: str, argv: "list[str]", env: "dict[str, str]") -> bytes:
    return json.dumps({"path": path, "argv": argv, "env": env}).encode("utf-8")


def _decode_request(payload: bytes) -> "tuple[str, list[str], dict[str, str] | None]":
    """Decode a run request, tolerating the older bare-path framing.

    The daemon outlives the call that spawned it but not the container, so
    client and daemon are always the same file. The bare-path fallback is for
    a daemon left running across an in-place file swap during development,
    where the alternative is a confusing JSON decode error.
    """
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return payload.decode("utf-8", errors="replace"), [], None
    if not isinstance(request, dict) or "path" not in request:
        return payload.decode("utf-8", errors="replace"), [], None
    return (
        str(request["path"]),
        [str(item) for item in request.get("argv", [])],
        {str(key): str(value) for key, value in request["env"].items()}
        if isinstance(request.get("env"), dict)
        else None,
    )


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            message = "forkrun daemon closed the connection"
            raise EOFError(message)
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _drain(read_fds: "list[int]") -> "dict[int, bytes]":
    """Read both pipes concurrently.

    Sequential reads deadlock: a script that fills the stderr pipe buffer while
    the parent is still reading stdout blocks forever.
    """
    out = {fd: [] for fd in read_fds}
    open_fds = list(read_fds)
    while open_fds:
        ready, _, _ = select.select(open_fds, [], [])
        for fd in ready:
            data = os.read(fd, 65536)
            if not data:
                open_fds.remove(fd)
                os.close(fd)
                continue
            out[fd].append(data)
    return {fd: b"".join(parts) for fd, parts in out.items()}


def _serve() -> None:
    for name in PRELOAD:
        # A missing optional dependency (pandas/numpy not installed in a
        # minimal image) is a slower child, not a broken one. Anything other
        # than ImportError is a real bug in the preload and should surface.
        with contextlib.suppress(ImportError):
            __import__(name)

    with contextlib.suppress(FileNotFoundError):
        Path(SOCKET_PATH).unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(16)

    while True:
        conn, _ = server.accept()
        try:
            length = struct.unpack("!I", _recv_exact(conn, 4))[0]
            path, argv, env = _decode_request(_recv_exact(conn, length))

            out_r, out_w = os.pipe()
            err_r, err_w = os.pipe()
            pid = os.fork()
            if pid == 0:
                # Child: becomes the widget script.
                try:
                    server.close()
                    conn.close()
                    os.close(out_r)
                    os.close(err_r)
                    os.dup2(out_w, 1)
                    os.dup2(err_w, 2)
                    os.close(out_w)
                    os.close(err_w)
                    _apply_request(path, argv, env)
                    code = _run_in_process(path)
                    sys.stdout.flush()
                    sys.stderr.flush()
                except BaseException:  # noqa: BLE001 -- fork-safety: the child must
                    # reach `os._exit` below no matter what, never fall through to
                    # normal interpreter shutdown, which would run atexit/finalizer
                    # state inherited (copy-on-write) from the parent and could
                    # corrupt or double-release resources the parent still owns.
                    code = 1
                os._exit(code)

            os.close(out_w)
            os.close(err_w)
            streams = _drain([out_r, err_r])
            _, status = os.waitpid(pid, 0)
            rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
            stdout, stderr = streams[out_r], streams[err_r]
            conn.sendall(
                struct.pack("!iII", rc, len(stdout), len(stderr)) + stdout + stderr
            )
        # One connection's protocol/IO failure (a client that disconnects
        # mid-request, a malformed length prefix, a pipe/fork error) must not
        # take down the daemon loop -- the next `accept()` should still get a
        # chance. Narrowed to the failure modes this block can actually raise;
        # anything else (e.g. a bug in our own code) is left to propagate.
        except (OSError, EOFError, UnicodeDecodeError, struct.error):
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()


def _spawn_daemon() -> None:
    """Double-forks so the daemon outlives the client that started it."""
    if os.fork() != 0:
        return
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        _serve()
    finally:
        os._exit(0)


def _connect() -> "socket.socket | None":
    for _ in range(2):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET_PATH)
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        else:
            return sock

        _spawn_daemon()
        deadline = time.time() + _READY_TIMEOUT_S
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(SOCKET_PATH)
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
            else:
                return sock
    return None


def main(argv: "list[str]") -> int:
    if len(argv) == _EXPECTED_ARGC and argv[1] == "--serve":
        _serve()
        return 0
    if len(argv) < _EXPECTED_ARGC:
        # This is the script's CLI usage message, not application logging;
        # stdout/stderr is the interface (see module docstring).
        print("usage: forkrun.py <script.py> [args...]", file=sys.stderr)  # noqa: T201
        return 2

    path = str(Path(argv[1]).resolve())
    script_argv = argv[2:]
    sock = _connect()
    if sock is None:
        _apply_request(path, script_argv, None)
        return _run_in_process(path)

    try:
        encoded = _encode_request(path, script_argv, dict(os.environ))
        sock.sendall(struct.pack("!I", len(encoded)) + encoded)
        rc, out_len, err_len = struct.unpack("!iII", _recv_exact(sock, 12))
        stdout = _recv_exact(sock, out_len)
        stderr = _recv_exact(sock, err_len)
    # Anything wrong with the daemon exchange (it died, the socket dropped,
    # a malformed reply) falls back to running the script in this process --
    # the documented fallback behaviour, narrowed to what this exchange can
    # actually raise.
    except (OSError, EOFError, struct.error):
        _apply_request(path, script_argv, None)
        return _run_in_process(path)
    finally:
        with contextlib.suppress(OSError):
            sock.close()

    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
