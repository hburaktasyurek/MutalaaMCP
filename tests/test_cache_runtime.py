"""Behavioral tests for CacheStore, SingleFlight, and serve/mutation locks."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mutalaamcp.cache.singleflight import SingleFlight
from mutalaamcp.cache.store import SCHEMA_VERSION, CacheError, CacheStore, Freshness
from mutalaamcp.runtime import AlreadyRunning, FileLock, mutation_lock, serve_lock_held

NS = "docs"
SOURCE = "https://example.test/doc"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
T3 = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _store(tmp_path: Path, *, max_bytes: int = 1_000_000) -> CacheStore:
    return CacheStore(tmp_path / "cache.sqlite", max_bytes=max_bytes)


def _put(
    store: CacheStore,
    key: str,
    content: str,
    *,
    now: datetime,
    namespace: str = NS,
    source_url: str = SOURCE,
    expires_at: datetime | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
) -> object:
    return store.put(
        namespace=namespace,
        key=key,
        source_url=source_url,
        content=content,
        fetched_at=now,
        validated_at=now,
        expires_at=expires_at,
        etag=etag,
        last_modified=last_modified,
        now=now,
    )


def _columns(db_path: Path, table: str) -> list[str]:
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_wal_and_schema_initialization(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite"
    with _store(tmp_path) as store:
        _put(store, "seed", "body", now=T0)
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        fks = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert int(fks) == 1

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"schema_meta", "cache_entry", "cache_content"} <= tables
        version = conn.execute(
            "SELECT version FROM schema_meta WHERE id = 1"
        ).fetchone()[0]
        assert int(version) == SCHEMA_VERSION
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_get_fresh_versus_expired(tmp_path: Path) -> None:
    expires = T1
    with _store(tmp_path) as store:
        _put(store, "dated", "v1", now=T0, expires_at=expires)
        _put(store, "immortal", "v1", now=T0, expires_at=None)

        fresh = store.get(NS, "dated", now=T0)
        at_expiry = store.get(NS, "dated", now=T1)
        after = store.get(NS, "dated", now=T2)
        never = store.get(NS, "immortal", now=T3)

        assert fresh is not None and fresh.freshness is Freshness.FRESH
        assert at_expiry is not None and at_expiry.freshness is Freshness.EXPIRED
        assert after is not None and after.freshness is Freshness.EXPIRED
        assert after.content == "v1"
        assert never is not None and never.freshness is Freshness.FRESH
        assert store.get(NS, "missing", now=T0) is None


def test_put_replacement_rolls_back_metadata_and_content(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _put(
            store,
            "doc",
            "original-body",
            now=T0,
            etag="etag-old",
            last_modified="Mon, 01 Jan 2026 12:00:00 GMT",
        )
        conn = store._conn
        assert conn is not None
        conn.execute(
            """
            CREATE TRIGGER fail_content_insert
            BEFORE INSERT ON cache_content
            BEGIN
                SELECT RAISE(ABORT, 'injected content write failure');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER fail_content_update
            BEFORE UPDATE ON cache_content
            BEGIN
                SELECT RAISE(ABORT, 'injected content write failure');
            END
            """
        )
        try:
            with pytest.raises(sqlite3.DatabaseError, match="injected content"):
                _put(
                    store,
                    "doc",
                    "replacement-body",
                    now=T1,
                    etag="etag-new",
                    last_modified="Tue, 02 Jan 2026 12:00:00 GMT",
                )
        finally:
            conn.execute("DROP TRIGGER IF EXISTS fail_content_insert")
            conn.execute("DROP TRIGGER IF EXISTS fail_content_update")

        rec = store.get(NS, "doc", now=T1)
        assert rec is not None
        assert rec.content == "original-body"
        assert rec.etag == "etag-old"
        assert rec.last_modified == "Mon, 01 Jan 2026 12:00:00 GMT"
        assert rec.fetched_at == T0
        assert rec.payload_bytes == len(b"original-body")


def test_mark_validated_updates_metadata_not_content(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        original = _put(
            store,
            "doc",
            "unchanged-body",
            now=T0,
            expires_at=T1,
            etag="e0",
            last_modified="old",
        )
        kept = store.mark_validated(NS, "doc", validated_at=T1)
        assert kept.content == "unchanged-body"
        assert kept.content_hash == original.content_hash
        assert kept.fetched_at == T0
        assert kept.validated_at == T1
        assert kept.etag == "e0"
        assert kept.last_modified == "old"
        assert kept.expires_at == T1

        later_expiry = T3 + timedelta(days=1)
        updated = store.mark_validated(
            NS,
            "doc",
            validated_at=T2,
            expires_at=later_expiry,
            etag="e1",
            last_modified="new",
        )
        assert updated.content == "unchanged-body"
        assert updated.content_hash == original.content_hash
        assert updated.fetched_at == T0
        assert updated.validated_at == T2
        assert updated.expires_at == later_expiry
        assert updated.etag == "e1"
        assert updated.last_modified == "new"

        with pytest.raises(KeyError):
            store.mark_validated(NS, "missing", validated_at=T3)


def test_lru_evicts_by_utf8_payload_bytes(tmp_path: Path) -> None:
    # "é"/"ü" are 2 UTF-8 bytes; character counts would be 4+8+4=16 <= 20.
    old = "é" * 4
    mid = "a" * 8
    new = "ü" * 4
    assert len(old) + len(mid) + len(new) <= 20
    assert (
        len(old.encode("utf-8")) + len(mid.encode("utf-8")) + len(new.encode("utf-8"))
        > 20
    )

    with _store(tmp_path, max_bytes=20) as store:
        _put(store, "old", old, now=T0)
        _put(store, "mid", mid, now=T1)
        bumped = store.get(NS, "old", now=T2)
        assert bumped is not None
        written = _put(store, "new", new, now=T3)

        assert written.payload_bytes == len(new.encode("utf-8"))
        assert store.get(NS, "mid", now=T3) is None
        kept_old = store.get(NS, "old", now=T3)
        kept_new = store.get(NS, "new", now=T3)
        assert kept_old is not None and kept_old.content == old
        assert kept_old.payload_bytes == len(old.encode("utf-8"))
        assert kept_new is not None and kept_new.content == new


def test_schema_has_no_query_column(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _put(store, "hashed-key", "body", now=T0)
        db_path = store.path

    for table in ("schema_meta", "cache_entry", "cache_content"):
        names = _columns(db_path, table)
        assert "query" not in names
    assert "body" in _columns(db_path, "cache_content")
    with sqlite3.connect(db_path) as conn:
        sql = " ".join(
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
            )
            if row[0]
        ).lower()
        assert "query" not in sql


def test_clear_removes_all_entries(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        _put(store, "a", "one", now=T0)
        _put(store, "b", "two", now=T1, namespace="other")
        store.clear()
        assert store.get(NS, "a", now=T2) is None
        assert store.get("other", "b", now=T2) is None
        _put(store, "c", "three", now=T2)
        rec = store.get(NS, "c", now=T2)
        assert rec is not None and rec.content == "three"

        conn = store._conn
        assert conn is not None
        assert conn.execute("SELECT COUNT(*) FROM cache_entry").fetchone()[0] == 1
        version = conn.execute(
            "SELECT version FROM schema_meta WHERE id = 1"
        ).fetchone()[0]
        assert version == SCHEMA_VERSION


def test_backup_and_atomic_restore(tmp_path: Path) -> None:
    backup_path = tmp_path / "snapshot.bak"
    with _store(tmp_path) as store:
        _put(store, "doc", "snapshot-body", now=T0, etag="snap")
        written = store.backup(backup_path)
        assert written == backup_path
        assert backup_path.is_file()

        _put(store, "doc", "later-body", now=T1, etag="later")
        _put(store, "extra", "only-after-backup", now=T1)
        store.restore(backup_path)

        restored = store.get(NS, "doc", now=T2)
        assert restored is not None
        assert restored.content == "snapshot-body"
        assert restored.etag == "snap"
        assert store.get(NS, "extra", now=T2) is None
        assert backup_path.is_file()


def test_invalid_backup_preserves_live_database(tmp_path: Path) -> None:
    junk = tmp_path / "not-a-db.bak"
    junk.write_bytes(b"this is not a sqlite database")
    with _store(tmp_path) as store:
        live_path = store.path
        _put(store, "doc", "live-body", now=T0, etag="live")

        with pytest.raises(CacheError) as caught:
            store.restore(junk)
        assert str(caught.value) == (
            f"Yedek geçerli bir SQLite veritabanı değil: {junk}"
        )

        rec = store.get(NS, "doc", now=T1)
        assert rec is not None
        assert rec.content == "live-body"
        assert rec.etag == "live"
        assert live_path.is_file()
        probe = sqlite3.connect(live_path)
        try:
            assert probe.execute("SELECT 1").fetchone()[0] == 1
        finally:
            probe.close()


def test_cache_startup_diagnostics_are_turkish_and_preserve_values(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "newer-schema.sqlite"
    with CacheStore(db_path):
        pass
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE schema_meta SET version = ? WHERE id = 1",
            (SCHEMA_VERSION + 1,),
        )

    with pytest.raises(CacheError) as caught:
        CacheStore(db_path)
    assert str(caught.value) == (
        f"Önbellek şeması {SCHEMA_VERSION + 1}, desteklenen {SCHEMA_VERSION} "
        "sürümünden daha yeni."
    )

    parent_path = tmp_path / "not-a-directory"
    parent_path.write_text("not a directory")
    with pytest.raises(CacheError) as caught:
        CacheStore(parent_path / "cache.sqlite")
    assert str(caught.value) == (
        f"Önbellek dizini güvenli hale getirilemedi: {parent_path}"
    )


def test_cache_wal_diagnostic_is_turkish_and_preserves_wal_value() -> None:
    class _Cursor:
        def fetchone(self) -> tuple[str]:
            return ("delete",)

    class _NoWalConnection:
        def execute(self, statement: str) -> _Cursor | None:
            if statement == "PRAGMA journal_mode = WAL":
                return _Cursor()
            return None

    store = CacheStore.__new__(CacheStore)
    store._busy_timeout_ms = 5000
    with pytest.raises(CacheError) as caught:
        store._configure(_NoWalConnection())  # type: ignore[arg-type]
    assert str(caught.value) == "WAL etkinleştirilemedi; alınan değer: 'delete'"


def test_missing_backup_diagnostic_is_turkish_and_preserves_path(
    tmp_path: Path,
) -> None:
    backup_path = tmp_path / "missing.bak"
    with _store(tmp_path) as store, pytest.raises(FileNotFoundError) as caught:
        store.restore(backup_path)
    assert str(caught.value) == f"Yedek dosyası bulunamadı: {backup_path}"


def test_singleflight_one_factory_call() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def factory() -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "payload"

        first = asyncio.create_task(flight.do("same", factory))
        await started.wait()
        second = asyncio.create_task(flight.do("same", factory))
        release.set()
        assert await asyncio.gather(first, second) == ["payload", "payload"]
        assert calls == 1

    asyncio.run(scenario())


def test_singleflight_exception_cleanup() -> None:
    async def scenario() -> None:
        flight = SingleFlight()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def failing() -> str:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            raise RuntimeError("upstream failed")

        first = asyncio.create_task(flight.do("k", failing))
        await started.wait()
        second = asyncio.create_task(flight.do("k", failing))
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert calls == 1
        assert all(
            isinstance(item, RuntimeError) and "upstream failed" in str(item)
            for item in results
        )

        async def recovered() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await flight.do("k", recovered) == "ok"
        assert calls == 2

    asyncio.run(scenario())


def test_filelock_second_serve_raises_already_running(tmp_path: Path) -> None:
    path = tmp_path / "serve.lock"
    with FileLock(path) as held:
        assert held.locked()
        assert serve_lock_held(path) is True
        with pytest.raises(AlreadyRunning) as caught:
            FileLock(path).acquire()
        assert caught.value.path == path
        assert (
            str(caught.value)
            == "Bu kullanıcı için bir MutalaaMCP serve işlemi zaten çalışıyor."
        )

    assert serve_lock_held(path) is False
    with FileLock(path) as again:
        assert again.locked()


def test_mutation_lock_held_through_operation(tmp_path: Path) -> None:
    path = tmp_path / "serve.lock"
    observed: list[bool] = []

    def operation() -> str:
        observed.append(serve_lock_held(path))
        with pytest.raises(AlreadyRunning):
            FileLock(path).acquire()
        return "done"

    with mutation_lock(path) as lock:
        assert lock.locked()
        assert operation() == "done"
        assert lock.locked()
        observed.append(lock.locked())
    assert observed == [True, True]
    assert serve_lock_held(path) is False

    with (
        pytest.raises(RuntimeError, match="mutation failed"),
        mutation_lock(path) as lock,
    ):
        assert lock.locked()
        try:
            raise RuntimeError("mutation failed")
        finally:
            assert lock.locked()
            with pytest.raises(AlreadyRunning):
                FileLock(path).acquire()
    assert serve_lock_held(path) is False
