"""Isolated behavioral tests for bounded HTTP and local document conversion."""

from __future__ import annotations

import gzip
import zlib
from collections.abc import AsyncIterator, Callable
from io import BytesIO
from time import monotonic, sleep
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest

from mutalaamcp.conversion import documents
from mutalaamcp.conversion.documents import (
    OcrRequired,
    convert_document,
)
from mutalaamcp.domain.models import ConversionMethod
from mutalaamcp.net import (
    ResponseTooLarge,
    SafeHttpClient,
    UnsafeUrlError,
    UpstreamError,
    UpstreamNotFound,
    UpstreamRateLimited,
    UpstreamUnavailable,
)

_ALLOWED_HOST = "upstream.test"
_SOURCE_URL = "https://upstream.test/generated-document"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)


class _TrackingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.yielded: list[bytes] = []
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.yielded.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _pdf_with_stream(stream: bytes, *, flate: bool = False) -> bytes:
    """Build a compact, valid one-page PDF without a third-party writer."""
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode()
        + (b" /Filter /FlateDecode" if flate else b"")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    document.extend(
        b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    )
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(document)


def _pdf_with_indirect_flate_streams(*streams: bytes) -> bytes:
    """Build PDF content streams whose filter is hidden behind an object reference."""
    first_content_object = 4
    filter_object = first_content_object + len(streams)
    font_object = filter_object + 1
    content_references = b" ".join(
        f"{object_number} 0 R".encode()
        for object_number in range(first_content_object, filter_object)
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
            b"/Resources << /Font << /F1 "
            + str(font_object).encode()
            + b" 0 R >> >> /Contents ["
            + content_references
            + b"] >>"
        ),
    ]
    objects.extend(
        b"<< /Length "
        + str(len(compressed := zlib.compress(stream))).encode()
        + f" /Filter {filter_object} 0 R >>\nstream\n".encode()
        + compressed
        + b"\nendstream"
        for stream in streams
    )
    objects.extend(
        (
            b"[/FlateDecode]",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        )
    )

    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    document.extend(
        b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    )
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(document)


def _pdf_with_tounicode_text() -> bytes:
    content = b"BT\n/F1 12 Tf\n<01> Tj\nET"
    cmap = (
        b"/CIDInit /ProcSet findresource begin\n"
        b"12 dict begin\n"
        b"begincmap\n"
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        b"/CMapName /Adobe-Identity-UCS def\n"
        b"/CMapType 2 def\n"
        b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n"
        b"1 beginbfchar\n<01> <015E>\nendbfchar\n"
        b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    )
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        (
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding /ToUnicode 6 0 R >>"
        ),
        b"<< /Length "
        + str(len(cmap)).encode()
        + b" >>\nstream\n"
        + cmap
        + b"endstream",
    )
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(body)
        document.extend(b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    document.extend(
        b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    )
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(document)


def _docx(document_xml: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return output.getvalue()


def _recover_local_ocr(data: bytes, source_url: str) -> bytes:
    assert data.startswith(b"%PDF-")
    assert source_url == _SOURCE_URL
    return b"Recovered fixture text"


def _hung_local_ocr(data: bytes, source_url: str) -> str:
    assert data.startswith(b"%PDF-")
    assert source_url == _SOURCE_URL
    sleep(60)
    return "unreachable"


def _oversized_local_ocr(data: bytes, source_url: str) -> str:
    assert data.startswith(b"%PDF-")
    assert source_url == _SOURCE_URL
    return "private OCR content must not escape" * 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "reason"),
    (
        ("https://untrusted.test/resource", "ana makine izin listesinde değil"),
        (
            "https://fixture-user@upstream.test/resource",
            "kullanıcı bilgisine izin verilmez",
        ),
        (
            "https://upstream.test:8443/resource",
            "açıkça belirtilen porta izin verilmez",
        ),
    ),
)
async def test_safe_http_rejects_untrusted_host_userinfo_and_port(
    url: str, reason: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="unexpected", request=request)

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST})
        with pytest.raises(UnsafeUrlError) as raised:
            await http.request("GET", url)

    assert str(raised.value) == f"Güvenli olmayan üst kaynak URL'si ({reason}): {url}"

    assert requests == []


