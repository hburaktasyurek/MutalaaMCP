"""Safe, bounded HTTP access for configured official upstreams.

``SafeHttpClient`` deliberately wraps an injected :class:`httpx.AsyncClient` rather
than creating a process-global client.  Callers therefore own connection-pool
lifetime while this module owns URL validation, redirect handling, response-size
limits, and the small common upstream failure vocabulary.
"""

from __future__ import annotations

import asyncio
import time
import zlib
from collections import deque
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(timeout=20.0, connect=10.0)
DEFAULT_MAX_RESPONSE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class SafeHttpError(RuntimeError):
    """Base exception raised for an upstream request that cannot be used."""


@dataclass(slots=True)
class UnsafeUrlError(SafeHttpError):
    """A URL falls outside the configured HTTPS upstream boundary."""

    url: str
    reason: str

    def __str__(self) -> str:
        return f"Güvenli olmayan üst kaynak URL'si ({self.reason}): {self.url}"


@dataclass(slots=True)
class ResponseTooLarge(SafeHttpError):
    """The upstream response exceeded the configured byte cap while streaming."""

    url: str
    max_bytes: int

    def __str__(self) -> str:
        return f"Üst kaynak yanıtı {self.max_bytes} bayt sınırını aştı: {self.url}"


@dataclass(slots=True)
class UpstreamError(SafeHttpError):
    """Structured common mapping for provider-facing HTTP failures."""

    message: str
    status_code: int | None = None
    retry_after: float | None = None
    stale_eligible: bool = True

    @property
    def code(self) -> str:
        raise NotImplementedError

    @property
    def retryable(self) -> bool:
        return self.code in {"upstream_rate_limited", "upstream_unavailable"}

    def __str__(self) -> str:
        return self.message


class UpstreamNotFound(UpstreamError):
    @property
    def code(self) -> str:
        return "not_found"


class UpstreamRateLimited(UpstreamError):
    @property
    def code(self) -> str:
        return "upstream_rate_limited"


class UpstreamUnavailable(UpstreamError):
    @property
    def code(self) -> str:
        return "upstream_unavailable"


Origin = tuple[str, str, int | None]


