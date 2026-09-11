"""Single WAL SQLite cache for metadata and normalized content."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from mutalaamcp.domain.models import ConversionMethod

SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 2 * 1024**3
DEFAULT_BUSY_TIMEOUT_MS = 5000
_SHA256_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")


class _Missing:
    __slots__ = ()


_MISSING = _Missing()
_SCHEMA_ID = 1

_OWNER_DIRECTORY_MODE = 0o700
_OWNER_FILE_MODE = 0o600


class CacheError(RuntimeError):
    """Base error for cache store failures."""


class SchemaError(CacheError):
    """Cache database schema is missing, newer than this code, or unrecognized."""


class Freshness(StrEnum):
    FRESH = "fresh"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class CacheRecord:
    namespace: str
    key: str
    source_url: str
    content: str
    content_hash: str
    fetched_at: datetime
    validated_at: datetime
    expires_at: datetime | None
    accessed_at: datetime
    etag: str | None
    last_modified: str | None
    mime_type: str | None
    conversion: ConversionMethod | None
    payload_bytes: int
    freshness: Freshness


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


class CacheStore:
    """WAL SQLite store: one transaction writes metadata and normalized body.

    Entries are addressed by ``(namespace, key)``. Callers that cache search
    results must hash canonical parameters into ``key``; the raw query is never
    stored. A crash mid-write leaves the previous committed row intact.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes en az 0 olmalıdır.")
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms en az 0 olmalıdır.")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        _ensure_private_cache_directory(self.path.parent)
        _ensure_private_regular_file(self.path, missing_ok=True)
        self._connect()
        try:
            self._ensure_schema()
        except sqlite3.Error as exc:
            self.close()
            raise CacheError(f"Önbellek şeması işlenemedi: {self.path}") from exc
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(
        self,
        namespace: str,
        key: str,
        *,
        now: datetime | None = None,
    ) -> CacheRecord | None:
        """Return a stored entry, distinguishing fresh vs expired. Miss is None."""
        _require_identity(namespace, key)
        moment = _as_utc(now)
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT
                    e.namespace, e.key, e.source_url, c.body, e.content_hash,
                    e.fetched_at, e.validated_at, e.expires_at, e.accessed_at,
                    e.etag, e.last_modified, e.mime_type, e.conversion,
                    e.payload_bytes
                FROM cache_entry AS e
                JOIN cache_content AS c USING (namespace, key)
                WHERE e.namespace = ? AND e.key = ?
                """,
                (namespace, key),
            ).fetchone()
            if row is None:
                orphan = conn.execute(
                    "SELECT 1 FROM cache_entry WHERE namespace = ? AND key = ?",
                    (namespace, key),
                ).fetchone()
                if orphan is not None:
                    conn.execute(
                        "DELETE FROM cache_entry WHERE namespace = ? AND key = ?",
                        (namespace, key),
                    )
                return None
            accessed = _iso(moment)
            conn.execute(
                """
                UPDATE cache_entry
                SET accessed_at = ?
                WHERE namespace = ? AND key = ?
                """,
                (accessed, namespace, key),
            )
            return _record_from_row(row, now=moment, accessed_at=moment)

    def put(
        self,
        *,
        namespace: str,
        key: str,
        source_url: str,
        content: str,
        fetched_at: datetime,
        validated_at: datetime,
        expires_at: datetime | None,
        content_hash: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        mime_type: str | None = None,
        conversion: ConversionMethod | str | None = None,
        now: datetime | None = None,
    ) -> CacheRecord:
        """Write metadata and body together. LRU-evicts other rows by payload bytes."""
        _require_identity(namespace, key)
        _require_absolute_uri(source_url)
        if not isinstance(content, str):
            raise TypeError("content str türünde olmalıdır.")
        payload_bytes = len(content.encode("utf-8"))
        digest = content_hash if content_hash is not None else _hash_content(content)
        _require_content_hash(digest, content)
        converted = _coerce_conversion(conversion)
        moment = _as_utc(now)
        fetched = _as_utc(fetched_at)
        validated = _as_utc(validated_at)
        expires = None if expires_at is None else _as_utc(expires_at)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO cache_entry (
                    namespace, key, source_url, content_hash,
                    fetched_at, validated_at, expires_at, accessed_at,
                    etag, last_modified, mime_type, conversion, payload_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    source_url = excluded.source_url,
                    content_hash = excluded.content_hash,
                    fetched_at = excluded.fetched_at,
                    validated_at = excluded.validated_at,
                    expires_at = excluded.expires_at,
                    accessed_at = excluded.accessed_at,
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    mime_type = excluded.mime_type,
                    conversion = excluded.conversion,
                    payload_bytes = excluded.payload_bytes
                """,
                (
                    namespace,
                    key,
                    source_url,
                    digest,
                    _iso(fetched),
                    _iso(validated),
                    None if expires is None else _iso(expires),
                    _iso(moment),
                    etag,
                    last_modified,
                    mime_type,
                    None if converted is None else converted.value,
                    payload_bytes,
                ),
            )
            conn.execute(
                """
                INSERT INTO cache_content (namespace, key, body)
                VALUES (?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET body = excluded.body
                """,
                (namespace, key, content),
            )
            _evict_lru(conn, max_bytes=self.max_bytes, protect=(namespace, key))
        freshness = _freshness(expires, moment)
        return CacheRecord(
            namespace=namespace,
            key=key,
            source_url=source_url,
            content=content,
            content_hash=digest,
            fetched_at=fetched,
            validated_at=validated,
            expires_at=expires,
            accessed_at=moment,
            etag=etag,
            last_modified=last_modified,
            mime_type=mime_type,
            conversion=converted,
            payload_bytes=payload_bytes,
            freshness=freshness,
        )

    def mark_validated(
        self,
        namespace: str,
        key: str,
        *,
        validated_at: datetime,
        expires_at: datetime | None | object = _MISSING,
        etag: str | None | object = _MISSING,
        last_modified: str | None | object = _MISSING,
    ) -> CacheRecord:
        """Record a successful revalidation without rewriting normalized content."""
        _require_identity(namespace, key)
        validated = _as_utc(validated_at)
        with self._tx() as conn:
            row = conn.execute(
                """
                SELECT
                    e.namespace, e.key, e.source_url, c.body, e.content_hash,
                    e.fetched_at, e.validated_at, e.expires_at, e.accessed_at,
                    e.etag, e.last_modified, e.mime_type, e.conversion,
                    e.payload_bytes
                FROM cache_entry AS e
                JOIN cache_content AS c USING (namespace, key)
                WHERE e.namespace = ? AND e.key = ?
                """,
                (namespace, key),
            ).fetchone()
            if row is None:
                raise KeyError((namespace, key))
            new_expires = (
                row["expires_at"]
                if expires_at is _MISSING
                else (
                    None if expires_at is None else _iso(_as_utc(expires_at))  # type: ignore[arg-type]
                )
            )
            new_etag = row["etag"] if etag is _MISSING else etag
            new_last_modified = (
                row["last_modified"] if last_modified is _MISSING else last_modified
            )
            conn.execute(
                """
                UPDATE cache_entry
                SET validated_at = ?, expires_at = ?, etag = ?, last_modified = ?
                WHERE namespace = ? AND key = ?
                """,
                (
                    _iso(validated),
                    new_expires,
                    new_etag,
                    new_last_modified,
                    namespace,
                    key,
                ),
            )
            updated = conn.execute(
                """
                SELECT
                    e.namespace, e.key, e.source_url, c.body, e.content_hash,
                    e.fetched_at, e.validated_at, e.expires_at, e.accessed_at,
                    e.etag, e.last_modified, e.mime_type, e.conversion,
                    e.payload_bytes
                FROM cache_entry AS e
                JOIN cache_content AS c USING (namespace, key)
                WHERE e.namespace = ? AND e.key = ?
                """,
                (namespace, key),
            ).fetchone()
        return _record_from_row(updated, now=_as_utc(None))

    def delete(self, namespace: str, key: str) -> bool:
        _require_identity(namespace, key)
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM cache_entry WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
            return cur.rowcount > 0

    def clear(self) -> None:
        try:
            with self._tx() as conn:
                conn.execute("DELETE FROM cache_entry")
            with self._lock:
                self._require_conn().execute("VACUUM")
                self._secure_live_files()
        except sqlite3.Error as exc:
            raise CacheError(f"Önbellek temizlenemedi: {self.path}") from exc

    def backup(self, destination: str | Path | None = None) -> Path:
        """Write a consistent SQLite snapshot. Used before schema migration."""
        dest = destination if destination is not None else self._default_backup_path()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = self._require_conn()
            dest_conn = sqlite3.connect(dest)
            try:
                conn.backup(dest_conn)
            finally:
                dest_conn.close()
        return dest

    def restore(self, backup_path: str | Path) -> None:
        """Replace the live database with a previously taken backup.

        Copies the backup to a same-directory temporary file, flushes and
        fsyncs it, validates it as SQLite, then atomically replaces the live
        database. The backup is left in place until reconnection succeeds.
        This method does not acquire the process-wide serve/mutation lock;
        callers that must exclude ``serve`` wrap it themselves.
        """
        backup_path = Path(backup_path)
        if not _is_safe_regular_file(backup_path):
            raise FileNotFoundError(f"Yedek dosyası bulunamadı: {backup_path}")
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            tmp_path = None
            replaced = False
            try:
                tmp_path = _write_restore_temp(backup_path, self.path)
                _validate_sqlite_file(tmp_path, display_path=backup_path)
                Path(f"{self.path}-wal").unlink(missing_ok=True)
                Path(f"{self.path}-shm").unlink(missing_ok=True)
                os.replace(tmp_path, self.path)
                self._secure_live_files()
                replaced = True
                tmp_path = None
                _fsync_directory(self.path.parent)
                self._connect_locked()
            except BaseException:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                if not replaced and self._conn is None:
                    try:
                        self._connect_locked()
                    except (sqlite3.Error, OSError, CacheError):
                        self.close()
                raise

    def _connect(self) -> None:
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        _ensure_private_regular_file(self.path, missing_ok=True)
        for suffix in ("-wal", "-shm"):
            _ensure_private_regular_file(Path(f"{self.path}{suffix}"), missing_ok=True)
        try:
            conn = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise CacheError(
                f"Önbellek veritabanına bağlanılamadı: {self.path}"
            ) from exc
        try:
            conn.row_factory = sqlite3.Row
            _ensure_private_regular_file(self.path, missing_ok=False)
            self._configure(conn)
            self._secure_live_files()
        except sqlite3.Error as exc:
            conn.close()
            raise CacheError(
                f"Önbellek veritabanı yapılandırılamadı: {self.path}"
            ) from exc
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def _secure_live_files(self) -> None:
        _ensure_private_regular_file(self.path, missing_ok=False)
        for suffix in ("-wal", "-shm"):
            _ensure_private_regular_file(Path(f"{self.path}{suffix}"), missing_ok=True)

    def _configure(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise CacheError(f"WAL etkinleştirilemedi; alınan değer: {mode!r}")
        conn.execute("PRAGMA foreign_keys = ON")
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if not enabled:
            raise CacheError("Yabancı anahtarlar etkinleştirilemedi.")
        conn.execute("PRAGMA synchronous = NORMAL")

    def _ensure_schema(self) -> None:
        with self._tx() as conn:
            version = _read_version(conn)
            if version is None:
                _create_schema(conn)
                return
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise SchemaError(
                    f"Önbellek şeması {version}, desteklenen {SCHEMA_VERSION} "
                    "sürümünden daha yeni."
                )
        self._migrate(version)

    def _migrate(self, version: int) -> None:
        backup_path = self.backup()
        try:
            with self._tx() as conn:
                current = version
                while current < SCHEMA_VERSION:
                    migrator = _MIGRATIONS.get(current)
                    if migrator is None:
                        raise SchemaError(f"{current} şema sürümünden geçiş yok.")
                    migrator(conn)
                    current += 1
                    conn.execute(
                        "UPDATE schema_meta SET version = ? WHERE id = ?",
                        (current, _SCHEMA_ID),
                    )
        except Exception:
            self.restore(backup_path)
            raise

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._require_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            self._secure_live_files()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise CacheError("Önbellek deposu kapalı.")
        return self._conn

    def _default_backup_path(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return self.path.with_name(f"{self.path.stem}-v{SCHEMA_VERSION}-{stamp}.bak")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE schema_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE cache_entry (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            source_url TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            validated_at TEXT NOT NULL,
            expires_at TEXT,
            accessed_at TEXT NOT NULL,
            etag TEXT,
            last_modified TEXT,
            mime_type TEXT,
            conversion TEXT,
            payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
            PRIMARY KEY (namespace, key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE cache_content (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            body TEXT NOT NULL,
            PRIMARY KEY (namespace, key),
            FOREIGN KEY (namespace, key)
                REFERENCES cache_entry (namespace, key)
                ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_cache_entry_accessed_at ON cache_entry (accessed_at)"
    )
    conn.execute(
        "INSERT INTO schema_meta (id, version) VALUES (?, ?)",
        (_SCHEMA_ID, SCHEMA_VERSION),
    )


def _read_version(conn: sqlite3.Connection) -> int | None:
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not names:
        return None
    if "schema_meta" not in names:
        raise SchemaError("Tanımlanamayan önbellek veritabanı.")
    row = conn.execute(
        "SELECT version FROM schema_meta WHERE id = ?",
        (_SCHEMA_ID,),
    ).fetchone()
    if row is None:
        raise SchemaError("schema_meta içinde sürüm satırı eksik.")
    return int(row[0])


def _evict_lru(
    conn: sqlite3.Connection,
    *,
    max_bytes: int,
    protect: tuple[str, str],
) -> None:
    total = conn.execute(
        "SELECT COALESCE(SUM(payload_bytes), 0) FROM cache_entry"
    ).fetchone()[0]
    if total <= max_bytes:
        return
    rows = conn.execute(
        """
        SELECT namespace, key, payload_bytes
        FROM cache_entry
        ORDER BY accessed_at ASC, namespace ASC, key ASC
        """
    ).fetchall()
    for namespace, key, size in rows:
        if (namespace, key) == protect:
            continue
        conn.execute(
            "DELETE FROM cache_entry WHERE namespace = ? AND key = ?",
            (namespace, key),
        )
        total -= size
        if total <= max_bytes:
            return


def _record_from_row(
    row: sqlite3.Row,
    *,
    now: datetime,
    accessed_at: datetime | None = None,
) -> CacheRecord:
    expires = _parse_optional_dt(row["expires_at"])
    accessed = accessed_at if accessed_at is not None else _parse_dt(row["accessed_at"])
    conversion_raw = row["conversion"]
    return CacheRecord(
        namespace=row["namespace"],
        key=row["key"],
        source_url=row["source_url"],
        content=row["body"],
        content_hash=row["content_hash"],
        fetched_at=_parse_dt(row["fetched_at"]),
        validated_at=_parse_dt(row["validated_at"]),
        expires_at=expires,
        accessed_at=accessed,
        etag=row["etag"],
        last_modified=row["last_modified"],
        mime_type=row["mime_type"],
        conversion=_coerce_conversion(conversion_raw),
        payload_bytes=int(row["payload_bytes"]),
        freshness=_freshness(expires, now),
    )


def _freshness(expires_at: datetime | None, now: datetime) -> Freshness:
    if expires_at is None or now < expires_at:
        return Freshness.FRESH
    return Freshness.EXPIRED


def _hash_content(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _require_content_hash(digest: str, content: str) -> None:
    if _SHA256_HASH.match(digest) is None:
        raise ValueError(
            "content_hash sha256:<64 küçük harfli onaltılık karakter> "
            "biçiminde olmalıdır."
        )
    expected = _hash_content(content)
    if digest != expected:
        raise ValueError("content_hash içerikle eşleşmiyor.")


def _require_identity(namespace: str, key: str) -> None:
    if not namespace:
        raise ValueError("namespace boş olmamalıdır.")
    if not key:
        raise ValueError("key boş olmamalıdır.")


def _require_absolute_uri(value: str) -> None:
    if "://" not in value or any(ch.isspace() for ch in value):
        raise ValueError("source_url mutlak bir URI olmalıdır.")


def _coerce_conversion(
    value: ConversionMethod | str | None,
) -> ConversionMethod | None:
    if value is None or isinstance(value, ConversionMethod):
        return value
    try:
        return ConversionMethod(value)
    except ValueError as exc:
        raise ValueError("conversion geçerli bir dönüşüm yöntemi olmalıdır.") from exc


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_dt(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


def _parse_optional_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_dt(value)


def _is_safe_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _ensure_private_cache_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise CacheError(f"Önbellek üst dizini bir dizin olmalıdır: {path}")
        if os.name == "posix":
            os.chmod(path, _OWNER_DIRECTORY_MODE)
    except CacheError:
        raise
    except OSError as exc:
        raise CacheError(f"Önbellek dizini güvenli hale getirilemedi: {path}") from exc


def _ensure_private_regular_file(path: Path, *, missing_ok: bool) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing_ok:
            return
        raise CacheError(f"Önbellek dosyası bulunamadı: {path}") from None
    except OSError as exc:
        raise CacheError(f"Önbellek dosyası incelenemedi: {path}") from exc
    if not stat.S_ISREG(mode):
        raise CacheError(f"Önbellek yolu normal bir dosya olmalıdır: {path}")
    try:
        if os.name == "posix":
            os.chmod(path, _OWNER_FILE_MODE)
    except OSError as exc:
        raise CacheError(f"Önbellek dosyası güvenli hale getirilemedi: {path}") from exc


def _write_restore_temp(backup_path: Path, live_path: Path) -> Path:
    if not _is_safe_regular_file(backup_path):
        raise CacheError(f"Yedek normal bir dosya olmalıdır: {backup_path}")
    fd, raw = tempfile.mkstemp(
        prefix=f".{live_path.name}.restore-",
        suffix=".tmp",
        dir=live_path.parent,
    )
    tmp_path = Path(raw)
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        source_fd = os.open(backup_path, flags)
        with os.fdopen(fd, "wb") as outf, os.fdopen(source_fd, "rb") as inf:
            shutil.copyfileobj(inf, outf)
            outf.flush()
            os.fsync(outf.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _validate_sqlite_file(path: Path, *, display_path: Path | None = None) -> None:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise CacheError(
            f"Yedek geçerli bir SQLite veritabanı değil: {display_path or path}"
        ) from exc


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
