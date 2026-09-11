"""Structured tool error envelope."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from mutalaamcp.domain.models import AbsoluteUri, FrozenModel, RevalidationError


class _OmitNone(FrozenModel):
    def model_dump(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("exclude_none", True)
        return super().model_dump_json(**kwargs)


class ErrorCode(StrEnum):
    INVALID_PARAMS = "invalid_params"
    AUTHENTICATION_REQUIRED = "authentication_required"
    SESSION_EXPIRED = "session_expired"
    EMAIL_VERIFICATION_REQUIRED = "email_verification_required"
    TERMS_REQUIRED = "terms_required"
    ACCOUNT_DISABLED = "account_disabled"
    ACCOUNT_LINK_REQUIRED = "account_link_required"
    ALREADY_RUNNING = "already_running"
    NOT_CONFIGURED = "not_configured"
    NOT_FOUND = "not_found"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CHUNK_OUT_OF_RANGE = "chunk_out_of_range"
    OCR_REQUIRED = "ocr_required"


_RETRYABLE_CODES = frozenset(
    {ErrorCode.UPSTREAM_RATE_LIMITED, ErrorCode.UPSTREAM_UNAVAILABLE}
)


class ErrorDetails(_OmitNone):
    field_errors: dict[str, tuple[str, ...]] | None = None
    retry_after: float | None = Field(default=None, ge=0)
    terms_url: AbsoluteUri | None = None
    source_url: AbsoluteUri | None = None
    upstream: Literal["bedesten", "anayasa"] | None = None
    revalidation_error: RevalidationError | None = None

    @field_validator("field_errors")
    @classmethod
    def _field_errors_shape(
        cls, value: dict[str, tuple[str, ...]] | None
    ) -> dict[str, tuple[str, ...]] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("field_errors must contain at least one field")
        for name, messages in value.items():
            if not name:
                raise ValueError("field_errors keys must be non-empty")
            if not messages:
                raise ValueError(
                    f"field_errors[{name}] must contain at least one message"
                )
            if any(not message for message in messages):
                raise ValueError(f"field_errors[{name}] messages must be non-empty")
        return value


class ErrorBody(_OmitNone):
    code: ErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    details: ErrorDetails | None = None

    @model_validator(mode="after")
    def _code_specific_contract(self) -> Self:
        expected_retryable = self.code in _RETRYABLE_CODES
        if self.retryable != expected_retryable:
            raise ValueError(f"{self.code} retryable must be {expected_retryable}")
        details = self.details
        if self.code is ErrorCode.INVALID_PARAMS:
            if details is None or not details.field_errors:
                raise ValueError("invalid_params requires details.field_errors")
        elif self.code is ErrorCode.TERMS_REQUIRED:
            if details is None or details.terms_url is None:
                raise ValueError("terms_required requires details.terms_url")
        elif self.code is ErrorCode.OCR_REQUIRED:
            if details is None or details.source_url is None:
                raise ValueError("ocr_required requires details.source_url")
        elif self.code is ErrorCode.UPSTREAM_RATE_LIMITED and (
            details is None or details.retry_after is None
        ):
            raise ValueError("upstream_rate_limited requires details.retry_after")
        return self


class ErrorEnvelope(_OmitNone):
    ok: Literal[False] = False
    error: ErrorBody
