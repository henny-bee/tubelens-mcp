from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope as ASGIScope, Send

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, ErrorCode, ErrorResponse


@dataclass(frozen=True)
class Principal:
    principal_id: str
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({"read"}))

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "current_principal", default=None
)
_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_request_id", default=""
)


def get_current_principal() -> Principal | None:
    return _current_principal.get()


def set_current_principal(principal: Principal | None) -> contextvars.Token[Principal | None]:
    return _current_principal.set(principal)


def get_request_id() -> str:
    req_id = _current_request_id.get()
    return req_id or f"req_{uuid.uuid4().hex[:12]}"


class TokenAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_table: list[tuple[str, Principal]] = []
        raw_primary = settings.mcp_bearer_token.get_secret_value().strip()
        if raw_primary:
            digest = hashlib.sha256(raw_primary.encode("utf-8")).hexdigest()[:12]
            self._token_table.append(
                (
                    raw_primary,
                    Principal(
                        principal_id=f"p_{digest}",
                        scopes=frozenset(settings.mcp_default_scopes),
                    ),
                )
            )
        if settings.mcp_tokens_json.strip():
            parsed = json.loads(settings.mcp_tokens_json)
            for tok, meta in parsed.items():
                pid = str(meta.get("principal_id") or f"p_{hashlib.sha256(tok.encode()).hexdigest()[:12]}")
                scopes = frozenset(meta.get("scopes") or ["read"])
                self._token_table.append((str(tok), Principal(principal_id=pid, scopes=scopes)))

    def authenticate_headers(self, headers: Headers, query_string: bytes = b"") -> Principal:
        if query_string:
            qs = parse_qs(query_string.decode("utf-8", errors="ignore"))
            if "token" in qs or "access_token" in qs or "bearer" in qs:
                raise AppError(
                    ErrorCode.UNAUTHORIZED,
                    "Tokens in URL query parameters are forbidden; use the Authorization header.",
                )

        if self._settings.auth_mode == "trusted_ingress":
            pid = headers.get("x-authenticated-principal", "").strip()
            if not pid:
                raise AppError(ErrorCode.UNAUTHORIZED, "Missing trusted ingress principal header.")
            raw_scopes = headers.get("x-authenticated-scopes", "read")
            scopes = frozenset(s.strip() for s in raw_scopes.split(",") if s.strip())
            return Principal(principal_id=pid, scopes=scopes or frozenset({"read"}))

        auth_header = headers.get("authorization", "")
        if not auth_header.startswith(("Bearer ", "bearer ")):
            raise AppError(ErrorCode.UNAUTHORIZED, "Missing or malformed Bearer Authorization header.")

        candidate = auth_header[7:].strip()
        if not candidate:
            raise AppError(ErrorCode.UNAUTHORIZED, "Empty bearer token.")

        matched_principal: Principal | None = None
        for expected_token, principal in self._token_table:
            if hmac.compare_digest(candidate.encode("utf-8"), expected_token.encode("utf-8")):
                matched_principal = principal

        if matched_principal is None:
            raise AppError(ErrorCode.UNAUTHORIZED, "Invalid or revoked bearer token.")
        return matched_principal


class AuthMiddleware:
    def __init__(self, app: ASGIApp, authenticator: TokenAuthenticator) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(self, scope: ASGIScope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path == "/healthz":
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        req_id = headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
        req_token = _current_request_id.set(req_id)
        principal_token = None
        try:
            principal = self._authenticator.authenticate_headers(
                headers, scope.get("query_string", b"")
            )
            principal_token = _current_principal.set(principal)
            await self._app(scope, receive, send)
        except AppError as exc:
            status_code = 401 if exc.code == ErrorCode.UNAUTHORIZED else 403
            resp = JSONResponse(
                ErrorResponse(
                    code=exc.code,
                    message=exc.message,
                    retryable=False,
                ).model_dump(),
                status_code=status_code,
                headers={"WWW-Authenticate": "Bearer", "X-Request-ID": req_id},
            )
            await resp(scope, receive, send)
        finally:
            if principal_token is not None:
                _current_principal.reset(principal_token)
            _current_request_id.reset(req_token)
