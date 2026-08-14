import secrets
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


api_token_scheme = HTTPBearer(
    scheme_name="API Token",
    bearerFormat="opaque token",
    description="API token from the QF_API_TOKEN environment setting.",
    auto_error=False,
)


def require_api_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(api_token_scheme),
    ],
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    expected_token = request.app.state.settings.api_token.get_secret_value()
    if not secrets.compare_digest(
        credentials.credentials.encode("utf-8"),
        expected_token.encode("utf-8"),
    ):
        raise _unauthorized()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API token",
        headers={"WWW-Authenticate": "Bearer"},
    )
