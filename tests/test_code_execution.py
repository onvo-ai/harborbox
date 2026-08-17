"""`POST /v1/executions` without a Jupyter kernel.

The kernel is gone; Python now runs as an ordinary command through
`sandbox/coderun.py`, and the control plane recovers the final-expression echo
from a sentinel-delimited trailer on stdout. Two things have to hold: the
runner reproduces the semantics callers already depend on, and the trailer
never leaks into the output they read.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harborbox.opensandbox_runtime import _split_result_trailer

CODERUN = Path(__file__).resolve().parent.parent / "sandbox" / "coderun.py"
FORKRUN = Path(__file__).resolve().parent.parent / "sandbox" / "forkrun.py"
SENTINEL = "__harborbox_result_test__"
# The status `test_sys_exit_keeps_its_status` asserts survives the runner.
EXPECTED_EXIT_STATUS = 3


def run_code(code: str, tmp_path: Path) -> tuple[int, str, str, dict]:
    """Run `code` the way the runtime does, and split the trailer back off."""
    source = tmp_path / "user_code.py"
    source.write_text(code, encoding="utf-8")
    completed = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(CODERUN)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HARBORBOX_CODE_PATH": str(source),
            "HARBORBOX_RESULT_SENTINEL": SENTINEL,
        },
    )
    stdout, result, error = _split_result_trailer([completed.stdout], SENTINEL)
    return (
        completed.returncode,
        "".join(stdout),
        completed.stderr,
        {"text": result.text if result else None, "error": error},
    )


def test_the_final_expression_is_echoed(tmp_path: Path) -> None:
    """The one thing a kernel gave that `python file.py` does not.

    The SDK's documented example prints `result.text` for exactly this, so it
    is a contract, not a nicety.
    """
    code, stdout, _, trailer = run_code("x = 40\nprint('calculating')\nx + 2", tmp_path)

    assert code == 0
    assert trailer["text"] == "42"
    assert stdout.strip() == "calculating"


def test_the_sentinel_trailer_is_stripped_from_stdout(tmp_path: Path) -> None:
    """Callers must never see the transport.

    A leaked trailer would corrupt every widget that parses its own stdout,
    which is how Onvo reads results.
    """
    _, stdout, _, _ = run_code("print('only this')\n1 + 1", tmp_path)

    assert SENTINEL not in stdout
    assert stdout.strip() == "only this"


def test_a_body_with_no_trailing_expression_reports_no_result(tmp_path: Path) -> None:
    _, _, _, trailer = run_code("y = 1\nfor _ in range(2):\n    pass", tmp_path)

    assert trailer["text"] is None
    assert trailer["error"] is None


def test_a_raising_body_reports_the_error_and_a_nonzero_exit(tmp_path: Path) -> None:
    code, _, stderr, trailer = run_code("raise ValueError('boom')", tmp_path)

    assert code == 1
    assert trailer["error"] is not None
    assert trailer["error"].name == "ValueError"
    assert trailer["error"].value == "boom"
    assert "ValueError: boom" in stderr


def test_a_traceback_does_not_expose_the_runner(tmp_path: Path) -> None:
    """The user's traceback should start at the user's code.

    `coderun.py` frames above it are noise nobody can act on, and they make an
    ordinary mistake look like an internal failure.
    """
    _, _, stderr, trailer = run_code("raise ValueError('boom')", tmp_path)

    assert "coderun.py" not in stderr
    assert trailer["error"] is not None
    assert not any("coderun.py" in line for line in trailer["error"].traceback)


def test_a_syntax_error_is_reported_as_the_users_error(tmp_path: Path) -> None:
    """Parsing happens inside the runner, so this is where its frames leak."""
    code, _, stderr, trailer = run_code("def (", tmp_path)

    assert code == 1
    assert trailer["error"] is not None
    assert trailer["error"].name == "SyntaxError"
    assert "coderun.py" not in stderr


def test_sys_exit_keeps_its_status(tmp_path: Path) -> None:
    """`python file.py` semantics: an explicit exit code survives."""
    code, _, _, trailer = run_code(
        f"import sys\nsys.exit({EXPECTED_EXIT_STATUS})", tmp_path
    )

    assert code == EXPECTED_EXIT_STATUS
    assert trailer["error"] is None


def test_forkrun_gives_the_child_the_callers_environment(tmp_path: Path) -> None:
    """A forked child must see the *call's* environment, not the daemon's.

    The daemon is started by whichever call happens to be first and its
    children inherit its `os.environ`. Without forwarding, a per-call
    environment -- the credentials `/commands` injects, or the code path
    `coderun.py` reads -- is silently replaced by the first caller's, which
    fails as a wrong answer rather than an error.
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


@pytest.mark.parametrize(
    ("chunks", "expected_stdout"),
    [
        (["no trailer here"], "no trailer here"),
        ([f"out\n{SENTINEL}not json\n"], f"out\n{SENTINEL}not json\n"),
    ],
)
def test_output_without_a_usable_trailer_is_returned_unchanged(
    chunks: list[str], expected_stdout: str
) -> None:
    """Output is bounded, so a big enough body can push the trailer off the end.

    Losing the final-expression echo is the right failure: the code ran, and
    reporting its stdout beats failing an execution that succeeded.
    """
    stdout, result, error = _split_result_trailer(chunks, SENTINEL)

    assert "".join(stdout) == expected_stdout
    assert result is None
    assert error is None


def test_only_the_last_trailer_counts() -> None:
    """User code can print anything, including something shaped like a trailer.

    The sentinel is fresh random per execution so this cannot be forged in
    practice, but the parser still has to take the real one -- which is always
    last, because the runner writes it after the body.
    """
    forged = json.dumps({"text": "forged", "error": None})
    real = json.dumps({"text": "real", "error": None})

    _, result, _ = _split_result_trailer(
        [f"\n{SENTINEL}{forged}\nmore output\n{SENTINEL}{real}\n"], SENTINEL
    )

    assert result is not None
    assert result.text == "real"
