"""Anayasa Mahkemesinin güncel KBB SPA API'si için istemci.

İki kamuya açık KBB koleksiyonu aynı POST uç noktasını kullanır ancak bilinçli
olarak farklı kökenlerde barınır. Bu modül bir sonuç URL'sini asla izlemez:
kimlikler yalnızca API kaydının kanonik yolundan veya belgelenmiş atıf
alanından üretilir; belge getirme de kimliği API üzerinden yeniden çözümler.
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as CalendarDate
from html.parser import HTMLParser
from typing import Final
from urllib.parse import quote, urlparse

from mutalaamcp.domain.ids import (
    ANAYASA_SEQUENCE_MAX_DIGITS,
    AnayasaId,
    InvalidDocumentId,
    parse_document_id,
)
from mutalaamcp.domain.models import ConstitutionalKind
from mutalaamcp.net import SafeHttpClient

NORM_ORIGIN: Final = "https://normkararlarbilgibankasi.anayasa.gov.tr"
INDIVIDUAL_ORIGIN: Final = "https://kararlarbilgibankasi.anayasa.gov.tr"
SEARCH_PATH: Final = "/api/core/public/search"
AYM_ALLOWED_HOSTS: Final = frozenset(
    {
        "normkararlarbilgibankasi.anayasa.gov.tr",
        "kararlarbilgibankasi.anayasa.gov.tr",
    }
)
_MAX_SEARCH_SIZE: Final = 10
_MAX_CANONICAL_RESOLUTION_PAGES: Final = 3
_CANONICAL_PATH_FIELDS: Final = ("path", "canonicalPath", "urlPath")
_AYM_SEQUENCE_PATTERN: Final = rf"[1-9][0-9]{{0,{ANAYASA_SEQUENCE_MAX_DIGITS - 1}}}"
_CITATION = re.compile(
    rf"^(?P<year>[1-9][0-9]{{3}})/(?P<sequence>{_AYM_SEQUENCE_PATTERN})$"
)
_CANONICAL_ROUTE = re.compile(
    rf"^/(?P<route>ND|BB)/(?P<year>[1-9][0-9]{{3}})/"
    rf"(?P<sequence>{_AYM_SEQUENCE_PATTERN})$"
)


class AnayasaProtocolError(ValueError):
    """Üst kaynak yanıtı V1 sözleşmesine güvenle normalleştirilemiyor."""


class AnayasaNotFound(LookupError):
    """Güncel API'de kanonik AYM kimliğiyle eşleşen bir belge yok."""


@dataclass(frozen=True, slots=True)
class AnayasaRecord:
    """Normalleştirilmiş bir KBB sonuç kaydı.

    ``provider_id``, yalnızca üst kaynakta kullanılan bir arama belirtecidir.
    Mutalaa belge kimliği olarak asla dışa açılmaz ve yalnızca aynı
    koleksiyonun POST uç noktasının JSON ``id`` alanında kullanılır.
    """

    document_id: AnayasaId
    kind: ConstitutionalKind
    provider_id: str
    source_url: str
    title: str | None
    summary: str | None
    esas_no: str | None
    karar_no: str | None
    application_number: str | None
    decision_date: CalendarDate | None
    snippet: str | None
    html_content: str | None = None


@dataclass(frozen=True, slots=True)
class AnayasaSearchPage:
    """``/api/core/public/search`` uç noktasından normalleştirilmiş yanıt zarfı."""

    records: tuple[AnayasaRecord, ...]
    total_records: int
    page: int
    page_size: int


def _matching_record(
    page: AnayasaSearchPage, document_id: AnayasaId
) -> AnayasaRecord | None:
    return next(
        (record for record in page.records if record.document_id == document_id),
        None,
    )


