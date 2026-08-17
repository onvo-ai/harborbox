"""`forkrun.py`, the pre-warmed script runner behind /commands.

`import pandas` costs ~1.5 s and a dashboard refresh runs eight scripts in one
sandbox, so forkrun forks each run from a daemon that has already imported.
What has to hold is that a forked child is indistinguishable from
`python script.py` -- including its environment, which is where the bug below
lived.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FORKRUN = Path(__file__).resolve().parent.parent / "sandbox" / "forkrun.py"


def test_forkrun_gives_the_child_the_callers_environment(tmp_path: Path) -> None:
    """A forked child must see the *call's* environment, not the daemon's.

    The daemon is started by whichever call happens to be first and its
    children inherit its `os.environ`. Without forwarding, a per-call
    environment -- the credentials `/commands` injects -- is silently replaced
    by the first caller's, which fails as a wrong answer rather than an error.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'marker': os.environ.get('PROBE_MARKER'),"
        " 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    socket_path = tmp_path / "forkrun.sock"

    def call(marker: str) -> dict:
        completed = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, str(FORKRUN), str(probe), "an-arg"],
            capture_output=True,
            text=True,
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PROBE_MARKER": marker,
                # forkrun's socket path is a module constant; point it at tmp so
                # the test cannot collide with a real sandbox's daemon.
                "HARBORBOX_FORKRUN_SOCKET": str(socket_path),
            },
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    first = call("alpha")
    second = call("beta")

    assert first["marker"] == "alpha"
    assert first["argv"] == ["an-arg"]
    # The daemon was started by the first call and carries `alpha`; the second
    # call must still see its own value.
    assert second["marker"] == "beta"
