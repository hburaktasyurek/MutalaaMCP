"""Paylaşılan değişmez araştırma modelleri."""

from __future__ import annotations

from datetime import date as CalendarDate
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from mutalaamcp.domain.ids import (
    AnayasaId,
    BedestenId,
    MevzuatId,
    format_document_id,
    parse_document_id,
)

STALE_CONTENT = "stale_content"
_SHA256_HASH_PATTERN = r"^sha256:[a-f0-9]{64}$"


def _canonical_document_id(value: str) -> str:
    return format_document_id(parse_document_id(value))


def _absolute_uri(value: str) -> str:
    if "://" not in value or any(ch.isspace() for ch in value):
        raise ValueError("mutlak bir URI olmalıdır")
    return value


DocumentIdStr = Annotated[str, AfterValidator(_canonical_document_id)]
AbsoluteUri = Annotated[str, AfterValidator(_absolute_uri), Field(min_length=1)]
Sha256Hash = Annotated[str, Field(pattern=_SHA256_HASH_PATTERN)]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Court(StrEnum):
    YARGITAY = "yargitay"
    DANISTAY = "danistay"
    ISTINAF = "istinaf"
    YEREL = "yerel"
    KYB = "kyb"


class LegislationType(StrEnum):
    KANUN = "KANUN"
    KHK = "KHK"
    TUZUK = "TUZUK"
    CB_KARARNAME = "CB_KARARNAME"
    YONETMELIK = "YONETMELIK"
    CB_YONETMELIK = "CB_YONETMELIK"
    CB_KARAR = "CB_KARAR"
    CB_GENELGE = "CB_GENELGE"
    KKY = "KKY"
    UY = "UY"
    TEBLIGLER = "TEBLIGLER"
    MULGA = "MULGA"


class ConstitutionalKind(StrEnum):
    NORM_REVIEW = "norm_denetimi"
    INDIVIDUAL_APPLICATION = "bireysel_basvuru"


class ConversionMethod(StrEnum):
    HTML_MARKDOWN = "html_markdown"
    TEXT_MARKDOWN = "text_markdown"
    PDF_MARKDOWN = "pdf_markdown"
    OCR_MARKDOWN = "ocr_markdown"
    DOCX_MARKDOWN = "docx_markdown"


class RevalidationError(FrozenModel):
    code: Literal["upstream_unavailable", "upstream_rate_limited"]
    message: str = Field(min_length=1)


class DocumentRef(FrozenModel):
    document_id: DocumentIdStr
    source_url: AbsoluteUri


class HitBase(FrozenModel):
    id: DocumentIdStr
    source_url: AbsoluteUri
    title: str | None = None
    snippet: str | None = None


class DecisionHit(HitBase):
    source: Literal["bedesten"] = "bedesten"
    court: Court
    chamber: str | None = None
    esas_no: str | None = None
    karar_no: str | None = None
    date: CalendarDate | None = None

    @field_validator("id")
    @classmethod
    def _must_be_bedesten(cls, value: str) -> str:
        if not isinstance(parse_document_id(value), BedestenId):
            raise ValueError("DecisionHit.id bir bedesten belge kimliği olmalıdır")  # noqa: TRY004
        return value


class LegislationHit(HitBase):
    source: Literal["bedesten"] = "bedesten"
    legislation_type: LegislationType
    number: str | None = None
    title: str = Field(min_length=1)
    official_gazette_date: CalendarDate | None = None
    official_gazette_issue: str | None = None

    @field_validator("id")
    @classmethod
    def _must_be_mevzuat(cls, value: str) -> str:
        if not isinstance(parse_document_id(value), MevzuatId):
            raise ValueError("LegislationHit.id bir mevzuat belge kimliği olmalıdır")  # noqa: TRY004
        return value


class ConstitutionalHit(HitBase):
    source: Literal["anayasa"] = "anayasa"
    kind: ConstitutionalKind
    esas_no: str | None = None
    karar_no: str | None = None
    application_number: str | None = None
    date: CalendarDate | None = None
    summary: str | None = None

    @field_validator("id")
    @classmethod
    def _must_be_anayasa(cls, value: str) -> str:
        if not isinstance(parse_document_id(value), AnayasaId):
            raise ValueError("ConstitutionalHit.id bir anayasa belge kimliği olmalıdır")  # noqa: TRY004
        return value

    @model_validator(mode="after")
    def _kind_matches_id(self) -> Self:
        parsed = parse_document_id(self.id)
        if not isinstance(parsed, AnayasaId):
            raise ValueError("ConstitutionalHit.id bir anayasa belge kimliği olmalıdır")  # noqa: TRY004
        expected = (
            ConstitutionalKind.NORM_REVIEW
            if parsed.kind == "nd"
            else ConstitutionalKind.INDIVIDUAL_APPLICATION
        )
        if self.kind != expected:
            raise ValueError("anayasal karar türü anayasa kimliğiyle eşleşmelidir")
        return self


class PageInfo(FrozenModel):
    page: int = Field(ge=1)
    page_size: int = Field(
        ge=1,
        description="İstenen arama sayfası kapasitesi; bu sayfada döndürülen sonuç sayısı değildir.",
    )
    total_records: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    has_more: bool

    @model_validator(mode="after")
    def _has_more_matches_total_pages(self) -> Self:
        expected = self.page < self.total_pages
        if self.has_more != expected:
            raise ValueError(
                "has_more yalnızca page < total_pages olduğunda true olmalıdır"
            )
        return self


class ToolWarning(FrozenModel):
    code: Literal["stale_content", "outline_from_document", "outline_incomplete"]
    message: str = Field(min_length=1)


class SourceStatus(FrozenModel):
    source: str
    available: bool
    stale: bool = False
    message: str | None = None

    @model_validator(mode="after")
    def _unavailable_is_not_stale(self) -> Self:
        if not self.available and self.stale:
            raise ValueError("ulaşılamayan kaynak güncel olmayan olamaz")
        return self


class DocumentContent(FrozenModel):
    document_id: DocumentIdStr
    markdown: str
    page: PageInfo
    fetched_at: datetime
    validated_at: datetime
    expires_at: datetime | None
    content_hash: Sha256Hash
    conversion: ConversionMethod
    source_status: SourceStatus
    source_url: AbsoluteUri
    mime_type: str | None = None
    warnings: tuple[ToolWarning, ...] = ()
    revalidation_error: RevalidationError | None = None

    @model_validator(mode="after")
    def _stale_content_is_consistent(self) -> Self:
        stale_warning = any(warning.code == STALE_CONTENT for warning in self.warnings)
        if self.source_status.stale != stale_warning:
            raise ValueError(
                "stale_content uyarısı source_status.stale ile eşleşmelidir"
            )
        if self.revalidation_error is not None and not self.source_status.stale:
            raise ValueError(
                "revalidation_error yalnızca güncel olmayan içerik için geçerlidir"
            )
        if self.source_status.stale and self.revalidation_error is None:
            raise ValueError("güncel olmayan içerik revalidation_error gerektirir")
        parsed = parse_document_id(self.document_id)
        if isinstance(parsed, MevzuatId):
            if self.expires_at is None:
                raise ValueError("mevzuat belgeleri için expires_at boş olmamalıdır")
        elif self.expires_at is not None:
            raise ValueError(
                "bedesten ve anayasa belgeleri için planlanmış son kullanma zamanı yoktur"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_absent_revalidation_error(
        self, serializer: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        payload = serializer(self)
        if payload.get("revalidation_error") is None:
            payload.pop("revalidation_error", None)
        return payload
