"""Loopback OAuth bridge for native MCP clients; upstream tokens stay in keyring."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastmcp.server.auth import AccessToken, OAuthProvider
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mutalaamcp.auth import AuthSession, DeviceAuthorization
from mutalaamcp.auth.activation import ActivationError
from mutalaamcp.auth.device_flow import DeviceFlowError

SCOPE = "local_research"
_TTL = 600
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class _Store:
    """Persist registrations and token hashes, never bearer/refresh credentials."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name == "posix":
            path.chmod(0o600)
        # Initialization can precede the ASGI event-loop thread. All operations
        # below are synchronous and run without yielding on that event loop.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS oauth_records "
            "(kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY(kind,key))"
        )

    def put(self, kind: str, key: str, value: dict[str, Any], expires: float) -> None:
        self.db.execute("DELETE FROM oauth_records WHERE expires <= ?", (time.time(),))
        self.db.execute(
            "INSERT OR REPLACE INTO oauth_records VALUES (?,?,?,?)",
            (kind, key, json.dumps(value), expires),
        )
        self.db.commit()

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT value FROM oauth_records WHERE kind=? AND key=? AND expires>?",
            (kind, key, time.time()),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, kind: str, key: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM oauth_records WHERE kind=? AND key=?", (kind, key)
        )
        self.db.commit()
        return cursor.rowcount > 0

    def clear_tokens(self) -> None:
        self.db.execute("DELETE FROM oauth_records WHERE kind != 'client'")
        self.db.commit()

    def count(self) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM oauth_records WHERE expires>?", (time.time(),)
            ).fetchone()[0]
        )


@dataclass
class _Flow:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires: float = field(default_factory=lambda: time.time() + _TTL)
    cookie: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    task: asyncio.Task[None] | None = None
    device: DeviceAuthorization | None = None
    ready: bool = False
    error: str | None = None
    terms_url: str | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _loopback_redirect(value: str) -> bool:
    url = urlsplit(value)
    return (
        url.scheme == "http"
        and url.hostname in {"127.0.0.1", "localhost", "::1"}
        and url.username is None
        and url.password is None
        and not url.fragment
    )