@pytest.mark.asyncio
async def test_safe_http_allows_only_configured_explicit_port() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"ok", request=request)

    async with _client(handler) as raw:
        http = SafeHttpClient(
            raw,
            {_ALLOWED_HOST},
            allowed_ports={_ALLOWED_HOST: (8443,)},
        )
        response = await http.request("GET", "https://upstream.test:8443/resource")

    assert response.content == b"ok"
    assert [str(request.url) for request in requests] == [
        "https://upstream.test:8443/resource"
    ]


@pytest.mark.asyncio
async def test_safe_http_revalidates_each_redirect_target() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://untrusted.test/redirect-target"},
            request=request,
        )

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST})
        with pytest.raises(UnsafeUrlError) as raised:
            await http.request("GET", "https://upstream.test/start")

    assert str(raised.value) == (
        "Güvenli olmayan üst kaynak URL'si "
        "(ana makine izin listesinde değil): "
        "https://untrusted.test/redirect-target"
    )

    assert [str(request.url) for request in requests] == ["https://upstream.test/start"]


@pytest.mark.asyncio
async def test_safe_http_stops_and_closes_an_oversized_stream_early() -> None:
    stream = _TrackingAsyncByteStream((b"12345", b"must-not-be-read"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST}, max_bytes=4)
        with pytest.raises(ResponseTooLarge) as raised:
            await http.request("GET", _SOURCE_URL)

    assert (
        str(raised.value) == "Üst kaynak yanıtı 4 bayt sınırını aştı: "
        "https://upstream.test/generated-document"
    )

    assert stream.yielded == [b"12345"]
    assert stream.closed is True


@pytest.mark.asyncio
async def test_safe_http_rejects_raw_streamed_gzip_expansion_before_buffering_decoded_body() -> (
    None
):
    encoded = gzip.compress(b"x" * 128)
    assert len(encoded) < 32
    stream = _TrackingAsyncByteStream((encoded,))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
            request=request,
        )

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST}, max_bytes=32)
        with pytest.raises(ResponseTooLarge) as raised:
            await http.request("GET", _SOURCE_URL)

    assert (
        str(raised.value) == "Üst kaynak yanıtı 32 bayt sınırını aştı: "
        "https://upstream.test/generated-document"
    )

    assert stream.closed is True


@pytest.mark.asyncio
async def test_safe_http_returns_httpx_decoded_gzip_once_without_stale_headers() -> (
    None
):
    payload = b"decoded fixture"
    encoded = gzip.compress(payload)

    assert len(encoded) > len(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(encoded)),
            },
            content=encoded,
            request=request,
        )
        assert response.is_stream_consumed
        return response

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST}, max_bytes=len(payload))
        response = await http.request("GET", _SOURCE_URL)

    assert response.content == payload
    assert "content-encoding" not in response.headers
    assert "content-length" not in response.headers


@pytest.mark.asyncio
async def test_safe_http_limits_httpx_decoded_gzip_content() -> None:
    encoded = gzip.compress(b"x" * 17)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            content=encoded,
            request=request,
        )

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST}, max_bytes=16)
        with pytest.raises(ResponseTooLarge) as raised:
            await http.request("GET", _SOURCE_URL)

    assert (
        str(raised.value) == "Üst kaynak yanıtı 16 bayt sınırını aştı: "
        "https://upstream.test/generated-document"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "error_type", "code"),
    (
        (404, {}, UpstreamNotFound, "not_found"),
        (429, {"Retry-After": "7"}, UpstreamRateLimited, "upstream_rate_limited"),
        (503, {}, UpstreamUnavailable, "upstream_unavailable"),
        (400, {}, UpstreamUnavailable, "upstream_unavailable"),
    ),
)
async def test_safe_http_maps_upstream_statuses(
    status: int,
    headers: dict[str, str],
    error_type: type[UpstreamError],
    code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, request=request)

    async with _client(handler) as raw:
        http = SafeHttpClient(raw, {_ALLOWED_HOST})
        with pytest.raises(error_type) as raised:
            await http.request("GET", _SOURCE_URL)

    error = raised.value
    assert error.code == code
    assert error.status_code == status
    if status == 429:
        assert isinstance(error, UpstreamRateLimited)
        assert error.retry_after == 7.0
    if status == 400:
        assert isinstance(error, UpstreamUnavailable)
        assert error.stale_eligible is False