class AnayasaClient:
    """İki V1 Anayasa Mahkemesi karar türü için enjekte edilmiş taşıma istemcisi."""

    def __init__(self, http: SafeHttpClient) -> None:
        self._http = http

    async def search(
        self,
        *,
        kind: ConstitutionalKind,
        query: str | None,
        page: int,
        size: int,
    ) -> AnayasaSearchPage:
        """Desteklenen bir koleksiyonda güncel SPA JSON gövdesiyle arama yapar."""
        _validate_pagination(page=page, size=size)
        karar_tipi = _karar_tipi(kind)
        body: dict[str, object] = {
            "kararTipi": karar_tipi,
            "page": page,
            "size": size,
        }
        compiled_query = _optional_query(query)
        if compiled_query is not None:
            body["query"] = compiled_query
        response = await self._http.request(
            "POST", _origin_for(kind) + SEARCH_PATH, json=body
        )
        return _parse_search_payload(
            response.json(), kind=kind, requested_page=page, requested_size=size
        )

    async def _search_by_identifier(
        self, document_id: AnayasaId, *, page: int
    ) -> AnayasaSearchPage:
        kind = _kind_for_id(document_id)
        body: dict[str, object] = {
            "kararTipi": _karar_tipi(kind),
            "page": page,
            "size": _MAX_SEARCH_SIZE,
            (
                "esasNo" if kind is ConstitutionalKind.NORM_REVIEW else "basvuruNo"
            ): f"{document_id.year}/{document_id.sequence}",
        }
        response = await self._http.request(
            "POST", _origin_for(kind) + SEARCH_PATH, json=body
        )
        return _parse_search_payload(
            response.json(),
            kind=kind,
            requested_page=page,
            requested_size=_MAX_SEARCH_SIZE,
        )

    async def get_document(self, document_id: AnayasaId) -> AnayasaRecord:
        """Kanonik bir AYM belgesini SPA API'si üzerinden çözümler ve getirir.

        Arka uç, istek gövdesinde geçici UUID'sini gerektirir. Yeniden
        başlatmaya dayanıklı bir ``anayasa:`` kimliği önce uygun koleksiyonun
        resmî ``esasNo`` veya ``basvuruNo`` filtresiyle çözümlenir; ardından
        eşleşen kayıt, ayrıntı isteği için UUID'yi sağlar. Ham URL'ler bu akışa
        hiç girmez.
        """
        kind = _kind_for_id(document_id)
        resolution = await self._search_by_identifier(document_id, page=1)
        resolved = _matching_record(resolution, document_id)
        # The total is untrusted metadata; canonical-query resolution gets a fixed
        # three-page budget instead of allowing it to set the request count.
        for resolution_page in range(2, _MAX_CANONICAL_RESOLUTION_PAGES + 1):
            if resolved is not None or not resolution.records:
                break
            candidate_page = await self._search_by_identifier(
                document_id, page=resolution_page
            )
            resolved = _matching_record(candidate_page, document_id)
            resolution = candidate_page
        if resolved is None:
            raise AnayasaNotFound(document_id.format())

        body: dict[str, object] = {
            "kararTipi": _karar_tipi(kind),
            "id": resolved.provider_id,
            "page": 1,
            "size": 1,
        }
        response = await self._http.request(
            "POST", _origin_for(kind) + SEARCH_PATH, json=body
        )
        payload = _require_mapping(response.json(), "document response")
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise AnayasaNotFound(document_id.format())
        record = _parse_record(data[0], kind=kind)
        if record.document_id != document_id:
            raise AnayasaProtocolError(
                "belge yanıtı köken bilgisi istenen anayasa kimliğiyle eşleşmiyor"
            )
        if record.provider_id != resolved.provider_id:
            raise AnayasaProtocolError(
                "belge yanıtı kimliği arama çözümlemesiyle eşleşmiyor"
            )
        if record.html_content is None:
            raise AnayasaProtocolError("belge yanıtında icerik HTML alanı yok")
        return record


def _parse_search_payload(
    payload: object,
    *,
    kind: ConstitutionalKind,
    requested_page: int,
    requested_size: int,
) -> AnayasaSearchPage:
    envelope = _require_mapping(payload, "arama yanıtı")
    data = envelope.get("data")
    if not isinstance(data, list):
        raise AnayasaProtocolError("arama yanıtı data alanı bir dizi olmalıdır")
    total = _non_negative_int(envelope.get("total"), "arama yanıtı total")
    response_page = _positive_int(
        envelope.get("page", requested_page), "arama yanıtı page"
    )
    response_size = _positive_int(
        envelope.get("page_size", requested_size), "arama yanıtı page_size"
    )
    records = tuple(_parse_record(item, kind=kind) for item in data)
    return AnayasaSearchPage(
        records=records,
        total_records=total,
        page=response_page,
        page_size=response_size,
    )


