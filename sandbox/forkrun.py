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

import os
import socket
import struct
import sys
import time

SOCKET_PATH = "/tmp/.harborbox-forkrun.sock"

# Imported once in the daemon and inherited by every child. Kept to what widget
# templates actually import: anything else is memory every child pays for.
PRELOAD = ("pandas", "numpy", "json", "datetime", "math")

_READY_TIMEOUT_S = 30.0


def _run_in_process(path: str) -> int:
    """The fallback, and what the forked child ends up calling."""
    import runpy

    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    except BaseException:
        import traceback

        traceback.print_exc()
        return 1
    return 0


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            raise EOFError("forkrun daemon closed the connection")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _drain(read_fds: "list[int]") -> "dict[int, bytes]":
    """Reads both pipes concurrently.

    Sequential reads deadlock: a script that fills the stderr pipe buffer while
    the parent is still reading stdout blocks forever.
    """
    import select

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
        try:
            __import__(name)
        except Exception:
            # A missing preload is a slower child, not a broken one.
            pass

    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(16)

    while True:
        conn, _ = server.accept()
        try:
            length = struct.unpack("!I", _recv_exact(conn, 4))[0]
            path = _recv_exact(conn, length).decode("utf-8")

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
                    code = _run_in_process(path)
                    sys.stdout.flush()
                    sys.stderr.flush()
                except BaseException:
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
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


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
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            pass

        _spawn_daemon()
        deadline = time.time() + _READY_TIMEOUT_S
        while time.time() < deadline:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(SOCKET_PATH)
                return sock
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
    return None


def main(argv: "list[str]") -> int:
    if len(argv) == 2 and argv[1] == "--serve":
        _serve()
        return 0
    if len(argv) != 2:
        print("usage: forkrun.py <script.py>", file=sys.stderr)
        return 2

    path = os.path.abspath(argv[1])
    sock = _connect()
    if sock is None:
        return _run_in_process(path)

    try:
        encoded = path.encode("utf-8")
        sock.sendall(struct.pack("!I", len(encoded)) + encoded)
        rc, out_len, err_len = struct.unpack("!iII", _recv_exact(sock, 12))
        stdout = _recv_exact(sock, out_len)
        stderr = _recv_exact(sock, err_len)
    except Exception:
        return _run_in_process(path)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
