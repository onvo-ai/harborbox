from harborbox.config import Settings
from harborbox.execution_secrets import (
    SECRET_ENVELOPE_KEY,
    open_environment,
    scrub_environment,
    seal_environment,
)


def test_secret_environment_is_encrypted_then_scrubbed() -> None:
    settings = Settings(execution_secret_key="unit-test-key")  # noqa: S106 -- fixed test fixture, not a real credential
    stored = seal_environment(
        settings,
        {"VISIBLE": "value"},
        {"API_TOKEN": "super-secret"},
    )

    assert stored["VISIBLE"] == "value"
    assert "super-secret" not in stored[SECRET_ENVELOPE_KEY]
    assert open_environment(settings, stored) == {
        "VISIBLE": "value",
        "API_TOKEN": "super-secret",
    }
    assert scrub_environment(stored) == {"VISIBLE": "value"}
