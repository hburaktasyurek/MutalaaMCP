"""Bounded local document-to-Markdown conversion.

The converter has no network capability and does not choose or download an OCR
engine.  OCR is an optional caller-injected local callable; image-only PDFs
without one fail explicitly instead of silently delegating document data to a
remote service.
"""

from __future__ import annotations

import asyncio
import hashlib
import pickle
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from io import BytesIO, StringIO
from multiprocessing import get_context
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue
from pathlib import PurePosixPath
from queue import Empty
from time import monotonic
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from unittest.mock import patch

from mutalaamcp.domain.models import ConversionMethod

_HTML_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_MIME_TYPES = frozenset({"text/plain"})
_PDF_MIME_TYPES = frozenset({"application/pdf", "application/x-pdf"})
_DOCX_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-word.document.macroenabled.12",
    }
)
_MAX_DOCX_MEMBERS = 1_000
_MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 100
_MAX_PDF_INPUT_BYTES = 32 * 1024 * 1024
_MAX_PDF_PAGES = 1_000
_MAX_PDF_DECODED_STREAM_BYTES = 32 * 1024 * 1024
_MAX_PDF_TEXT_BYTES = 8 * 1024 * 1024
_PDF_PARSE_TIMEOUT_SECONDS = 10.0
_OCR_TIMEOUT_SECONDS = 120.0
_MAX_OCR_TEXT_BYTES = 8 * 1024 * 1024

_PYPDF_STREAM_LIMIT_NAMES = (
    "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
    "MAX_DECLARED_STREAM_LENGTH",
    "JBIG2_MAX_OUTPUT_LENGTH",
    "LZW_MAX_OUTPUT_LENGTH",
    "RUN_LENGTH_MAX_OUTPUT_LENGTH",
    "ZLIB_MAX_OUTPUT_LENGTH",
    "ZLIB_MAX_RECOVERY_INPUT_LENGTH",
    "FLATE_MAX_BUFFER_SIZE",
)
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD = f"{{{_WORD_NS}}}"

if TYPE_CHECKING:
    from pypdf import PageObject


class OcrCallable(Protocol):
    """A local OCR implementation injected by the runtime, never imported here."""

    def __call__(self, data: bytes, source_url: str) -> str | bytes: ...


@runtime_checkable
class OcrConverter(Protocol):
    """An OCR implementation that exposes a conversion method."""

    def convert(self, data: bytes, source_url: str) -> str | bytes: ...


@dataclass(frozen=True, slots=True)
class ConvertedDocument:
    """Normalized Markdown and the source representation used to produce it."""

    markdown: str
    mime_type: str
    conversion: ConversionMethod
    content_hash: str


@dataclass(frozen=True, slots=True)
class DocumentConversionError(RuntimeError):
    """A conversion failure that services can map to the public error envelope."""

    code: str
    message: str
    source_url: str

    def __str__(self) -> str:
        return self.message


class UnsupportedDocumentFormat(DocumentConversionError):
    def __init__(
        self, mime_type: str, source_url: str, message: str | None = None
    ) -> None:
        super().__init__(
            "unsupported_format",
            message or f"Desteklenmeyen belge biçimi: {mime_type or 'bilinmeyen'}.",
            source_url,
        )


class OcrRequired(DocumentConversionError):
    def __init__(self, source_url: str) -> None:
        super().__init__(
            "ocr_required",
            "Bu yalnızca görsel içeren PDF'nin dönüştürülmesi için yerel OCR gereklidir.",
            source_url,
        )


async def convert_document(
    data: bytes,
    mime_type: str,
    source_url: str,
    ocr: OcrCallable | OcrConverter | None = None,
) -> ConvertedDocument:
    """Convert supported bytes to normalized Markdown outside the event loop.

    Plain text, HTML, PDF, and DOCX conversion can parse or decompress substantial
    content, so the complete CPU path (including injected local OCR) runs in
    :func:`asyncio.to_thread`.  ``ocr`` is deliberately supplied by the caller;
    this module never reaches a remote OCR endpoint or imports an optional OCR
    package as an import-time side effect.
    """
    if not isinstance(data, bytes):
        raise TypeError("data, bytes türünde olmalıdır")
    if not isinstance(mime_type, str):
        raise TypeError("mime_type, str türünde olmalıdır")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("source_url boş olmayan bir dizge olmalıdır")
    return await asyncio.to_thread(_convert_sync, data, mime_type, source_url, ocr)


