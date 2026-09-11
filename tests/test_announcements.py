import asyncio

import httpx
import pytest

from mutalaamcp.announcements import Announcements

ITEM = {
    "id": "1",
    "title": "Duyuru",
    "body": "Yeni özellikler hazır.",
    "url": "https://mutalaa.tr/",
}


@pytest.mark.asyncio
async def test_delivery_is_cached_and_persistent_across_instances(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"announcement": ITEM})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = Announcements(client, "https://mutalaa.tr", tmp_path)
        results = await asyncio.gather(service.take(), service.take())
        assert results.count(ITEM) == 1
        assert results.count(None) == 1
        assert len(requests) == 1
        assert (
            await Announcements(client, "https://mutalaa.tr", tmp_path).take() is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"title": "missing"},
        {**ITEM, "url": "javascript:alert(1)"},
        {**ITEM, "body": "x" * 601},
    ],
)
async def test_missing_or_invalid_announcements_are_ignored(tmp_path, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"announcement": payload})
        )
    ) as client:
        assert (
            await Announcements(client, "https://mutalaa.tr", tmp_path).take() is None
        )


@pytest.mark.asyncio
async def test_outage_is_not_retried_on_every_tool_call(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = Announcements(client, "https://mutalaa.tr", tmp_path)
        assert await service.take() is None
        assert await service.take() is None
        assert len(requests) == 1


@pytest.mark.asyncio
async def test_refresh_removes_disabled_notice_and_daily_delivery_recovers(
    tmp_path, monkeypatch
):
    clock = [100000.0]
    monkeypatch.setattr("mutalaamcp.announcements.time.time", lambda: clock[0])
    active = [ITEM]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"announcement": active[0]})
        )
    ) as client:
        service = Announcements(client, "https://mutalaa.tr", tmp_path)
        assert await service.take() == ITEM
        clock[0] += 86401
        assert await service.take() == ITEM
        active[0] = None
        clock[0] += 86401
        assert await service.take() is None
