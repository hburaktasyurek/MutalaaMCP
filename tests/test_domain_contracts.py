"""Domain contract tests. No network, user directories, or live providers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from mutalaamcp.domain.errors import (
    ErrorBody,
    ErrorCode,
    ErrorDetails,
    ErrorEnvelope,
)
from mutalaamcp.domain.ids import (
    ANAYASA_SEQUENCE_MAX_DIGITS,
    AnayasaId,
    BedestenId,
    InvalidDocumentId,
    MevzuatId,
    format_document_id,
    parse_document_id,
)
from mutalaamcp.domain.models import (
    STALE_CONTENT,
    ConstitutionalHit,
    ConstitutionalKind,
    ConversionMethod,
    Court,
    DecisionHit,
    DocumentContent,
    LegislationHit,
    LegislationType,
    PageInfo,
    RevalidationError,
    SourceStatus,
    ToolWarning,
)
from mutalaamcp.server import V1_TOOL_NAMES
from mutalaamcp.services.common import error_envelope

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
SOURCE_URL = "https://example.test/doc"
NOW = datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
EXPIRES = NOW + timedelta(hours=24)
CONTENT_HASH = "sha256:" + "ab" * 32
OPAQUE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
ANAYASA_SEQUENCE_PATTERN = rf"[1-9][0-9]{{0,{ANAYASA_SEQUENCE_MAX_DIGITS - 1}}}"
CANONICAL_ID = re.compile(
    rf"^(?:bedesten:[A-Za-z0-9._-]+|mevzuat:[A-Za-z0-9._-]+|"
    rf"anayasa:(?:nd|bb):[1-9][0-9]{{3}}:{ANAYASA_SEQUENCE_PATTERN})$"
)
RETRYABLE_CODES = frozenset(
    {ErrorCode.UPSTREAM_RATE_LIMITED, ErrorCode.UPSTREAM_UNAVAILABLE}
)
EXPECTED_TOOLS = frozenset(
    {
        "turk_hukuku_sorularinda_once_bu_araci_cagir",
        "karar_ara",
        "mevzuat_ara",
        "anayasa_karari_ara",
        "belge_getir",
        "mevzuat_madde_getir",
        "mevzuat_icinde_ara",
        "mevzuat_madde_agaci_getir",
    }
)
LEGACY_PUBLIC_TOOL_NAMES = frozenset(
    {
        "search_decisions",
        "search_legislation",
        "search_constitutional",
        "get_document",
        "get_legislation_article",
        "search_in_legislation",
        "get_legislation_outline",
    }
)
EXPECTED_TOOL_COUNT = 8

opaque_ids = st.text(alphabet=OPAQUE_ALPHABET, min_size=1, max_size=64)
anayasa_kinds = st.sampled_from(("nd", "bb"))
anayasa_years = st.integers(min_value=1000, max_value=9999)
anayasa_sequences = st.integers(min_value=1, max_value=1_000_000)
canonical_ids = st.one_of(
    opaque_ids.map(lambda opaque: f"bedesten:{opaque}"),
    opaque_ids.map(lambda opaque: f"mevzuat:{opaque}"),
    st.builds(
        lambda kind, year, sequence: f"anayasa:{kind}:{year}:{sequence}",
        anayasa_kinds,
        anayasa_years,
        anayasa_sequences,
    ),
)


def _page(
    *,
    page: int = 1,
    page_size: int = 10,
    total_records: int = 3,
    total_pages: int = 1,
    has_more: bool = False,
) -> PageInfo:
    return PageInfo(
        page=page,
        page_size=page_size,
        total_records=total_records,
        total_pages=total_pages,
        has_more=has_more,
    )


def _status(
    *,
    source: str = "bedesten",
    available: bool = True,
    stale: bool = False,
    message: str | None = None,
) -> SourceStatus:
    return SourceStatus(
        source=source, available=available, stale=stale, message=message
    )


def _content(**overrides: object) -> DocumentContent:
    payload: dict[str, object] = {
        "document_id": "bedesten:abc123",
        "markdown": "# body",
        "page": _page(),
        "fetched_at": NOW,
        "validated_at": NOW,
        "expires_at": None,
        "content_hash": CONTENT_HASH,
        "conversion": ConversionMethod.HTML_MARKDOWN,
        "source_status": _status(),
        "source_url": SOURCE_URL,
    }
    payload.update(overrides)
    return DocumentContent.model_validate(payload)


def _details_for(code: ErrorCode) -> ErrorDetails | None:
    if code is ErrorCode.INVALID_PARAMS:
        return ErrorDetails(field_errors={"page": ("must be >= 1",)})
    if code is ErrorCode.TERMS_REQUIRED:
        return ErrorDetails(terms_url="https://example.test/kosullar")
    if code is ErrorCode.OCR_REQUIRED:
        return ErrorDetails(source_url=SOURCE_URL)
    if code is ErrorCode.UPSTREAM_RATE_LIMITED:
        return ErrorDetails(retry_after=1.5)
    return None


def _envelope(code: ErrorCode, *, retryable: bool | None = None) -> ErrorEnvelope:
    expected = code in RETRYABLE_CODES
    return ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message="contract",
            retryable=expected if retryable is None else retryable,
            details=_details_for(code),
        )
    )


def _load_contract(name: str) -> dict[str, object]:
    payload = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


# --- IDs ------------------------------------------------------------------


@given(value=canonical_ids)
def test_canonical_ids_parse_format_round_trip(value: str) -> None:
    parsed = parse_document_id(value)
    assert format_document_id(parsed) == value
    assert format_document_id(parse_document_id(format_document_id(parsed))) == value


@given(opaque=opaque_ids)
def test_bedesten_and_mevzuat_constructed_ids_round_trip(opaque: str) -> None:
    bedesten = BedestenId(opaque)
    mevzuat = MevzuatId(opaque)
    assert parse_document_id(bedesten.format()) == bedesten
    assert parse_document_id(mevzuat.format()) == mevzuat
    assert bedesten.format() == f"bedesten:{opaque}"
    assert mevzuat.format() == f"mevzuat:{opaque}"


@given(kind=anayasa_kinds, year=anayasa_years, sequence=anayasa_sequences)
def test_anayasa_constructed_ids_round_trip(
    kind: str, year: int, sequence: int
) -> None:
    document = (
        AnayasaId(kind="nd", year=year, sequence=sequence)
        if kind == "nd"
        else AnayasaId(kind="bb", year=year, sequence=sequence)
    )
    formatted = document.format()
    assert formatted == f"anayasa:{kind}:{year}:{sequence}"
    assert parse_document_id(formatted) == document


def test_anayasa_sequence_digit_limit_is_shared_by_parser_and_constructor() -> None:
    largest_sequence = "9" * ANAYASA_SEQUENCE_MAX_DIGITS
    accepted = f"anayasa:nd:2023:{largest_sequence}"
    parsed = parse_document_id(accepted)

    assert parsed == AnayasaId(kind="nd", year=2023, sequence=int(largest_sequence))

    overflow = f"anayasa:nd:2023:{'9' * (ANAYASA_SEQUENCE_MAX_DIGITS + 1)}"
    with pytest.raises(InvalidDocumentId):
        parse_document_id(overflow)
    with pytest.raises(InvalidDocumentId):
        AnayasaId(kind="nd", year=2023, sequence=10**ANAYASA_SEQUENCE_MAX_DIGITS)


@pytest.mark.parametrize(
    "sequence",
    (
        "9" * (ANAYASA_SEQUENCE_MAX_DIGITS + 1),
        "9" * 5_000,
    ),
)
def test_anayasa_sequence_rejects_out_of_range_text_before_int_conversion(
    sequence: str,
) -> None:
    value = f"anayasa:nd:2023:{sequence}"

    with pytest.raises(InvalidDocumentId) as exc:
        parse_document_id(value)

    assert exc.value.value == value


def test_advertised_anayasa_id_patterns_match_parser_grammar() -> None:
    surface = _load_contract("tool-surface-v1.json")
    sequence_pattern = rf"[1-9][0-9]{{0,{ANAYASA_SEQUENCE_MAX_DIGITS - 1}}}"
    norm_pattern = rf"^anayasa:nd:[1-9][0-9]{{3}}:{sequence_pattern}$"
    individual_pattern = rf"^anayasa:bb:[1-9][0-9]{{3}}:{sequence_pattern}$"
    either_pattern = rf"^anayasa:(nd|bb):[1-9][0-9]{{3}}:{sequence_pattern}$"
    document_pattern = (
        r"^(bedesten:[A-Za-z0-9._-]+|mevzuat:[A-Za-z0-9._-]+|"
        rf"anayasa:(nd|bb):[1-9][0-9]{{3}}:{sequence_pattern})$"
    )

    patterns = (
        (surface["id_grammars"]["anayasa_norm_review"]["pattern"], "nd"),
        (surface["id_grammars"]["anayasa_individual_application"]["pattern"], "bb"),
        (surface["$defs"]["AnayasaDocumentId"]["pattern"], "nd"),
        (surface["$defs"]["DocumentId"]["pattern"], "bb"),
        (
            surface["$defs"]["ConstitutionalHit"]["allOf"][1]["then"]["properties"][
                "id"
            ]["pattern"],
            "nd",
        ),
        (
            surface["$defs"]["ConstitutionalHit"]["allOf"][3]["then"]["properties"][
                "id"
            ]["pattern"],
            "bb",
        ),
    )

    assert tuple(pattern for pattern, _ in patterns) == (
        norm_pattern,
        individual_pattern,
        either_pattern,
        document_pattern,
        norm_pattern,
        individual_pattern,
    )
    largest_sequence = "9" * ANAYASA_SEQUENCE_MAX_DIGITS
    overflow_sequence = largest_sequence + "9"
    for pattern, kind in patterns:
        accepted = f"anayasa:{kind}:2023:{largest_sequence}"
        rejected = f"anayasa:{kind}:2023:{overflow_sequence}"
        assert re.fullmatch(pattern, accepted)
        assert not re.fullmatch(pattern, rejected)
        assert isinstance(parse_document_id(accepted), AnayasaId)
        with pytest.raises(InvalidDocumentId):
            parse_document_id(rejected)


@given(value=st.text(max_size=80))
def test_non_canonical_strings_are_rejected(value: str) -> None:
    if CANONICAL_ID.fullmatch(value):
        assert format_document_id(parse_document_id(value)) == value
        return
    with pytest.raises(InvalidDocumentId) as exc:
        parse_document_id(value)
    assert exc.value.value == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "bedesten:",
        "bedesten:abc def",
        "bedesten:abc/def",
        "https://example.test/doc",
        "http:foo",
        "file:foo",
        "ftp:foo",
        "/ND/2023/1",
        "//host/path",
        "anayasa:nd:2023",
        "anayasa:ND:2023:1",
        "anayasa:xx:2023:1",
        "anayasa:nd:0234:1",
        "anayasa:nd:2023:0",
        "anayasa:nd:2023:01",
        "anayasa:nd:2023:1:extra",
        "mevzuat:kanun:5:6698",
        "bedesten:abc:extra",
        "unknown:abc",
        " bedesten:abc",
        "bedesten:abc ",
    ],
)
def test_document_id_rejection_examples(value: str) -> None:
    with pytest.raises(InvalidDocumentId):
        parse_document_id(value)


@pytest.mark.parametrize("value", [None, 1, b"bedesten:abc", ["bedesten:abc"]])
def test_document_id_rejects_non_strings(value: object) -> None:
    with pytest.raises(InvalidDocumentId):
        parse_document_id(value)  # type: ignore[arg-type]


# --- Hits -----------------------------------------------------------------


@given(opaque=opaque_ids)
def test_decision_hit_requires_bedesten_namespace(opaque: str) -> None:
    hit = DecisionHit(
        id=f"bedesten:{opaque}",
        source_url=SOURCE_URL,
        court=Court.YARGITAY,
    )
    assert hit.source == "bedesten"
    assert isinstance(parse_document_id(hit.id), BedestenId)
    with pytest.raises(ValidationError):
        DecisionHit(
            id=f"mevzuat:{opaque}",
            source_url=SOURCE_URL,
            court=Court.YARGITAY,
        )
    with pytest.raises(ValidationError):
        DecisionHit(
            id="anayasa:nd:2023:1",
            source_url=SOURCE_URL,
            court=Court.YARGITAY,
        )
    with pytest.raises(ValidationError):
        DecisionHit.model_validate(
            {
                "id": f"bedesten:{opaque}",
                "source_url": SOURCE_URL,
                "court": "yargitay",
                "source": "anayasa",
            }
        )


@given(opaque=opaque_ids)
def test_legislation_hit_requires_mevzuat_namespace(opaque: str) -> None:
    hit = LegislationHit(
        id=f"mevzuat:{opaque}",
        source_url=SOURCE_URL,
        legislation_type=LegislationType.KANUN,
        title="Türk Medeni Kanunu",
    )
    assert hit.source == "bedesten"
    assert isinstance(parse_document_id(hit.id), MevzuatId)
    with pytest.raises(ValidationError):
        LegislationHit(
            id=f"bedesten:{opaque}",
            source_url=SOURCE_URL,
            legislation_type=LegislationType.KANUN,
            title="Türk Medeni Kanunu",
        )
    with pytest.raises(ValidationError):
        LegislationHit(
            id="anayasa:bb:2021:30620",
            source_url=SOURCE_URL,
            legislation_type=LegislationType.KANUN,
            title="Türk Medeni Kanunu",
        )
    with pytest.raises(ValidationError):
        LegislationHit.model_validate(
            {
                "id": f"mevzuat:{opaque}",
                "source_url": SOURCE_URL,
                "legislation_type": "KANUN",
                "title": "Türk Medeni Kanunu",
                "source": "anayasa",
            }
        )
    with pytest.raises(ValidationError):
        LegislationHit(
            id=f"mevzuat:{opaque}",
            source_url=SOURCE_URL,
            legislation_type=LegislationType.KANUN,
            title="",
        )


@given(kind=anayasa_kinds, year=anayasa_years, sequence=anayasa_sequences)
def test_constitutional_hit_namespace_source_and_kind(
    kind: str, year: int, sequence: int
) -> None:
    expected = (
        ConstitutionalKind.NORM_REVIEW
        if kind == "nd"
        else ConstitutionalKind.INDIVIDUAL_APPLICATION
    )
    wrong = (
        ConstitutionalKind.INDIVIDUAL_APPLICATION
        if expected is ConstitutionalKind.NORM_REVIEW
        else ConstitutionalKind.NORM_REVIEW
    )
    document_id = f"anayasa:{kind}:{year}:{sequence}"
    hit = ConstitutionalHit(
        id=document_id,
        source_url=SOURCE_URL,
        kind=expected,
    )
    assert hit.source == "anayasa"
    parsed = parse_document_id(hit.id)
    assert isinstance(parsed, AnayasaId)
    assert parsed.kind == kind
    with pytest.raises(ValidationError):
        ConstitutionalHit(id=document_id, source_url=SOURCE_URL, kind=wrong)
    with pytest.raises(ValidationError):
        ConstitutionalHit(
            id="bedesten:abc123",
            source_url=SOURCE_URL,
            kind=expected,
        )
    with pytest.raises(ValidationError):
        ConstitutionalHit(
            id="mevzuat:345097",
            source_url=SOURCE_URL,
            kind=expected,
        )
    with pytest.raises(ValidationError):
        ConstitutionalHit.model_validate(
            {
                "id": document_id,
                "source_url": SOURCE_URL,
                "kind": expected.value,
                "source": "bedesten",
            }
        )


# --- PageInfo -------------------------------------------------------------


@given(
    page=st.integers(min_value=1, max_value=40),
    page_size=st.integers(min_value=1, max_value=50),
    total_records=st.integers(min_value=0, max_value=400),
    total_pages=st.integers(min_value=0, max_value=40),
)
def test_pageinfo_has_more_matches_page_vs_total_pages(
    page: int, page_size: int, total_records: int, total_pages: int
) -> None:
    has_more = page < total_pages
    info = PageInfo(
        page=page,
        page_size=page_size,
        total_records=total_records,
        total_pages=total_pages,
        has_more=has_more,
    )
    assert info.has_more is (info.page < info.total_pages)
    with pytest.raises(ValidationError):
        PageInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
            has_more=not has_more,
        )


def test_pageinfo_partial_final_page() -> None:
    info = PageInfo(
        page=3,
        page_size=10,
        total_records=25,
        total_pages=3,
        has_more=False,
    )
    remainder = info.total_records % info.page_size
    assert remainder != 0
    assert remainder < info.page_size
    assert info.has_more is False
    assert info.page == info.total_pages
    with pytest.raises(ValidationError):
        PageInfo(
            page=3,
            page_size=10,
            total_records=25,
            total_pages=3,
            has_more=True,
        )


# --- DocumentContent ------------------------------------------------------


def test_document_content_fresh_bedesten_and_anayasa_have_no_expiry() -> None:
    bedesten = _content()
    assert bedesten.expires_at is None
    assert bedesten.source_status.stale is False
    assert bedesten.warnings == ()
    assert "revalidation_error" not in bedesten.model_dump()

    anayasa = _content(document_id="anayasa:nd:2023:123")
    assert anayasa.expires_at is None
    with pytest.raises(ValidationError):
        _content(expires_at=EXPIRES)
    with pytest.raises(ValidationError):
        _content(document_id="anayasa:bb:2021:30620", expires_at=EXPIRES)


def test_document_content_mevzuat_requires_expires_at() -> None:
    fresh = _content(
        document_id="mevzuat:345097",
        expires_at=EXPIRES,
        source_status=_status(source="bedesten"),
    )
    assert fresh.expires_at == EXPIRES
    with pytest.raises(ValidationError):
        _content(document_id="mevzuat:345097", expires_at=None)


def test_document_content_stale_requires_warning_and_revalidation_error() -> None:
    stale = _content(
        document_id="mevzuat:345097",
        expires_at=EXPIRES,
        source_status=_status(source="bedesten", stale=True),
        warnings=(ToolWarning(code=STALE_CONTENT, message="revalidation failed"),),
        revalidation_error=RevalidationError(
            code="upstream_unavailable", message="upstream 5xx"
        ),
    )
    assert stale.source_status.stale is True
    assert stale.warnings[0].code == STALE_CONTENT
    assert stale.revalidation_error is not None

    with pytest.raises(ValidationError):
        _content(
            document_id="mevzuat:345097",
            expires_at=EXPIRES,
            source_status=_status(stale=True),
            warnings=(ToolWarning(code=STALE_CONTENT, message="stale"),),
        )
    with pytest.raises(ValidationError):
        _content(
            document_id="mevzuat:345097",
            expires_at=EXPIRES,
            source_status=_status(stale=True),
            revalidation_error=RevalidationError(
                code="upstream_unavailable", message="timeout"
            ),
        )
    with pytest.raises(ValidationError):
        _content(
            document_id="mevzuat:345097",
            expires_at=EXPIRES,
            revalidation_error=RevalidationError(
                code="upstream_unavailable", message="timeout"
            ),
        )
    with pytest.raises(ValidationError):
        _content(
            document_id="mevzuat:345097",
            expires_at=EXPIRES,
            warnings=(ToolWarning(code=STALE_CONTENT, message="stale"),),
        )
    with pytest.raises(ValidationError):
        SourceStatus(source="bedesten", available=False, stale=True)


@given(conversion=st.sampled_from(tuple(ConversionMethod)))
def test_document_content_hash_and_conversion(conversion: ConversionMethod) -> None:
    doc = _content(conversion=conversion)
    assert doc.conversion is conversion
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", doc.content_hash)
    with pytest.raises(ValidationError):
        _content(content_hash="sha256:" + "A" * 64)
    with pytest.raises(ValidationError):
        _content(content_hash="sha256:" + "ab" * 31)
    with pytest.raises(ValidationError):
        _content(content_hash="md5:" + "ab" * 16)
    with pytest.raises(ValidationError):
        _content(conversion="rtf_markdown")


# --- Errors ---------------------------------------------------------------


def test_error_envelope_normalizes_a_plain_pydantic_value_error() -> None:
    class Request(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def _value_is_valid(cls, value: str) -> str:
            raise ValueError("Değer geçersiz.")

    with pytest.raises(ValidationError) as raised:
        Request(value="invalid")

    assert error_envelope(raised.value) == {
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "İstek parametreleri geçersiz.",
            "retryable": False,
            "details": {"field_errors": {"value": ["Değer geçersiz."]}},
        },
    }


def test_error_envelope_preserves_prefixed_pydantic_custom_error_text() -> None:
    class Request(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def _value_is_valid(cls, value: str) -> str:
            raise PydanticCustomError(
                "project_validation",
                "Value error, proje iletisi korunmalıdır.",
            )

    with pytest.raises(ValidationError) as raised:
        Request(value="invalid")

    assert error_envelope(raised.value) == {
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "İstek parametreleri geçersiz.",
            "retryable": False,
            "details": {
                "field_errors": {"value": ["Value error, proje iletisi korunmalıdır."]}
            },
        },
    }


def test_error_envelope_uses_turkish_fallback_for_empty_pydantic_value_error() -> None:
    class Request(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def _value_is_valid(cls, value: str) -> str:
            raise ValueError()

    with pytest.raises(ValidationError) as raised:
        Request(value="invalid")

    assert error_envelope(raised.value) == {
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "İstek parametreleri geçersiz.",
            "retryable": False,
            "details": {"field_errors": {"value": ["geçersiz değer"]}},
        },
    }


@pytest.mark.parametrize("code", list(ErrorCode))
def test_error_envelope_retryability_and_required_details(code: ErrorCode) -> None:
    envelope = _envelope(code)
    assert envelope.ok is False
    assert envelope.error.retryable is (code in RETRYABLE_CODES)
    dumped = envelope.model_dump()
    assert dumped["ok"] is False
    assert dumped["error"]["retryable"] is (code in RETRYABLE_CODES)

    with pytest.raises(ValidationError):
        _envelope(code, retryable=code not in RETRYABLE_CODES)


def test_error_envelope_code_specific_details() -> None:
    invalid = _envelope(ErrorCode.INVALID_PARAMS)
    assert invalid.error.details is not None
    assert invalid.error.details.field_errors == {"page": ("must be >= 1",)}
    with pytest.raises(ValidationError):
        ErrorBody(code=ErrorCode.INVALID_PARAMS, message="bad", retryable=False)
    with pytest.raises(ValidationError):
        ErrorDetails(field_errors={})
    with pytest.raises(ValidationError):
        ErrorDetails(field_errors={"": ("missing field name",)})
    with pytest.raises(ValidationError):
        ErrorDetails(field_errors={"page": ()})
    with pytest.raises(ValidationError):
        ErrorDetails(field_errors={"page": ("",)})

    terms = _envelope(ErrorCode.TERMS_REQUIRED)
    assert terms.error.details is not None
    assert terms.error.details.terms_url == "https://example.test/kosullar"
    with pytest.raises(ValidationError):
        ErrorBody(code=ErrorCode.TERMS_REQUIRED, message="terms", retryable=False)
    with pytest.raises(ValidationError):
        ErrorBody(
            code=ErrorCode.TERMS_REQUIRED,
            message="terms",
            retryable=False,
            details=ErrorDetails(terms_url="kosullar"),
        )

    ocr = _envelope(ErrorCode.OCR_REQUIRED)
    assert ocr.error.details is not None
    assert ocr.error.details.source_url == SOURCE_URL
    with pytest.raises(ValidationError):
        ErrorBody(code=ErrorCode.OCR_REQUIRED, message="ocr", retryable=False)

    limited = _envelope(ErrorCode.UPSTREAM_RATE_LIMITED)
    assert limited.error.retryable is True
    assert limited.error.details is not None
    assert limited.error.details.retry_after == 1.5
    with pytest.raises(ValidationError):
        ErrorBody(
            code=ErrorCode.UPSTREAM_RATE_LIMITED,
            message="slow",
            retryable=True,
        )
    with pytest.raises(ValidationError):
        ErrorDetails(retry_after=-1)

    unavailable = _envelope(ErrorCode.UPSTREAM_UNAVAILABLE)
    assert unavailable.error.retryable is True
    assert unavailable.error.details is None
    dumped = unavailable.model_dump()
    assert "details" not in dumped["error"]
    with pytest.raises(ValidationError):
        ErrorEnvelope.model_validate(
            {"ok": True, "error": unavailable.error.model_dump()}
        )


def test_tool_surface_date_bounds_are_structurally_inclusive() -> None:
    surface = _load_contract("tool-surface-v1.json")
    tools = {tool["name"]: tool for tool in surface["tools"]}
    decision_properties = tools["karar_ara"]["input_schema"]["properties"]
    legislation_properties = tools["mevzuat_ara"]["input_schema"]["properties"]

    assert surface["$defs"]["CalendarDate"]["bounds_inclusive"] is True
    assert all(
        properties[field]["bounds_inclusive"] is True
        for properties, field in (
            (decision_properties, "date_from"),
            (decision_properties, "date_to"),
            (legislation_properties, "gazette_date_from"),
            (legislation_properties, "gazette_date_to"),
        )
    )
    assert decision_properties["date_from"]["provider_wire_format"] == (
        "YYYY-MM-DDT00:00:00.000Z"
    )
    assert decision_properties["date_to"]["provider_wire_format"] == (
        "YYYY-MM-DDT23:59:59.000Z"
    )


# --- JSON contracts -------------------------------------------------------


def test_json_tool_surface_contract() -> None:
    surface = _load_contract("tool-surface-v1.json")
    tools_listed = surface["tools"]
    assert isinstance(tools_listed, list)
    surface_names = [tool["name"] for tool in tools_listed]
    assert V1_TOOL_NAMES == EXPECTED_TOOLS
    assert not V1_TOOL_NAMES & LEGACY_PUBLIC_TOOL_NAMES
    assert len(surface_names) == EXPECTED_TOOL_COUNT
    assert len(set(surface_names)) == EXPECTED_TOOL_COUNT
    assert set(surface_names) == EXPECTED_TOOLS
    assert not set(surface_names) & LEGACY_PUBLIC_TOOL_NAMES

    decision_tool = next(tool for tool in tools_listed if tool["name"] == "karar_ara")
    decision_input_schema = decision_tool["input_schema"]
    ek_fields = (
        "esas_year",
        "esas_sequence",
        "karar_year",
        "karar_sequence",
    )
    schema_properties = decision_input_schema["properties"]
    assert set(ek_fields) <= set(schema_properties)
    assert decision_input_schema["required"] == ["courts"]
    any_of_required_fields = {
        frozenset(option["required"]) for option in decision_input_schema["anyOf"]
    }
    assert frozenset(("esas_year", "esas_sequence")) in any_of_required_fields
    assert frozenset(("karar_year", "karar_sequence")) in any_of_required_fields
    assert decision_input_schema["dependentRequired"] == {
        "esas_year": ["esas_sequence"],
        "esas_sequence": ["esas_year"],
        "karar_year": ["karar_sequence"],
        "karar_sequence": ["karar_year"],
    }
    for field in ek_fields:
        schema = schema_properties[field]
        assert schema["type"] == "integer"
        assert schema["minimum"] >= 1