def _parse_record(value: object, *, kind: ConstitutionalKind) -> AnayasaRecord:
    item = _require_mapping(value, "arama sonucu")
    document_id = _document_id_from_provenance(item, kind=kind)
    provider_id = _provider_id(item)
    title = _text_field(item, "basvuruAdi")
    summary = _plain_text(_text_field(item, "kararKonusu"))
    if title is None:
        title = summary
    return AnayasaRecord(
        document_id=document_id,
        kind=kind,
        provider_id=provider_id,
        source_url=_spa_source_url(kind=kind, provider_id=provider_id),
        title=title,
        summary=summary,
        esas_no=_text_field(item, "esasNo")
        if kind is ConstitutionalKind.NORM_REVIEW
        else None,
        karar_no=_text_field(item, "kararNo")
        if kind is ConstitutionalKind.NORM_REVIEW
        else None,
        application_number=(
            _text_field(item, "basvuruNo")
            if kind is ConstitutionalKind.INDIVIDUAL_APPLICATION
            else None
        ),
        decision_date=_parse_date(_text_field(item, "kararTarihi")),
        snippet=_plain_text(
            _first_text_field(item, ("highlight", "highlightText", "snippet"))
        ),
        html_content=_text_field(item, "icerik"),
    )


def _document_id_from_provenance(
    item: Mapping[str, object], *, kind: ConstitutionalKind
) -> AnayasaId:
    evidence: list[AnayasaId] = []
    for field in _CANONICAL_PATH_FIELDS:
        if field in item and item[field] is not None:
            evidence.append(
                _id_from_canonical_route(item[field], expected_kind=kind, field=field)
            )

    citation_field = "esasNo" if kind is ConstitutionalKind.NORM_REVIEW else "basvuruNo"
    if citation_field in item and item[citation_field] is not None:
        evidence.append(
            _id_from_citation(
                item[citation_field], expected_kind=kind, field=citation_field
            )
        )

    if not evidence:
        raise AnayasaProtocolError(
            "sonuçta kanonik AYM yolu veya kanonik atıf köken bilgisi yok"
        )
    if any(candidate != evidence[0] for candidate in evidence[1:]):
        raise AnayasaProtocolError("sonuçta çelişen AYM köken bilgileri var")
    return evidence[0]