def _convert_sync(
    data: bytes,
    raw_mime_type: str,
    source_url: str,
    ocr: OcrCallable | OcrConverter | None,
) -> ConvertedDocument:
    mime_type = _normalized_mime_type(raw_mime_type)
    if mime_type in _TEXT_MIME_TYPES:
        markdown = _normalize_markdown(_decode_text(data))
        conversion = ConversionMethod.TEXT_MARKDOWN
    elif mime_type in _HTML_MIME_TYPES:
        markdown = _html_to_markdown(data)
        conversion = ConversionMethod.HTML_MARKDOWN
    elif mime_type in _PDF_MIME_TYPES:
        text = _pdf_to_text(data)
        if _has_meaningful_text(text):
            markdown = _normalize_markdown(text)
            conversion = ConversionMethod.PDF_MARKDOWN
        elif ocr is None:
            raise OcrRequired(source_url)
        else:
            markdown = _call_ocr(ocr, data, source_url)
            if not _has_meaningful_text(markdown):
                raise OcrRequired(source_url)
            conversion = ConversionMethod.OCR_MARKDOWN
    elif mime_type in _DOCX_MIME_TYPES:
        markdown = _docx_to_markdown(data, source_url)
        conversion = ConversionMethod.DOCX_MARKDOWN
    else:
        raise UnsupportedDocumentFormat(mime_type, source_url)

    if not markdown:
        raise UnsupportedDocumentFormat(
            mime_type, source_url, "Belge dönüştürülebilir metin içermiyor."
        )
    return ConvertedDocument(
        markdown=markdown,
        mime_type=mime_type,
        conversion=conversion,
        content_hash="sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )


def _normalized_mime_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _html_to_markdown(data: bytes) -> str:
    decoded = _decode_text(data)
    parser = _MarkdownHTMLParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception as exc:  # HTMLParser intentionally accepts malformed HTML.
        raise UnsupportedDocumentFormat(
            "text/html", "about:blank", "HTML belgesi geçersiz."
        ) from exc
    return _normalize_markdown(parser.markdown())


class _MarkdownHTMLParser(HTMLParser):
    """Small dependency-free HTML normalizer for official document pages."""

    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "blockquote",
            "div",
            "dl",
            "dt",
            "dd",
            "figcaption",
            "figure",
            "footer",
            "header",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "section",
            "table",
            "tbody",
            "td",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_depth = 0
        self._list_stack: list[tuple[str, int]] = []
        self._href_stack: list[str | None] = []
        self._pre_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._block()
            self._parts.append("#" * int(tag[1]) + " ")
        elif tag == "br":
            self._parts.append("\n")
        elif tag == "hr":
            self._block()
            self._parts.append("---\n")
        elif tag in {"ul", "ol"}:
            self._block()
            self._list_stack.append((tag, 0))
        elif tag == "li":
            self._block()
            if self._list_stack:
                kind, index = self._list_stack[-1]
                index += 1
                self._list_stack[-1] = (kind, index)
                self._parts.append(f"{index}. " if kind == "ol" else "- ")
            else:
                self._parts.append("- ")
        elif tag in self._BLOCK_TAGS:
            self._block()
        elif tag == "a":
            href = dict(attrs).get("href")
            self._href_stack.append(href)
            self._parts.append("[")
        elif tag == "pre":
            self._block()
            self._pre_depth += 1
            self._parts.append("```\n")
        elif tag == "code" and not self._pre_depth:
            self._parts.append("`")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template", "svg"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._href_stack:
            href = self._href_stack.pop()
            self._parts.append("]")
            if href:
                self._parts.append(f"({href})")
        elif tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._parts.append("\n```\n")
        elif tag == "code" and not self._pre_depth:
            self._parts.append("`")
        elif tag in {"ul", "ol"}:
            if self._list_stack:
                self._list_stack.pop()
            self._block()
        elif (
            tag in self._BLOCK_TAGS
            or tag.startswith("h")
            and len(tag) == 2
            and tag[1].isdigit()
        ):
            self._block()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._pre_depth:
            self._parts.append(data)
        else:
            self._parts.append(re.sub(r"\s+", " ", data))

    def markdown(self) -> str:
        return "".join(self._parts)

    def _block(self) -> None:
        if not self._parts:
            return
        if not self._parts[-1].endswith("\n\n"):
            self._parts.append("\n\n")


@dataclass(frozen=True, slots=True)
class _PdfLimits:
    max_pages: int
    max_decoded_stream_bytes: int
    max_text_bytes: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class _OcrLimits:
    max_text_bytes: int
    timeout_seconds: float


class _PdfBudgetExceeded(BaseException):
    """Stop pypdf work even where the library handles ordinary exceptions."""


class _PdfEncrypted(RuntimeError):
    """The parsed PDF is encrypted and therefore cannot be converted locally."""


class _PdfDependencyUnavailable(RuntimeError):
    """The required local PDF extraction library is unavailable."""


class _PdfBudget:
    def __init__(self, limits: _PdfLimits) -> None:
        self._limits = limits
        self._started_at = monotonic()
        self._pages = 0
        self._decoded_stream_bytes = 0
        self._text_bytes = 0

    def add_page(self) -> None:
        self._check_time()
        self._pages += 1
        if self._pages > self._limits.max_pages:
            raise _PdfBudgetExceeded

    def add_decoded_stream_bytes(self, size: int) -> None:
        self._check_time()
        self._decoded_stream_bytes += size
        if self._decoded_stream_bytes > self._limits.max_decoded_stream_bytes:
            raise _PdfBudgetExceeded

    def add_text(self, value: str) -> None:
        for index, character in enumerate(value):
            if index % 4_096 == 0:
                self._check_time()
            code_point = ord(character)
            self._text_bytes += (
                1
                if code_point < 0x80
                else 2
                if code_point < 0x800
                else 3
                if code_point < 0x10000
                else 4
            )
            if self._text_bytes > self._limits.max_text_bytes:
                raise _PdfBudgetExceeded

    def remaining_decoded_stream_bytes(self) -> int:
        self._check_time()
        remaining = self._limits.max_decoded_stream_bytes - self._decoded_stream_bytes
        if remaining <= 0:
            raise _PdfBudgetExceeded
        return remaining

    def _check_time(self) -> None:
        if monotonic() - self._started_at > self._limits.timeout_seconds:
            raise _PdfBudgetExceeded


class _BoundedPdfPageList(list["PageObject"]):
    """Bound pypdf's otherwise eager page-tree materialization."""

    def __init__(self, budget: _PdfBudget) -> None:
        super().__init__()
        self._budget = budget

    def append(self, page: PageObject) -> None:
        self._budget.add_page()
        super().append(page)


def _pdf_to_text(data: bytes) -> str:
    if not data.startswith(b"%PDF-"):
        raise UnsupportedDocumentFormat(
            "application/pdf", "about:blank", "PDF üst bilgisi geçersiz."
        )
    if len(data) > _MAX_PDF_INPUT_BYTES:
        raise _pdf_budget_error()

    result = _run_pdf_worker(
        data,
        _PdfLimits(
            max_pages=_MAX_PDF_PAGES,
            max_decoded_stream_bytes=_MAX_PDF_DECODED_STREAM_BYTES,
            max_text_bytes=_MAX_PDF_TEXT_BYTES,
            timeout_seconds=_PDF_PARSE_TIMEOUT_SECONDS,
        ),
    )
    if result[0] == "budget":
        raise _pdf_budget_error()
    if result[0] == "encrypted":
        raise UnsupportedDocumentFormat(
            "application/pdf",
            "about:blank",
            "Şifrelenmiş PDF'ler yerelde dönüştürülemez.",
        )
    if result[0] == "unavailable":
        raise UnsupportedDocumentFormat(
            "application/pdf",
            "about:blank",
            "PDF metnini çıkarmak için pypdf gerekir.",
        )
    if result[0] != "ok":
        raise UnsupportedDocumentFormat(
            "application/pdf", "about:blank", "PDF belgesi geçersiz."
        )
    return result[1]


def _run_pdf_worker(data: bytes, limits: _PdfLimits) -> tuple[str, str]:
    return _run_conversion_worker(
        _pdf_worker,
        (data, limits),
        limits.timeout_seconds,
    )


def _run_ocr_worker(
    ocr: OcrCallable | OcrConverter,
    data: bytes,
    source_url: str,
    limits: _OcrLimits,
) -> tuple[str, str]:
    return _run_conversion_worker(
        _ocr_worker,
        (ocr, data, source_url, limits),
        limits.timeout_seconds,
    )


def _run_conversion_worker(
    target: Callable[..., None],
    arguments: tuple[object, ...],
    timeout_seconds: float,
) -> tuple[str, str]:
    context = get_context("spawn")
    results: Queue[tuple[str, str]] = context.Queue(maxsize=1)
    worker = context.Process(target=target, args=(*arguments, results))
    worker.daemon = True
    try:
        # macOS uses spawn by default. Never fall back to direct invocation when
        # a callback cannot be pickled, because that would bypass this timeout.
        worker.start()
        deadline = monotonic() + timeout_seconds
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                return ("budget", "")
            try:
                return results.get(timeout=min(remaining, 0.1))
            except Empty:
                if not worker.is_alive():
                    return ("failure", "")
            except (EOFError, OSError, ValueError):
                return ("failure", "")
    except (AttributeError, OSError, pickle.PicklingError, RuntimeError, TypeError):
        return ("failure", "")
    finally:
        _stop_worker(worker)
        results.close()
        results.join_thread()


def _stop_worker(worker: BaseProcess) -> None:
    if worker.pid is None:
        return
    worker.join(timeout=0.1)
    if worker.is_alive():
        worker.terminate()
        worker.join(timeout=0.1)
    if worker.is_alive():
        worker.kill()
        worker.join(timeout=0.1)


def _pdf_worker(
    data: bytes,
    limits: _PdfLimits,
    results: Queue[tuple[str, str]],
) -> None:
    try:
        results.put(("ok", _extract_pdf_text(data, limits)))
    except _PdfBudgetExceeded:
        results.put(("budget", ""))
    except _PdfEncrypted:
        results.put(("encrypted", ""))
    except _PdfDependencyUnavailable:
        results.put(("unavailable", ""))
    except Exception:  # noqa: BLE001 - Worker must translate all parser failures into an invalid result across the IPC boundary.
        results.put(("failure", ""))


def _ocr_worker(
    ocr: OcrCallable | OcrConverter,
    data: bytes,
    source_url: str,
    limits: _OcrLimits,
    results: Queue[tuple[str, str]],
) -> None:
    try:
        result = _normalize_ocr_output(
            _invoke_ocr(ocr, data, source_url),
            limits.max_text_bytes,
        )
        results.put(("ok", result))
    except _PdfBudgetExceeded:
        results.put(("budget", ""))
    except Exception:  # noqa: BLE001 - Worker must not expose OCR failures or output.
        results.put(("failure", ""))


def _extract_pdf_text(data: bytes, limits: _PdfLimits) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import LimitReachedError as PypdfLimitReachedError
        from pypdf.generic import DictionaryObject
    except ImportError as exc:
        raise _PdfDependencyUnavailable from exc

    budget = _PdfBudget(limits)
    with _bounded_pypdf_streams(budget):
        try:
            reader = PdfReader(BytesIO(data))
            budget._check_time()
            if reader.is_encrypted:
                raise _PdfEncrypted

            # pypdf exposes pages through an eager flattening operation. Its
            # private traversal is the only API that lets us supply a bounded
            # destination list before that materialization begins.
            pages = _BoundedPdfPageList(budget)
            reader.flattened_pages = pages
            pages_root = reader.root_object["/Pages"].get_object()
            if not isinstance(pages_root, DictionaryObject):
                raise TypeError("PDF sayfa ağacı sözlük olmalıdır")
            reader._flatten(pages=pages_root)

            text = StringIO()
            visitor = _pdf_text_budget_visitor(budget)
            for page in pages:
                page_text = page.extract_text(visitor_text=visitor) or ""
                if not isinstance(page_text, str):
                    raise TypeError("PDF sayfa metni str türünde olmalıdır")
                _append_pdf_text(text, page_text)
            return text.getvalue()
        except PypdfLimitReachedError as exc:
            raise _PdfBudgetExceeded from exc


@contextmanager
def _bounded_pypdf_streams(budget: _PdfBudget) -> Iterator[None]:
    """Meter data returned by pypdf, including indirect-filter object streams."""
    from pypdf import filters
    from pypdf.errors import LimitReachedError as PypdfLimitReachedError
    from pypdf.generic import EncodedStreamObject, StreamObject

    original_stream_get_data = StreamObject.get_data
    original_encoded_get_data = EncodedStreamObject.get_data
    original_limits: dict[str, int] = {}
    for name in _PYPDF_STREAM_LIMIT_NAMES:
        try:
            value = getattr(filters, name)
        except AttributeError:
            continue
        if not isinstance(value, int):
            raise TypeError("pypdf akış sınırı tamsayı olmalıdır")
        original_limits[name] = value
    metered_stream_ids: set[int] = set()

    def set_pypdf_stream_limits() -> None:
        remaining = budget.remaining_decoded_stream_bytes()
        for name in original_limits:
            setattr(filters, name, remaining)

    def bounded_stream_get_data(stream: StreamObject) -> bytes:
        value = original_stream_get_data(stream)
        stream_id = id(stream)
        if stream_id not in metered_stream_ids:
            budget.add_decoded_stream_bytes(len(value))
            metered_stream_ids.add(stream_id)
        return value

    def bounded_encoded_get_data(stream: EncodedStreamObject) -> bytes:
        try:
            if stream.decoded_self is None:
                set_pypdf_stream_limits()
            return original_encoded_get_data(stream)
        except PypdfLimitReachedError as exc:
            raise _PdfBudgetExceeded from exc

    try:
        set_pypdf_stream_limits()
        with (
            patch.object(StreamObject, "get_data", bounded_stream_get_data),
            patch.object(EncodedStreamObject, "get_data", bounded_encoded_get_data),
        ):
            yield
    finally:
        for name, value in original_limits.items():
            setattr(filters, name, value)


def _pdf_text_budget_visitor(budget: _PdfBudget) -> Callable[..., None]:
    def visit_text(value: str, *_: object) -> None:
        if not isinstance(value, str):
            raise TypeError("PDF ziyaretçi metni str türünde olmalıdır")
        budget.add_text(value)

    return visit_text


def _append_pdf_text(output: StringIO, value: str) -> None:
    if not value:
        return
    if output.tell():
        output.write("\n")
    output.write(value)


def _pdf_budget_error() -> UnsupportedDocumentFormat:
    return UnsupportedDocumentFormat(
        "application/pdf",
        "about:blank",
        "PDF dönüştürme işlemi güvenlik sınırlarını aştı.",
    )


def _docx_to_markdown(data: bytes, source_url: str) -> str:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsupportedDocumentFormat(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            source_url,
            "DOCX arşivi geçersiz.",
        ) from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_DOCX_MEMBERS:
            raise UnsupportedDocumentFormat(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                source_url,
                "DOCX arşivinde çok fazla üye var.",
            )
        total_size = sum(info.file_size for info in infos)
        if total_size > _MAX_DOCX_UNCOMPRESSED_BYTES:
            raise UnsupportedDocumentFormat(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                source_url,
                "DOCX arşivi izin verilen boyut sınırını aşıyor.",
            )
        for info in infos:
            if (
                PurePosixPath(info.filename).is_absolute()
                or ".." in PurePosixPath(info.filename).parts
            ):
                raise UnsupportedDocumentFormat(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    source_url,
                    "DOCX üye yolu güvenli değil.",
                )
            if (
                info.compress_size
                and info.file_size / info.compress_size > _MAX_DOCX_COMPRESSION_RATIO
            ):
                raise UnsupportedDocumentFormat(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    source_url,
                    "DOCX arşivinin sıkıştırma oranı güvenli sınırı aşıyor.",
                )
        try:
            document_xml = archive.read("word/document.xml")
        except KeyError as exc:
            raise UnsupportedDocumentFormat(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                source_url,
                "DOCX document.xml girdisi eksik.",
            ) from exc
    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as exc:
        raise UnsupportedDocumentFormat(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            source_url,
            "DOCX XML belgesi geçersiz.",
        ) from exc

    blocks: list[str] = []
    body = root.find(f"{_WORD}body")
    if body is None:
        return ""
    for child in body:
        if child.tag == f"{_WORD}p":
            paragraph = _docx_paragraph(child)
            if paragraph:
                blocks.append(paragraph)
        elif child.tag == f"{_WORD}tbl":
            table = _docx_table(child)
            if table:
                blocks.append("\n".join(table))
    return _normalize_markdown("\n\n".join(blocks))


def _docx_paragraph(paragraph: ET.Element) -> str:
    text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD}t")).strip()
    if not text:
        return ""
    style = paragraph.find(f"{_WORD}pPr/{_WORD}pStyle")
    style_name = "" if style is None else style.attrib.get(f"{_WORD}val", "")
    heading = re.search(r"heading\s*([1-6])$", style_name, re.IGNORECASE)
    if heading:
        return f"{'#' * int(heading.group(1))} {text}"
    if paragraph.find(f"{_WORD}pPr/{_WORD}numPr") is not None:
        return f"- {text}"
    return text


