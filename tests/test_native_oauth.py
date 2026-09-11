"""Native OAuth exercised over HTTP, including consent, PKCE and token boundaries."""

from __future__ import annotations

import base64
import hashlib
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from mutalaamcp.auth.device_flow import DeviceAuthorization
from mutalaamcp.auth.native import NativeOAuth
from mutalaamcp.native_service import LoopbackGuard

ORIGIN = "http://127.0.0.1:8769"
REDIRECT = "http://127.0.0.1:45678/callback"
VERIFIER = "a" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)


class Session:
    def __init__(self):
        self.state = None
        self.logins = 0

    def local_state(self):
        return self.state

    async def login(self, present):
        self.logins += 1
        await present(
            DeviceAuthorization(
                device_code="private-device-code",
                user_code="ABCD-EFGH",
                verification_uri="https://mutalaa.tr/mcp/activate",
                verification_uri_complete="https://mutalaa.tr/mcp/activate?user_code=ABCD-EFGH",
                expires_in=300,
                interval=5,
            )
        )
        self.state = SimpleNamespace(status="active", generation=1)


@pytest.fixture
def native(tmp_path):
    provider = NativeOAuth(port=8769, data_dir=tmp_path)
    session = Session()
    provider.session = session
    server = FastMCP("OAuth test", auth=provider)
    app = server.http_app(middleware=[Middleware(LoopbackGuard, port=8769)])
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, provider, session
    provider.store.db.close()


def register(client, redirect=REDIRECT):
    return client.post(
        "/register",
        json={
            "redirect_uris": [redirect],
            "client_name": "Codex",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


def authorize(client, client_id, **extra):
    return client.get(
        "/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "scope": "local_research",
            "state": "client-state",
            "resource": ORIGIN + "/mcp",
            **extra,
        },
        follow_redirects=False,
    )


def consent(client, client_id):
    response = authorize(client, client_id)
    assert response.status_code == 302
    flow = urlsplit(response.headers["location"]).path
    page = client.get(flow)
    csrf = re.search(r"csrf:'([^']+)'", page.text).group(1)
    assert "frame-ancestors" in page.headers["content-security-policy"]
    assert (
        client.post(
            flow + "/start", data={"csrf": csrf}, headers={"Origin": ORIGIN}
        ).status_code
        == 200
    )
    status = client.get(flow + "/status")
    assert status.json()["ready"] is True
    assert "private-device-code" not in status.text
    complete = client.get(flow + "/complete", follow_redirects=False)
    query = parse_qs(urlsplit(complete.headers["location"]).query)
    assert query["state"] == ["client-state"]
    return query["code"][0]


def exchange(client, client_id, code, **extra):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": VERIFIER,
            "resource": ORIGIN + "/mcp",
            **extra,
        },
    )


def test_discovery_and_unauthenticated_http_challenge(native):
    client, _, session = native
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["authorization_endpoint"] == ORIGIN + "/authorize"
    assert metadata["registration_endpoint"] == ORIGIN + "/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    response = client.post("/mcp", json={})
    assert response.status_code == 401
    assert "oauth-protected-resource" in response.headers["www-authenticate"]
    assert session.logins == 0


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/callback",
        "http://127.0.0.1.evil.example/callback",
        "file:///tmp/callback",
        "http://127.0.0.1:80/callback#fragment",
    ],
)
def test_registration_rejects_non_loopback_redirects(native, redirect):
    client, _, _ = native
    assert register(client, redirect).status_code == 400


def test_host_origin_and_consent_are_required(native):
    client, _, session = native
    assert (
        client.get(
            "/.well-known/oauth-authorization-server", headers={"Host": "evil.example"}
        ).status_code
        == 403
    )
    assert register(client).status_code == 201
    client_id = register(client).json()["client_id"]
    flow = urlsplit(authorize(client, client_id).headers["location"]).path
    page = client.get(flow)
    csrf = re.search(r"csrf:'([^']+)'", page.text).group(1)
    assert session.logins == 0
    assert client.get(flow + "/complete").status_code == 400
    assert (
        client.post(
            flow + "/start",
            data={"csrf": csrf},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            flow + "/start", data={"csrf": "wrong"}, headers={"Origin": ORIGIN}
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert (
        client.post(
            flow + "/start", data={"csrf": csrf}, headers={"Origin": ORIGIN}
        ).status_code
        == 403
    )
    assert session.logins == 0


def test_pkce_code_replay_token_hashes_rotation_and_logout(native, tmp_path):
    client, _provider, session = native
    client_id = register(client).json()["client_id"]
    code = consent(client, client_id)
    assert (
        exchange(client, client_id, code, code_verifier="b" * 64).json()["error"]
        == "invalid_grant"
    )
    response = exchange(client, client_id, code)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert exchange(client, client_id, code).json()["error"] == "invalid_grant"
    assert (
        tokens["access_token"].encode()
        not in (tmp_path / "native-oauth.sqlite3").read_bytes()
    )
    assert (
        tokens["refresh_token"].encode()
        not in (tmp_path / "native-oauth.sqlite3").read_bytes()
    )
    # Auth passes: malformed MCP request now reaches the protocol (not HTTP 401).
    assert (
        client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer " + tokens["access_token"]},
        ).status_code
        != 401
    )
    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    assert (
        client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer " + tokens["access_token"]},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        ).json()["error"]
        == "invalid_grant"
    )
    session.state = SimpleNamespace(status="logged_out", generation=2)
    assert (
        client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer " + refreshed.json()["access_token"]},
        ).status_code
        == 401
    )


def test_wrong_resource_redirect_and_cross_client_exchange(native):
    client, _, _ = native
    client_id = register(client).json()["client_id"]
    bad = authorize(client, client_id, resource="https://evil.example/mcp")
    assert "invalid_request" in bad.headers["location"]
    assert (
        authorize(
            client, client_id, redirect_uri="http://127.0.0.1:9999/other"
        ).status_code
        == 400
    )
    code = consent(client, client_id)
    other = register(client).json()["client_id"]
    assert exchange(client, other, code).json()["error"] == "invalid_grant"
    assert exchange(client, client_id, code).status_code == 200
