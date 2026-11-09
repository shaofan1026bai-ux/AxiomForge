"""验证轮询传输会话归属及注销期间的响应，不依赖现场认证。"""

import asyncio

import pytest

from axiomforge import auth_socket
from axiomforge.auth import SESSION_COOKIE, AuthMiddleware, SessionStore


def scope_for(token, query=b"", kind="http"):
    return {
        "type": kind,
        "asgi": {"version": "3.0"},
        "method": "GET",
        "scheme": "http" if kind == "http" else "ws",
        "path": "/_nicegui_ws/socket.io/",
        "raw_path": b"/_nicegui_ws/socket.io/",
        "query_string": query,
        "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


def test_transport_sid_cannot_be_reused_by_other_cookie(monkeypatch):
    store = SessionStore()
    first, second = store.create("first"), store.create("second")
    monkeypatch.setattr(
        auth_socket.core.sio, "environ", {"copied-sid": {"asgi.scope": scope_for(first.token)}}
    )
    assert auth_socket.transport_session_matches(scope_for(first.token, b"sid=copied-sid"), first)
    assert not auth_socket.transport_session_matches(
        scope_for(second.token, b"sid=copied-sid"), second
    )
    assert not auth_socket.transport_session_matches(scope_for(first.token, b"sid=unknown"), first)
    assert not auth_socket.transport_session_matches(scope_for(first.token, b"sid="), first)
    assert not auth_socket.transport_session_matches(scope_for(first.token, b"sid=a&sid=b"), first)
    assert auth_socket.transport_session_matches(scope_for(first.token), first)


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_middleware_rejects_other_sessions_polling_or_upgrade(monkeypatch, kind):
    async def exercise():
        store = SessionStore()
        first, second = store.create("first"), store.create("second")
        monkeypatch.setattr(
            auth_socket.core.sio, "environ", {"copied-sid": {"asgi.scope": scope_for(first.token)}}
        )
        called, messages = [], []

        async def downstream(*_args):
            called.append(True)

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        await AuthMiddleware(downstream, store)(
            scope_for(second.token, b"sid=copied-sid", kind), receive, send
        )
        assert not called
        if kind == "http":
            assert messages[0]["status"] == 403
        else:
            assert messages == [{"type": "websocket.close", "code": 1008}]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "revoke_at", ["before_start", "before_body", "during_start", "after_first_chunk"]
)
def test_long_poll_and_stream_cannot_deliver_body_after_revocation(revoke_at):
    async def exercise():
        store = SessionStore()
        session = store.create("user")
        messages = []

        async def downstream(scope, receive, send):
            if revoke_at == "before_start":
                store.revoke(session.token)
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"11")],
                }
            )
            if revoke_at == "before_body":
                store.revoke(session.token)
            if revoke_at == "after_first_chunk":
                await send({"type": "http.response.body", "body": b"safe", "more_body": True})
                store.revoke(session.token)
            await send({"type": "http.response.body", "body": b"secret-data", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)
            if revoke_at == "during_start" and message["type"] == "http.response.start":
                store.revoke(session.token)

        await AuthMiddleware(downstream, store)(scope_for(session.token), receive, send)
        assert b"secret-data" not in b"".join(m.get("body", b"") for m in messages)
        assert messages[-1]["type"] == "http.response.body"
        assert messages[-1].get("more_body") is False
        if revoke_at in {"before_start", "before_body"}:
            assert messages[0]["status"] == 401
        else:
            assert (b"content-length", b"11") not in messages[0]["headers"]

    asyncio.run(exercise())


def test_malformed_origin_fails_closed_without_server_error():
    from fastapi import Request

    from axiomforge.auth import _same_origin

    scope = scope_for("unused")
    scope["headers"].append((b"origin", b"http://[malformed"))
    assert not _same_origin(Request(scope))


def test_non_ascii_csrf_is_rejected_as_invalid_form(monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from axiomforge.auth import install_auth

    store = SessionStore()
    app = FastAPI()
    install_auth(app, store)
    called = []
    monkeypatch.setattr("axiomforge.auth.authenticate_account", lambda *_: called.append(True))
    with TestClient(app) as client:
        client.get("/login")
        response = client.post(
            "/login",
            data={"csrf": "无效校验", "account": "user", "password": "private"},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 403
        assert not called
        session = store.create("user")
        client.cookies.set(SESSION_COOKIE, session.token)
        response = client.post(
            "/logout", data={"csrf": "无效校验"}, headers={"Origin": "http://testserver"}
        )
        assert response.status_code == 403
        assert store.get(session.token) is not None