def _docx_table(table: ET.Element) -> list[str]:
    rows: list[list[str]] = []
    for row in table.findall(f"{_WORD}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{_WORD}tc"):
            text = " ".join(node.text or "" for node in cell.iter(f"{_WORD}t")).strip()
            cells.append(text.replace("|", r"\|"))
        if cells:
            rows.append(cells)
    if not rows:
        return []
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    rendered = [
        "| " + " | ".join(normalized[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    rendered.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
    return rendered


def _call_ocr(
    ocr: OcrCallable | OcrConverter,
    data: bytes,
    source_url: str,
) -> str:
    result = _run_ocr_worker(
        ocr,
        data,
        source_url,
        _OcrLimits(
            max_text_bytes=_MAX_OCR_TEXT_BYTES,
            timeout_seconds=_OCR_TIMEOUT_SECONDS,
        ),
    )
    if result[0] == "budget":
        raise _pdf_budget_error()
    if result[0] != "ok":
        raise UnsupportedDocumentFormat(
            "application/pdf",
            source_url,
            "Yerel OCR dönüştürmesi başarısız oldu.",
        )
    return result[1]


def _invoke_ocr(
    ocr: OcrCallable | OcrConverter,
    data: bytes,
    source_url: str,
) -> str | bytes:
    if isinstance(ocr, OcrConverter):
        return ocr.convert(data, source_url)
    return ocr(data, source_url)


def _normalize_ocr_output(result: str | bytes, max_text_bytes: int) -> str:
    if isinstance(result, bytes):
        if len(result) > max_text_bytes:
            raise _PdfBudgetExceeded
        value = _decode_text(result)
    elif isinstance(result, str):
        value = result
    else:
        raise TypeError("OCR, str veya bytes döndürmelidir")
    _ensure_text_within_budget(value, max_text_bytes)
    markdown = _normalize_markdown(value)
    _ensure_text_within_budget(markdown, max_text_bytes)
    return markdown


def _ensure_text_within_budget(value: str, max_text_bytes: int) -> None:
    size = 0
    for character in value:
        code_point = ord(character)
        size += (
            1
            if code_point < 0x80
            else 2
            if code_point < 0x800
            else 3
            if code_point < 0x10000
            else 4
        )
        if size > max_text_bytes:
            raise _PdfBudgetExceeded


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "windows-1254", "iso-8859-9"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _has_meaningful_text(value: str) -> bool:
    return bool(re.search(r"[\w\u00c0-\u024f]", value))


def _normalize_markdown(markdown: str) -> str:
    value = unescape(markdown).replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    lines = [line.rstrip() for line in value.split("\n")]
    normalized: list[str] = []
    blank = False
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            normalized.append(line.strip())
            blank = False
            continue
        if not in_fence:
            line = re.sub(r"[ \t]+", " ", line).strip() if line.strip() else ""
        if not line:
            if normalized and not blank:
                normalized.append("")
            blank = True
        else:
            normalized.append(line)
            blank = False
    while normalized and not normalized[-1]:
        normalized.pop()
    return "\n".join(normalized) + ("\n" if normalized else "")