@pytest.mark.asyncio
async def test_convert_html_discards_active_content_and_normalizes_markdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def pdf_path_must_not_run(data: bytes) -> str:
        raise AssertionError("HTML conversion must not invoke PDF extraction")

    monkeypatch.setattr(documents, "_pdf_to_text", pdf_path_must_not_run)

    converted = await convert_document(
        b"<h1>Generated heading</h1><p>One&nbsp; two</p>"
        b"<script>ignored fixture content</script><ul><li>Sample item</li></ul>",
        "text/html; charset=utf-8",
        _SOURCE_URL,
    )

    assert converted.conversion is ConversionMethod.HTML_MARKDOWN
    assert converted.mime_type == "text/html"
    assert converted.markdown == "# Generated heading\n\nOne two\n\n- Sample item\n"
    assert converted.content_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_convert_plain_text_normalizes_markdown() -> None:
    converted = await convert_document(
        "  Danıştay karar metni  \r\n\r\n\r\nİkinci satır\t \r\n".encode(),
        "text/plain; charset=utf-8",
        _SOURCE_URL,
    )

    assert converted.conversion.value == "text_markdown"
    assert converted.mime_type == "text/plain"
    assert converted.markdown == "Danıştay karar metni\n\nİkinci satır\n"
    assert converted.content_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_convert_text_pdf_without_network_ocr() -> None:
    converted = await convert_document(
        _pdf_with_stream(b"BT\n/F1 12 Tf\n(Synthetic PDF line) Tj\nET"),
        "application/pdf",
        _SOURCE_URL,
    )

    assert converted.conversion is ConversionMethod.PDF_MARKDOWN
    assert converted.markdown == "Synthetic PDF line\n"


@pytest.mark.asyncio
async def test_convert_mapped_font_pdf_uses_tounicode() -> None:
    converted = await convert_document(
        _pdf_with_tounicode_text(),
        "application/pdf",
        _SOURCE_URL,
    )

    assert converted.conversion is ConversionMethod.PDF_MARKDOWN
    assert converted.markdown == "Ş\n"


def test_bounded_pypdf_stream_instrumentation_is_scoped() -> None:
    from pypdf.generic import EncodedStreamObject, StreamObject

    original_stream_get_data = StreamObject.get_data
    original_encoded_get_data = EncodedStreamObject.get_data
    budget = documents._PdfBudget(
        documents._PdfLimits(
            max_pages=1,
            max_decoded_stream_bytes=1,
            max_text_bytes=1,
            timeout_seconds=1.0,
        )
    )

    with documents._bounded_pypdf_streams(budget):
        assert StreamObject.get_data is not original_stream_get_data
        assert EncodedStreamObject.get_data is not original_encoded_get_data

    assert StreamObject.get_data is original_stream_get_data
    assert EncodedStreamObject.get_data is original_encoded_get_data


@pytest.mark.asyncio
async def test_convert_pdf_rejects_compressed_stream_beyond_decoded_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_MAX_PDF_DECODED_STREAM_BYTES", 64)
    document = _pdf_with_stream(zlib.compress(b"x" * 128), flate=True)

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(document, "application/pdf", _SOURCE_URL)

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."

    assert raised.value.code == "unsupported_format"