class NativeOAuth(OAuthProvider):
    """Issue local, audience-bound grants after explicit consent and Mütalaa login."""

    def __init__(self, *, port: int, data_dir: Path) -> None:
        self.origin = f"http://127.0.0.1:{port}"
        self.resource = self.origin + "/mcp"
        super().__init__(
            base_url=self.origin,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        self.store = _Store(data_dir / "native-oauth.sqlite3")
        self.session: AuthSession | None = None
        self.flows: dict[str, _Flow] = {}
        self.codes: dict[str, AuthorizationCode] = {}

    async def aclose(self) -> None:
        tasks = [f.task for f in self.flows.values() if f.task and not f.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.db.close()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if (
            client_info.token_endpoint_auth_method != "none"
            or client_info.client_secret
            or not client_info.redirect_uris
            or any(not _loopback_redirect(str(u)) for u in client_info.redirect_uris)
            or self.store.count() >= 1024
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Yerel PKCE istemcisi gerekli."
            )
        assert client_info.client_id is not None
        self.store.put(
            "client",
            client_info.client_id,
            client_info.model_dump(mode="json"),
            time.time() + 30 * 86400,
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        now = time.time()
        for key, flow in list(self.flows.items()):
            if flow.expires <= now:
                if flow.task:
                    flow.task.cancel()
                del self.flows[key]
        self.codes = {k: v for k, v in self.codes.items() if v.expires_at > now}
        if len(self.flows) >= 8:
            raise AuthorizeError(
                "temporarily_unavailable", "Bekleyen bağlantıyı tamamlayın."
            )
        if params.resource not in (None, self.resource) or not _loopback_redirect(
            str(params.redirect_uri)
        ):
            raise AuthorizeError(
                "invalid_request", "Geçersiz yerel kaynak veya dönüş adresi."
            )
        if params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", "local_research izni gerekli.")
        flow_id = secrets.token_urlsafe(32)
        self.flows[flow_id] = _Flow(client, params)
        return self.origin + "/oauth/flow/" + flow_id

    def _authorized_state(self) -> int | None:
        state = self.session.local_state() if self.session else None
        return state.generation if state and state.status == "active" else None

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self.codes.get(_hash(authorization_code))
        if (
            code
            and code.client_id == client.client_id
            and code.expires_at > time.time()
        ):
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        code = await self.load_authorization_code(client, authorization_code.code)
        if code is None or self._authorized_state() is None:
            raise TokenError("invalid_grant", "Bağlantının süresi doldu.")
        del self.codes[_hash(code.code)]
        return self._issue(code.client_id, code.scopes)

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        base = {
            "client_id": client_id,
            "scopes": scopes,
            "generation": self._authorized_state(),
            "resource": self.resource,
        }
        self.store.put(
            "access",
            _hash(access),
            {**base, "refresh_hash": _hash(refresh)},
            now + 3600,
        )
        self.store.put(
            "refresh",
            _hash(refresh),
            {**base, "access_hash": _hash(access)},
            now + 30 * 86400,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=3600,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    def _load_grant(self, kind: str, token: str) -> dict[str, Any] | None:
        data = self.store.get(kind, _hash(token))
        generation = self._authorized_state()
        return (
            data
            if data
            and generation is not None
            and data["generation"] == generation
            and data.get("resource") == self.resource
            else None
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._load_grant("access", token)
        return (
            AccessToken(
                token=token,
                client_id=data["client_id"],
                scopes=data["scopes"],
                resource=self.resource,
            )
            if data
            else None
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._load_grant("refresh", refresh_token)
        if data and data["client_id"] == client.client_id:
            return RefreshToken(
                token=refresh_token, client_id=data["client_id"], scopes=data["scopes"]
            )
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        data = self._load_grant("refresh", refresh_token.token)
        if (
            not data
            or data["client_id"] != client.client_id
            or not set(scopes) <= set(data["scopes"])
        ):
            raise TokenError("invalid_grant", "Bağlantının süresi doldu.")
        if not self.store.delete("refresh", _hash(refresh_token.token)):
            raise TokenError("invalid_grant", "Bağlantının süresi doldu.")
        self.store.delete("access", data["access_hash"])
        return self._issue(data["client_id"], scopes)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        other = "refresh" if kind == "access" else "access"
        data = self.store.get(kind, _hash(token.token))
        if data:
            self.store.delete(kind, _hash(token.token))
            self.store.delete(other, data[other + "_hash"])

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        return super().get_routes(mcp_path) + [
            Route("/oauth/flow/{flow_id}", self.page),
            Route("/oauth/flow/{flow_id}/start", self.start, methods=["POST"]),
            Route("/oauth/flow/{flow_id}/status", self.status),
            Route("/oauth/flow/{flow_id}/complete", self.complete),
        ]

    def _flow(self, request: Request, *, cookie: bool = True) -> _Flow | None:
        flow = self.flows.get(request.path_params["flow_id"])
        if not flow or flow.expires <= time.time():
            return None
        if cookie and not secrets.compare_digest(
            request.cookies.get("mutalaa_flow", ""), flow.cookie
        ):
            return None
        return flow

    async def page(self, request: Request) -> Response:
        flow = self._flow(request, cookie=False)
        if flow is None:
            return HTMLResponse(
                "Bağlantının süresi doldu. Uygulamadan yeniden Kimliği Doğrula seçin.",
                status_code=400,
                headers=_HEADERS,
            )
        nonce = secrets.token_urlsafe(24)
        from mutalaamcp.auth.native_page import render_page

        response = HTMLResponse(
            render_page(
                client_name=html.escape(flow.client.client_name or "MCP istemcisi"),
                redirect_uri=html.escape(str(flow.params.redirect_uri)),
                csrf=flow.csrf,
                nonce=nonce,
            ),
            headers={
                **_HEADERS,
                "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "X-Frame-Options": "DENY",
            },
        )
        response.set_cookie(
            "mutalaa_flow",
            flow.cookie,
            httponly=True,
            samesite="strict",
            path=request.url.path,
            max_age=_TTL,
        )
        return response

    async def start(self, request: Request) -> Response:
        flow = self._flow(request)
        if flow is None or request.headers.get("origin") != self.origin:
            return JSONResponse(
                {"error": "Geçersiz bağlantı isteği."},
                status_code=403,
                headers=_HEADERS,
            )
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), flow.csrf):
            return JSONResponse(
                {"error": "Geçersiz bağlantı isteği."},
                status_code=403,
                headers=_HEADERS,
            )
        if any(
            f is not flow and f.task and not f.task.done() for f in self.flows.values()
        ):
            return JSONResponse(
                {"error": "Önce diğer bağlantı penceresini tamamlayın."},
                status_code=409,
                headers=_HEADERS,
            )
        if flow.task is None:
            flow.task = asyncio.create_task(self._login(flow))
        elif flow.terms_url and flow.task.done() and not flow.error:
            flow.task = asyncio.create_task(self._accept_terms(flow))
        return JSONResponse({"started": True}, headers=_HEADERS)

    async def _login(self, flow: _Flow) -> None:
        async def present(device: DeviceAuthorization) -> None:
            flow.device = device
            flow.expires = min(flow.expires, time.time() + device.expires_in)

        try:
            if self.session is None:
                raise RuntimeError("Çalışma zamanı hazır değil.")
            await self.session.login(present)
            self._finish_login(flow)
        except ActivationError as exc:
            if exc.terms_url:
                flow.terms_url = exc.terms_url
            else:
                flow.error = exc.message
        except DeviceFlowError as exc:
            flow.error = f"Giriş tamamlanamadı ({exc.error}). Uygulamadan yeniden Kimliği Doğrula seçin."
        except Exception:  # noqa: BLE001 -- no credentials or provider bodies in UI/logs
            flow.error = (
                "Giriş tamamlanamadı. Uygulamadan yeniden Kimliği Doğrula seçin."
            )

    async def _accept_terms(self, flow: _Flow) -> None:
        try:
            assert self.session is not None
            await self.session.activate()
            self._finish_login(flow)
        except ActivationError as exc:
            flow.error = exc.message
        except Exception:  # noqa: BLE001 -- secret-free browser boundary
            flow.error = "Koşulların kabulü doğrulanamadı. Yeniden bağlantı kurun."

    def _finish_login(self, flow: _Flow) -> None:
        self.store.clear_tokens()
        self.codes.clear()
        flow.terms_url = None
        flow.ready = True

    async def status(self, request: Request) -> Response:
        flow = self._flow(request)
        if flow is None:
            return JSONResponse(
                {"error": "Bağlantının süresi doldu. Uygulamadan yeniden deneyin."},
                status_code=400,
                headers=_HEADERS,
            )
        return JSONResponse(
            {
                "ready": flow.ready,
                "error": flow.error,
                "terms_url": flow.terms_url,
                "verification_uri": flow.device.verification_uri_complete
                if flow.device
                else None,
                "user_code": flow.device.user_code if flow.device else None,
            },
            headers=_HEADERS,
        )

    async def complete(self, request: Request) -> Response:
        flow = self._flow(request)
        if flow is None or not flow.ready or self._authorized_state() is None:
            return JSONResponse(
                {"error": "Giriş henüz tamamlanmadı."},
                status_code=400,
                headers=_HEADERS,
            )
        code = secrets.token_urlsafe(32)
        self.codes[_hash(code)] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=time.time() + 60,
            client_id=flow.client.client_id or "",
            code_challenge=flow.params.code_challenge,
            redirect_uri=flow.params.redirect_uri,
            redirect_uri_provided_explicitly=flow.params.redirect_uri_provided_explicitly,
            resource=self.resource,
        )
        del self.flows[request.path_params["flow_id"]]
        return RedirectResponse(
            construct_redirect_uri(
                str(flow.params.redirect_uri), code=code, state=flow.params.state
            ),
            status_code=302,
            headers=_HEADERS,
        )
