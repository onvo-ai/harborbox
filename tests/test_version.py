"""The version the API reports must be the version that was built.

`harborbox/__init__.py` said 0.1.0 while `api.py` passed a literal "0.2.0" to
FastAPI, and `pyproject.toml` said 0.2.0. That is three sources for one fact.

What made it expensive rather than merely untidy: FastAPI's *default* version is
also "0.1.0". So `/openapi.json` reporting 0.1.0 was indistinguishable between
"the deploy is stale" and "nobody wired a version up" — and the deployed
container really was stale, which is not something the reported version could
have told anyone.
"""

import tomllib
from pathlib import Path

from harborbox import __version__
from harborbox.api import app


def test_api_reports_the_package_version() -> None:
    """Not a literal. A literal is what drifted."""
    assert app.version == __version__


def test_package_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text())["project"]["version"]

    assert declared == __version__


def test_version_is_not_fastapis_default() -> None:
    """A guard against the ambiguity itself, not against any particular value.

    If the package version is ever legitimately 0.1.0 again this fails, and the
    right response is to delete this test — by then the reported version means
    something, because the other two assertions pin it to the build.
    """
    assert app.version != "0.1.0"
