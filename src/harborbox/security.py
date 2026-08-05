import hmac

from fastapi import Header, HTTPException, status

from harborbox.config import get_settings


async def require_api_key(
    x_api_key: str | None = Header(default=None),
    open_sandbox_api_key: str | None = Header(
        default=None, alias="OPEN-SANDBOX-API-KEY"
    ),
) -> None:
    expected = get_settings().api_key
    supplied = x_api_key or open_sandbox_api_key
    if supplied is None or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
        )
