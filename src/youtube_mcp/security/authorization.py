from __future__ import annotations

from enum import StrEnum

from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.security.auth import Principal, get_current_principal


class Scope(StrEnum):
    READ = "read"
    MEDIA_DOWNLOAD = "media:download"
    ADMIN_JOBS = "admin:jobs"


def require_scope(required_scope: Scope | str) -> Principal:
    principal = get_current_principal()
    if principal is None:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "Authentication is required to invoke this tool.",
        )
    scope_val = str(required_scope)
    if not principal.has_scope(scope_val):
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"Principal '{principal.principal_id}' lacks required scope '{scope_val}'.",
            details={"required_scope": scope_val},
        )
    return principal
