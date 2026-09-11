"""Best-effort public announcements, with persistent per-install delivery cooldown."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


class Announcements:
    def __init__(self, client: httpx.AsyncClient, origin: str, data_dir: Path):
        self.client = client
        self.url = origin.rstrip("/") + "/api/mcp/v1/announcement"
        self.path = data_dir / "announcement-delivery.sqlite3"
        self.lock = asyncio.Lock()
        self.checked = 0.0
        self.cached: dict[str, Any] | None = None

    async def take(self) -> dict[str, Any] | None:
        """Reserve delivery, not a view receipt. Never interrupt research on failure."""
        try:
            async with self.lock:
                now = time.time()
                if now - self.checked >= 300:
                    self.checked = now
                    self.cached = None
                    async with self.client.stream(
                        "GET", self.url, timeout=2, follow_redirects=False
                    ) as response:
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 8192:
                                return None
                    import json

                    item = json.loads(body).get("announcement")
                    if item is not None:
                        self.cached = self._validate(item)
                item = self.cached
                if item is None:
                    return None
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(self.path, timeout=0.1) as db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, sent REAL NOT NULL)"
                    )
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT sent FROM deliveries WHERE id = ?", (item["id"],)
                    ).fetchone()
                    if row and now - row[0] < 86400:
                        return None
                    db.execute(
                        "INSERT OR REPLACE INTO deliveries VALUES (?, ?)",
                        (item["id"], now),
                    )
                    db.execute(
                        "DELETE FROM deliveries WHERE sent < ?", (now - 2592000,)
                    )
                return dict(item)
        except Exception:  # noqa: BLE001 -- optional delivery must not fail research
            return None

    @staticmethod
    def _validate(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise TypeError("Invalid announcement")
        for field, limit in [("id", 100), ("title", 120), ("body", 600)]:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise TypeError("Invalid announcement")
        url = item.get("url")
        if url is not None:
            if not isinstance(url, str) or len(url) > 500:
                raise ValueError("Invalid URL")
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Invalid URL")
        return {key: item.get(key) for key in ("id", "title", "body", "url")}