def _id_from_canonical_route(
    value: object, *, expected_kind: ConstitutionalKind, field: str
) -> AnayasaId:
    if not isinstance(value, str):
        raise AnayasaProtocolError(f"{field} kanonik bir AYM yolu olmalıdır")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.hostname not in AYM_ALLOWED_HOSTS:
            raise AnayasaProtocolError(f"{field} güvenilmeyen bir AYM kökenine sahip")
        expected_host = urlparse(_origin_for(expected_kind)).hostname
        if (
            parsed.hostname != expected_host
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise AnayasaProtocolError(
                f"{field} kanonik olmayan bir AYM kökenine sahip"
            )
        if parsed.query or parsed.fragment:
            raise AnayasaProtocolError(f"{field} sorgu veya parça içermemelidir")
        path = parsed.path
    else:
        path = value
    match = _CANONICAL_ROUTE.fullmatch(path)
    if match is None:
        raise AnayasaProtocolError(
            f"{field} kanonik bir /ND/YYYY/N veya /BB/YYYY/N yolu değildir"
        )
    route_kind = (
        ConstitutionalKind.NORM_REVIEW
        if match.group("route") == "ND"
        else ConstitutionalKind.INDIVIDUAL_APPLICATION
    )
    if route_kind is not expected_kind:
        raise AnayasaProtocolError(f"{field} istenen AYM koleksiyonuyla çelişiyor")
    return _canonical_id_from_parts(
        kind=route_kind,
        year=match.group("year"),
        sequence=match.group("sequence"),
        field=field,
    )


def _id_from_citation(
    value: object, *, expected_kind: ConstitutionalKind, field: str
) -> AnayasaId:
    if not isinstance(value, str):
        raise AnayasaProtocolError(f"{field} kanonik bir YYYY/N atfı olmalıdır")
    match = _CITATION.fullmatch(value)
    if match is None:
        raise AnayasaProtocolError(f"{field} kanonik bir YYYY/N atfı değildir")
    return _canonical_id_from_parts(
        kind=expected_kind,
        year=match.group("year"),
        sequence=match.group("sequence"),
        field=field,
    )


def _canonical_id_from_parts(
    *,
    kind: ConstitutionalKind,
    year: str,
    sequence: str,
    field: str,
) -> AnayasaId:
    canonical = (
        f"anayasa:{'nd' if kind is ConstitutionalKind.NORM_REVIEW else 'bb'}:"
        f"{year}:{sequence}"
    )
    try:
        parsed = parse_document_id(canonical)
    except InvalidDocumentId as exc:
        raise AnayasaProtocolError(
            f"{field} geçersiz bir AYM kimliği içeriyor"
        ) from exc
    assert isinstance(parsed, AnayasaId)
    return parsed


def _provider_id(item: Mapping[str, object]) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value or value != value.strip():
        raise AnayasaProtocolError(
            "sonuç id'si boş olmayan bir sağlayıcı belirteci olmalıdır"
        )
    if "://" in value or value.startswith(("/", "\\")):
        raise AnayasaProtocolError("sonuç id'si bir URL veya yol olmamalıdır")
    return value


def _spa_source_url(*, kind: ConstitutionalKind, provider_id: str) -> str:
    token = (
        base64.urlsafe_b64encode(f"kbb:{provider_id}".encode())
        .decode("ascii")
        .rstrip("=")
    )
    karar_tipi = _karar_tipi(kind)
    return (
        f"{_origin_for(kind)}/kbb/pages/search/{karar_tipi}"
        f"?id={quote(token, safe='')}&type={quote(karar_tipi, safe='')}"
    )


def _origin_for(kind: ConstitutionalKind) -> str:
    return NORM_ORIGIN if kind is ConstitutionalKind.NORM_REVIEW else INDIVIDUAL_ORIGIN


def _karar_tipi(kind: ConstitutionalKind) -> str:
    if kind is ConstitutionalKind.NORM_REVIEW:
        return "NormDenetimi"
    if kind is ConstitutionalKind.INDIVIDUAL_APPLICATION:
        return "BireyselBasvuru"
    raise ValueError("yalnızca norm_denetimi ve bireysel_basvuru desteklenir")


def _kind_for_id(document_id: AnayasaId) -> ConstitutionalKind:
    return (
        ConstitutionalKind.NORM_REVIEW
        if document_id.kind == "nd"
        else ConstitutionalKind.INDIVIDUAL_APPLICATION
    )


def _optional_query(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("query, str veya None olmalıdır")
    query = value.strip()
    return query or None


def _validate_pagination(*, page: int, size: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page pozitif bir tam sayı olmalıdır")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= _MAX_SEARCH_SIZE
    ):
        raise ValueError(
            f"size, 1 ile {_MAX_SEARCH_SIZE} arasında bir tam sayı olmalıdır"
        )


def _require_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AnayasaProtocolError(f"{context} bir nesne olmalıdır")
    return value


def _positive_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AnayasaProtocolError(f"{context} pozitif bir tam sayı olmalıdır")
    return value


def _non_negative_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AnayasaProtocolError(f"{context} negatif olmayan bir tam sayı olmalıdır")
    return value


def _text_field(item: Mapping[str, object], field: str) -> str | None:
    value = item.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnayasaProtocolError(f"{field} mevcut olduğunda bir metin olmalıdır")
    text = value.strip()
    return text or None


def _first_text_field(
    item: Mapping[str, object], fields: tuple[str, ...]
) -> str | None:
    for field in fields:
        value = _text_field(item, field)
        if value is not None:
            return value
    return None


def _parse_date(value: str | None) -> CalendarDate | None:
    if value is None:
        return None
    raw = value.strip()
    for parser in (CalendarDate.fromisoformat,):
        try:
            return parser(raw[:10])
        except ValueError:
            pass
    parts = raw.split(".")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        try:
            return CalendarDate(int(parts[2][:4]), int(parts[1]), int(parts[0]))
        except ValueError:
            pass
    raise AnayasaProtocolError("kararTarihi desteklenen bir takvim tarihi değildir")


def _plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    parser = _PlainTextHTMLParser()
    parser.feed(html.unescape(value))
    parser.close()
    text = " ".join(parser.parts)
    return re.sub(r"\s+", " ", text).strip() or None


class _PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.parts.append(data)
