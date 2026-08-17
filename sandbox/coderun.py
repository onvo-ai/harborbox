r"""Run a `POST /v1/executions` code body and report its final expression.

This is what replaced the Jupyter kernel. execd runs bash directly but proxied
Python to a Jupyter server, so serving `run_code` used to mean shipping
jupyter-server and ipykernel in the image and starting a server in every
sandbox -- measured at ~3 s of boot and ~197 MB resident before any code ran,
for a path Onvo never takes (it uploads a script and runs it through
`/v1/sandboxes/{id}/commands`).

What the kernel actually provided that a plain `python file.py` does not is
IPython's last-expression echo: `run_code("x = 40\\nx + 2")` returns `"42"` in
`results[0].text`, and the SDK's documented example prints it. That is
reproduced here with `ast`, which is the whole trick -- everything else about
a kernel was overhead.

Contract with the control plane (`OpenSandboxRuntime.execute_code`):

* `HARBORBOX_CODE_PATH` points at the user's code.
* `HARBORBOX_RESULT_SENTINEL` is a per-execution random marker. The last line
  of stdout is that marker followed by a JSON payload. It is generated fresh
  per execution and never reused, so user code cannot forge the trailer by
  printing a guessed value.

Exit status is 0 when the body completed and 1 when it raised, matching what a
caller gets from `python file.py`.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import traceback
from typing import Any

# Result text is truncated here as well as in the control plane: a repr can be
# arbitrarily large (a whole DataFrame), and the trailer travels inline on
# stdout, so an unbounded one would push real output out of the buffer.
MAX_RESULT_TEXT = 65_536


def _split_final_expression(tree: ast.Module) -> tuple[ast.Module, ast.Expression | None]:
    """Peel off a trailing expression statement so its value can be reported.

    A body ending in anything else (an assignment, a loop, a function
    definition) has no value to echo, and the whole module is returned to run
    as-is.
    """
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        final = ast.Expression(body=tree.body[-1].value)
        ast.copy_location(final, tree.body[-1])
        return ast.Module(body=tree.body[:-1], type_ignores=tree.type_ignores), final
    return tree, None


def _emit(sentinel: str, payload: dict[str, Any]) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    # Leading newline so the trailer is its own line even when the body's last
    # write had no trailing newline of its own.
    sys.stdout.write("\n" + sentinel + json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    path = os.environ["HARBORBOX_CODE_PATH"]
    sentinel = os.environ["HARBORBOX_RESULT_SENTINEL"]

    with open(path, encoding="utf-8") as handle:  # noqa: PTH123 - stdlib-only by design
        source = handle.read()

    namespace: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": path,
        "__builtins__": __builtins__,
    }

    try:
        body, final = _split_final_expression(ast.parse(source, filename=path))
    except SyntaxError as exc:
        # `format_exception_only`, not `format_exc`: the parse happens in this
        # module, so a full traceback would show the caller coderun.py's own
        # frames above their syntax error, which is noise they cannot act on.
        frames = traceback.format_exception_only(type(exc), exc)
        sys.stderr.write("".join(frames))
        _emit(
            sentinel,
            {
                "text": None,
                "error": {
                    "name": type(exc).__name__,
                    "value": str(exc),
                    "traceback": "".join(frames).splitlines(),
                },
            },
        )
        return 1

    try:
        exec(compile(body, path, "exec"), namespace)  # noqa: S102 - running user code is the job
        value = (
            eval(compile(final, path, "eval"), namespace)  # noqa: S307 - same
            if final is not None
            else None
        )
    except BaseException as exc:  # noqa: BLE001 - user code may raise anything, including
        # SystemExit and KeyboardInterrupt; all of it has to become a reported
        # result rather than propagate and lose the trailer the caller parses.
        if isinstance(exc, SystemExit):
            code = exc.code
            _emit(sentinel, {"text": None, "error": None})
            return code if isinstance(code, int) else 0 if code is None else 1
        # Drop this module's own frame so the traceback starts at user code.
        frames = traceback.format_exception(type(exc), exc, exc.__traceback__.tb_next)
        sys.stderr.write("".join(frames))
        _emit(
            sentinel,
            {
                "text": None,
                "error": {
                    "name": type(exc).__name__,
                    "value": str(exc),
                    "traceback": "".join(frames).splitlines(),
                },
            },
        )
        return 1

    text: str | None = None
    if value is not None:
        try:
            text = repr(value)[:MAX_RESULT_TEXT]
        except Exception:  # noqa: BLE001 - a broken __repr__ must not fail the execution
            text = f"<unreprable {type(value).__name__}>"

    _emit(sentinel, {"text": text, "error": None})
    return 0


if __name__ == "__main__":
    sys.exit(main())