@pytest.mark.asyncio
async def test_convert_pdf_rejects_cumulative_indirect_filter_stream_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_MAX_PDF_DECODED_STREAM_BYTES", 128)
    content = b"BT\n/F1 12 Tf\n(" + b"x" * 96 + b") Tj\nET"

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(
            _pdf_with_indirect_flate_streams(content, content),
            "application/pdf",
            _SOURCE_URL,
        )

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."

    assert raised.value.code == "unsupported_format"


@pytest.mark.asyncio
async def test_convert_pdf_rejects_oversized_extracted_text_during_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_MAX_PDF_TEXT_BYTES", 64)
    private_text = b"private-PDF-text-" * 8

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(
            _pdf_with_stream(b"BT\n/F1 12 Tf\n(" + private_text + b") Tj\nET"),
            "application/pdf",
            _SOURCE_URL,
        )

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."

    assert raised.value.code == "unsupported_format"
    assert "private-PDF-text" not in str(raised.value)


@pytest.mark.asyncio
async def test_convert_pdf_rejects_page_count_beyond_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_MAX_PDF_PAGES", 0)

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(
            _pdf_with_stream(b"BT\n(Page budget fixture) Tj\nET"),
            "application/pdf",
            _SOURCE_URL,
        )

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."


@pytest.mark.asyncio
async def test_convert_pdf_terminates_worker_when_parse_time_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_PDF_PARSE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(
            _pdf_with_stream(b"BT\n(Time budget fixture) Tj\nET"),
            "application/pdf",
            _SOURCE_URL,
        )

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."


@pytest.mark.asyncio
async def test_convert_scanned_pdf_requires_or_uses_injected_ocr() -> None:
    scanned = _pdf_with_stream(b"BT\nET")
    with pytest.raises(OcrRequired) as raised:
        await convert_document(scanned, "application/pdf", _SOURCE_URL)

    assert (
        str(raised.value)
        == "Bu yalnızca görsel içeren PDF'nin dönüştürülmesi için yerel OCR gereklidir."
    )

    converted = await convert_document(
        scanned, "application/pdf", _SOURCE_URL, _recover_local_ocr
    )

    assert converted.conversion is ConversionMethod.OCR_MARKDOWN
    assert converted.markdown == "Recovered fixture text\n"


@pytest.mark.asyncio
async def test_convert_scanned_pdf_terminates_hung_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_OCR_TIMEOUT_SECONDS", 0.05)
    started_at = monotonic()

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(
            _pdf_with_stream(b"BT\nET"),
            "application/pdf",
            _SOURCE_URL,
            _hung_local_ocr,
        )

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."

    assert monotonic() - started_at < 2.0


@pytest.mark.asyncio
async def test_convert_scanned_pdf_rejects_oversized_ocr_output_without_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents, "_MAX_OCR_TEXT_BYTES", 32)

    with pytest.raises(documents.UnsupportedDocumentFormat) as raised:
        await convert_document(
            _pdf_with_stream(b"BT\nET"),
            "application/pdf",
            _SOURCE_URL,
            _oversized_local_ocr,
        )

    assert str(raised.value) == "PDF dönüştürme işlemi güvenlik sınırlarını aştı."

    assert "private OCR content" not in str(raised.value)


@pytest.mark.asyncio
async def test_convert_docx_preserves_structured_local_content() -> None:
    document = _docx(
        """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
        <w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
          <w:body>
            <w:p><w:pPr><w:pStyle w:val=\"Heading1\"/></w:pPr><w:r><w:t>Fixture heading</w:t></w:r></w:p>
            <w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Fixture list item</w:t></w:r></w:p>
            <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Column</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          </w:body>
        </w:document>"""
    )

    converted = await convert_document(
        document,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _SOURCE_URL,
    )

    assert converted.conversion is ConversionMethod.DOCX_MARKDOWN
    assert converted.markdown == (
        "# Fixture heading\n\n- Fixture list item\n\n| Column |\n| --- |\n"
    )
