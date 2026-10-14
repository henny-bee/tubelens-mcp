from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    INVALID_ID = "INVALID_ID"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    CAPTION_UNAVAILABLE = "CAPTION_UNAVAILABLE"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    YOUTUBE_ACCESS_BLOCKED = "YOUTUBE_ACCESS_BLOCKED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    STORAGE_FULL = "STORAGE_FULL"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_CANCELLED = "JOB_CANCELLED"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorResponse(BaseModel):
    status: Literal["error"] = "error"
    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class PaginationMeta(BaseModel):
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    total: int = Field(ge=0)
    has_more: bool


class CursorPaginationMeta(BaseModel):
    cursor: str | None = None
    next_cursor: str | None = None
    limit: int = Field(ge=1)
    has_more: bool


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_response(self) -> dict[str, Any]:
        return ErrorResponse(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            details=self.details,
        ).model_dump()