class OriginProtection:
    """One shared concurrency and rolling-rate budget for an upstream origin."""

    def __init__(
        self,
        *,
        capacity: int = 10,
        window_seconds: float = 30.0,
        max_concurrency: int = 2,
    ) -> None:
        if capacity < 1 or window_seconds <= 0 or max_concurrency < 1:
            raise ValueError(
                "capacity, window_seconds ve max_concurrency değerleri pozitif olmalıdır"
            )
        self._capacity = capacity
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self._concurrency = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def request_slot(self) -> AsyncIterator[None]:
        """Reserve capacity for exactly one upstream HTTP request."""
        await self._concurrency.acquire()
        try:
            await self._acquire_rate_budget()
            yield
        finally:
            self._concurrency.release()

    async def pause(self, retry_after: float | None) -> None:
        """Honor a source-wide 429 before another request reaches the origin."""
        delay = (
            min(max(retry_after if retry_after is not None else 30.0, 1.0), 60.0) + 0.5
        )
        async with self._lock:
            self._timestamps.clear()
            self._blocked_until = max(self._blocked_until, time.monotonic() + delay)

    async def _acquire_rate_budget(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while (
                    self._timestamps
                    and self._timestamps[0] <= now - self._window_seconds
                ):
                    self._timestamps.popleft()
                if now < self._blocked_until:
                    delay = self._blocked_until - now
                elif len(self._timestamps) < self._capacity:
                    self._timestamps.append(now)
                    return
                else:
                    delay = self._timestamps[0] + self._window_seconds - now
            await asyncio.sleep(max(delay, 0.001))


@asynccontextmanager
async def _source_request_slot(
    protection: OriginProtection | None,
) -> AsyncIterator[None]:
    if protection is None:
        yield
        return
    async with protection.request_slot():
        yield


class SafeHttpClient:
    """Make bounded requests only to explicit HTTPS hosts.

    ``allowed_hosts`` contains host names, not URL prefixes or suffixes.  A
    host is case-normalized for DNS comparison but otherwise matched exactly;
    ``example.gov.tr`` does not authorize ``sub.example.gov.tr``.  Ports and
    userinfo are rejected by default.  A port may be allowed for one exact host
    with ``allowed_ports``; this is intentionally exceptional because the V1
    upstreams use normal HTTPS origins.

    Responses are read in bounded chunks before being returned.  This makes the
    returned ``httpx.Response`` safe to use normally via ``.content``, ``.text``
    or ``.json()``, while preventing ``httpx`` from buffering an unbounded body.
    Redirects are followed here (not by the injected client) so every Location
    is subjected to the same host validation.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        allowed_hosts: Collection[str],
        *,
        allowed_ports: Mapping[str, Collection[int]] | None = None,
        allow_userinfo: bool = False,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        origin_protections: Mapping[Origin, OriginProtection] | None = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts boş olamaz")
        if max_bytes < 1:
            raise ValueError("max_bytes pozitif olmalıdır")
        if max_redirects < 0:
            raise ValueError("max_redirects sıfır veya daha büyük olmalıdır")
        if timeout is None:  # type: ignore[comparison-overlap]
            raise ValueError("timeout yapılandırılmalıdır")

        normalized_hosts = frozenset(
            _normalize_configured_host(host) for host in allowed_hosts
        )
        configured_ports: dict[str, frozenset[int]] = {}
        for host, ports in (allowed_ports or {}).items():
            normalized_host = _normalize_configured_host(host)
            if normalized_host not in normalized_hosts:
                raise ValueError(
                    "allowed_ports içindeki ana makine allowed_hosts içinde olmalıdır"
                )
            port_set = frozenset(int(port) for port in ports)
            if any(port < 1 or port > 65535 for port in port_set):
                raise ValueError("İzin verilen portlar 1..65535 aralığında olmalıdır")
            configured_ports[normalized_host] = port_set

        self._client = client
        self._allowed_hosts = normalized_hosts
        self._allowed_ports = configured_ports
        self._allow_userinfo = allow_userinfo
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._origin_protections = dict(origin_protections or {})

    async def request(
        self,
        method: str,
        url: str | httpx.URL,
        **kwargs: Any,
    ) -> httpx.Response:
        """Return a fully-read bounded response or raise a structured error.

        Per-request ``timeout`` may make a request stricter, but ``None`` is
        rejected so a caller cannot accidentally disable the bounded client
        policy.  ``follow_redirects`` is intentionally not accepted: redirects
        are validated and followed hop-by-hop by this method.
        """
        if kwargs.pop("follow_redirects", False):
            raise ValueError("SafeHttpClient yönlendirmeleri kendisi yönetir")
        if kwargs.get("timeout", self._timeout) is None:
            raise ValueError("timeout None olamaz")
        kwargs.setdefault("timeout", self._timeout)

        initial_url = str(url)
        self._validate_url(initial_url)
        try:
            request = self._client.build_request(method, initial_url, **kwargs)
        except (TypeError, ValueError) as exc:
            raise UnsafeUrlError(initial_url, "geçersiz istek") from exc

        history: list[httpx.Response] = []
        redirects = 0
        while True:
            protection = self._origin_protections.get(_origin(request.url))
            async with _source_request_slot(protection):
                try:
                    response = await self._client.send(
                        request, stream=True, follow_redirects=False
                    )
                except httpx.TimeoutException as exc:
                    raise UpstreamUnavailable(
                        "Üst kaynak isteği zaman aşımına uğradı."
                    ) from exc
                except httpx.RequestError as exc:
                    raise UpstreamUnavailable(
                        "Üst kaynak isteği başarısız oldu."
                    ) from exc

                if response.status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location")
                    if location is None:
                        await response.aclose()
                        raise UpstreamUnavailable(
                            "Üst kaynak, Location üst bilgisi olmayan bir yönlendirme döndürdü.",
                            status_code=response.status_code,
                            stale_eligible=False,
                        )
                    if redirects >= self._max_redirects:
                        await response.aclose()
                        raise UpstreamUnavailable(
                            "Üst kaynak yönlendirme sınırını aştı.",
                            status_code=response.status_code,
                            stale_eligible=False,
                        )
                    redirect_url = urljoin(str(request.url), location)
                    try:
                        self._validate_url(redirect_url)
                        next_request = self._redirect_request(
                            request, response.status_code, redirect_url
                        )
                    except BaseException:
                        await response.aclose()
                        raise
                    history.append(_history_response(response, request))
                    await response.aclose()
                    request = next_request
                    redirects += 1
                    continue

                try:
                    self._raise_for_status(response)
                    content, was_decoded = await self._read_bounded(response)
                    headers = response.headers.copy()
                except UpstreamRateLimited as exc:
                    if protection is not None:
                        await protection.pause(exc.retry_after)
                    raise
                finally:
                    await response.aclose()
                if was_decoded:
                    # The body is now representation data, so these upstream
                    # representation headers no longer describe it.
                    headers.pop("content-encoding", None)
                    headers.pop("content-length", None)
                reconstructed = httpx.Response(
                    status_code=response.status_code,
                    headers=headers,
                    content=content,
                    request=request,
                    extensions=response.extensions,
                    history=history,
                )
                if was_decoded:
                    reconstructed.headers.pop("content-length", None)
                return reconstructed

    def _validate_url(self, raw_url: str) -> None:
        parts = urlsplit(raw_url)
        if parts.scheme.lower() != "https":
            raise UnsafeUrlError(raw_url, "HTTPS gereklidir")
        if not parts.netloc or parts.hostname is None:
            raise UnsafeUrlError(raw_url, "ana makine gereklidir")
        if "@" in parts.netloc and not self._allow_userinfo:
            raise UnsafeUrlError(raw_url, "kullanıcı bilgisine izin verilmez")
        try:
            explicit_port = parts.port
        except ValueError as exc:
            raise UnsafeUrlError(raw_url, "geçersiz port") from exc
        host = parts.hostname.lower()
        if host not in self._allowed_hosts:
            raise UnsafeUrlError(raw_url, "ana makine izin listesinde değil")
        if explicit_port is not None and explicit_port not in self._allowed_ports.get(
            host, frozenset()
        ):
            raise UnsafeUrlError(raw_url, "açıkça belirtilen porta izin verilmez")

    def _redirect_request(
        self,
        request: httpx.Request,
        status_code: int,
        redirect_url: str,
    ) -> httpx.Request:
        method = request.method
        body: bytes | None = request.content
        if (status_code == 303 and method != "HEAD") or (
            status_code in {301, 302} and method == "POST"
        ):
            method = "GET"
            body = None

        headers = dict(request.headers)
        if _origin(request.url) != _origin(httpx.URL(redirect_url)):
            for header in ("authorization", "cookie", "proxy-authorization"):
                headers.pop(header, None)
        if body is None:
            for header in ("content-length", "content-type", "transfer-encoding"):
                headers.pop(header, None)
        return self._client.build_request(
            method, redirect_url, headers=headers, content=body
        )

    async def _read_bounded(self, response: httpx.Response) -> tuple[bytes, bool]:
        # Responses created with ``content=`` have already passed through HTTPX's
        # Content-Encoding decoder while retaining their original headers.  Their
        # body must be limited as decoded data, not passed through gzip again.
        if response.is_stream_consumed:
            content = response.content
            if len(content) > self._max_bytes:
                raise ResponseTooLarge(str(response.url), self._max_bytes)
            return content, self._has_gzip_content_encoding(response)

        decoder = self._content_decoder(response)
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                encoded_length = int(content_length)
            except ValueError:
                encoded_length = None
            if encoded_length is not None and encoded_length > self._max_bytes:
                raise ResponseTooLarge(str(response.url), self._max_bytes)

        body = bytearray()
        encoded_bytes = 0

        def consume(chunk: bytes) -> None:
            nonlocal encoded_bytes
            encoded_bytes += len(chunk)
            if encoded_bytes > self._max_bytes:
                raise ResponseTooLarge(str(response.url), self._max_bytes)
            if decoder is None:
                self._append_bounded(body, chunk, response)
            else:
                decoder.decode(chunk, body)

        try:
            async for chunk in response.aiter_raw():
                consume(chunk)
        except httpx.RequestError as exc:
            raise UpstreamUnavailable("Üst kaynak yanıt akışı başarısız oldu.") from exc

        if decoder is not None:
            decoder.finish(body)
        return bytes(body), decoder is not None

    def _content_decoder(self, response: httpx.Response) -> _BoundedGzipDecoder | None:
        if not self._has_gzip_content_encoding(response):
            return None
        return _BoundedGzipDecoder(str(response.url), self._max_bytes)

    @staticmethod
    def _has_gzip_content_encoding(response: httpx.Response) -> bool:
        encoding = response.headers.get("content-encoding")
        if encoding is None or encoding.strip().lower() == "identity":
            return False
        if encoding.strip().lower() in {"gzip", "x-gzip"}:
            return True
        raise UpstreamUnavailable(
            "Üst kaynak yanıtı desteklenmeyen bir içerik kodlaması kullanıyor.",
            stale_eligible=False,
        )

    def _append_bounded(
        self,
        body: bytearray,
        chunk: bytes,
        response: httpx.Response,
    ) -> None:
        if len(body) + len(chunk) > self._max_bytes:
            raise ResponseTooLarge(str(response.url), self._max_bytes)
        body.extend(chunk)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status_code = response.status_code
        if status_code == 404:
            raise UpstreamNotFound(
                "İstenen belge üst kaynakta bulunamadı.", status_code=status_code
            )
        if status_code == 429:
            raise UpstreamRateLimited(
                "Üst kaynak istek hızını sınırladı.",
                status_code=status_code,
                retry_after=_retry_after(response.headers.get("retry-after")),
            )
        if status_code >= 500:
            raise UpstreamUnavailable(
                "Üst kaynak şu anda kullanılamıyor.",
                status_code=status_code,
            )
        if status_code >= 400:
            raise UpstreamUnavailable(
                f"Üst kaynak isteği HTTP {status_code} ile reddetti.",
                status_code=status_code,
                stale_eligible=False,
            )


class _BoundedGzipDecoder:
    """Incrementally decode one gzip representation without exceeding the body cap."""

    def __init__(self, url: str, max_bytes: int) -> None:
        self._url = url
        self._max_bytes = max_bytes
        self._decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)

    def decode(self, chunk: bytes, output: bytearray) -> None:
        pending = chunk
        while pending:
            try:
                decoded = self._decompressor.decompress(
                    pending, self._max_bytes - len(output) + 1
                )
            except zlib.error as exc:
                raise self._invalid_encoding() from exc
            self._append(output, decoded)
            if self._decompressor.unused_data:
                raise self._invalid_encoding()
            pending = self._decompressor.unconsumed_tail
            if pending and not decoded:
                raise ResponseTooLarge(self._url, self._max_bytes)

    def finish(self, output: bytearray) -> None:
        try:
            decoded = self._decompressor.flush(self._max_bytes - len(output) + 1)
        except zlib.error as exc:
            raise self._invalid_encoding() from exc
        self._append(output, decoded)
        if not self._decompressor.eof or self._decompressor.unused_data:
            raise self._invalid_encoding()

    def _append(self, output: bytearray, decoded: bytes) -> None:
        if len(output) + len(decoded) > self._max_bytes:
            raise ResponseTooLarge(self._url, self._max_bytes)
        output.extend(decoded)

    @staticmethod
    def _invalid_encoding() -> UpstreamUnavailable:
        return UpstreamUnavailable(
            "Üst kaynak yanıtı geçersiz gzip kodlaması kullanıyor.",
            stale_eligible=False,
        )


def _normalize_configured_host(host: str) -> str:
    candidate = host.strip().lower()
    if not candidate or ":" in candidate or "/" in candidate or "@" in candidate:
        raise ValueError("allowed_hosts girdileri yalın ana makine adı olmalıdır")
    return candidate


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme.lower(), url.host.lower(), url.port)


def _history_response(
    response: httpx.Response, request: httpx.Request
) -> httpx.Response:
    """Keep redirect metadata without retaining a live body stream."""
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=b"",
        request=request,
        extensions=response.extensions,
    )


def _retry_after(value: str | None) -> float:
    """Return a non-negative delay, defaulting to the provider-safe 30 seconds."""
    if value is None:
        return 30.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 30.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds())
