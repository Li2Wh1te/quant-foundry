import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Identity derived from the credential that passed API authentication."""

    owner_scope: str


api_token_scheme = HTTPBearer(
    scheme_name="API Token",
    bearerFormat="opaque token",
    description="API token from the QF_API_TOKEN environment setting.",
    auto_error=False,
)


internal_token_scheme = APIKeyHeader(
    name="X-Backtest-Internal-Token",
    scheme_name="Internal Backtest Token",
    description="Internal token for Phase 2a backtest link acceptance.",
    auto_error=False,
)


def require_api_token(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(api_token_scheme),
    ],
) -> AuthenticatedPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    expected_token = request.app.state.settings.api_token.get_secret_value()
    if not secrets.compare_digest(
        credentials.credentials.encode("utf-8"),
        expected_token.encode("utf-8"),
    ):
        raise _unauthorized()

    # The deployment currently has one opaque API credential.  Its digest is
    # the authenticated owner identity: callers cannot choose a tenant header,
    # while rotating credentials intentionally creates a new ownership scope.
    principal = AuthenticatedPrincipal(
        owner_scope="token:" + hashlib.sha256(
            credentials.credentials.encode("utf-8")
        ).hexdigest()
    )
    request.state.authenticated_principal = principal
    request.state.owner_scope = principal.owner_scope
    return principal


def require_internal_backtest_token(
    request: Request,
    credentials: Annotated[str | None, Security(internal_token_scheme)],
) -> str:
    """Authorize the private Phase 2a endpoint with its own credential."""

    configured = getattr(request.app.state.settings, "backtest_internal_token", None)
    expected_token = (
        configured.get_secret_value() if configured is not None else None
    )
    if (
        expected_token is None
        or credentials is None
        or not secrets.compare_digest(
            credentials.encode("utf-8"),
            expected_token.encode("utf-8"),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "internal_capability_required",
                "message": "需要运维或服务专用令牌才可访问内部验收入口",
            },
        )
    return "service"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API token",
        headers={"WWW-Authenticate": "Bearer"},
    )
