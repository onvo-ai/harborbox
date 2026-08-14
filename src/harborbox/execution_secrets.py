from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from harborbox.config import Settings

SECRET_ENVELOPE_KEY = "__harborbox_secret_environment"


class InvalidSecretEnvelope(ValueError):
    pass


def _fernet(settings: Settings) -> Fernet:
    secret = settings.execution_secret_key.get_secret_value().encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def seal_environment(
    settings: Settings,
    environment: dict[str, str],
    secret_environment: dict[str, str],
) -> dict[str, str]:
    sealed = dict(environment)
    if secret_environment:
        payload = json.dumps(
            secret_environment,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        sealed[SECRET_ENVELOPE_KEY] = _fernet(settings).encrypt(payload).decode(
            "ascii"
        )
    return sealed


def open_environment(
    settings: Settings,
    environment: dict[str, str],
) -> dict[str, str]:
    public = dict(environment)
    envelope = public.pop(SECRET_ENVELOPE_KEY, None)
    if envelope is None:
        return public
    try:
        decoded = _fernet(settings).decrypt(envelope.encode("ascii"))
        secret = json.loads(decoded)
    except (InvalidToken, UnicodeError, json.JSONDecodeError) as exc:
        message = "invalid execution secret envelope"
        raise InvalidSecretEnvelope(message) from exc
    if not isinstance(secret, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in secret.items()
    ):
        message = "invalid execution secret environment"
        raise InvalidSecretEnvelope(message)
    return {**public, **secret}


def scrub_environment(environment: dict[str, str]) -> dict[str, str]:
    scrubbed = dict(environment)
    scrubbed.pop(SECRET_ENVELOPE_KEY, None)
    return scrubbed
