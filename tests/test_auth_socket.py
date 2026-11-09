"""验证登录会话与NiceGUI事件目标绑定；不创建真实连接或访问平台。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from axiomforge import auth_socket

EVENTS = ("handshake", "event", "javascript_response", "ack", "log")


class FakeClient:
    def __init__(self, client_id):
        self.id = client_id
        self.delete_handlers = []

    def on_delete(self, callback):
        self.delete_handlers.append(callback)


class FakeServer:
    def __init__(self):
        self.originals = {
            name: AsyncMock(return_value="accepted") for name in ("connect", "disconnect", *EVENTS)
        }
        self.handlers = {"/": dict(self.originals)}
        self.environ = {}
        self.disconnect = AsyncMock()

    def on(self, name, handler):
        self.handlers["/"][name] = handler

    def get_environ(self, sid):
        return self.environ.get(sid)


def environ(token, client_id="client-a"):
    return {
        "asgi.scope": {"session": token},
        "QUERY_STRING": f"client_id={client_id}&implicit_handshake=true",
    }


@pytest.fixture
def setup(monkeypatch):
    sessions = {token: SimpleNamespace(token=token) for token in ("session-a", "session-b")}
    store = SimpleNamespace(get=lambda token: sessions.get(token))
    monkeypatch.setattr(auth_socket, "sessions", store)
    monkeypatch.setattr(
        auth_socket, "session_from_scope", lambda scope: sessions.get(scope.get("session"))
    )
    monkeypatch.setattr(auth_socket, "_client_sessions", {})
    monkeypatch.setattr(auth_socket, "_socket_clients", {})
    clients = {name: FakeClient(name) for name in ("client-a", "client-b")}
    monkeypatch.setattr(auth_socket.Client, "instances", clients)
    for name, token in (("client-a", "session-a"), ("client-b", "session-b")):
        auth_socket.bind_client(clients[name], sessions[token])
    server = FakeServer()
    auth_socket.install_socket_guard(server)
    return server, sessions, clients


def test_connect_requires_valid_cookie_and_matching_client_owner(setup):
    async def exercise():
        server, _, _ = setup
        handler = server.handlers["/"]["connect"]
        assert await handler("bad-cookie", environ("forged")) is False
        assert await handler("cross-owner", environ("session-b")) is False
        assert await handler("unknown-client", environ("session-a", "unknown")) is False
        assert await handler("missing-client", {"asgi.scope": {"session": "session-a"}}) is False
        server.originals["connect"].assert_not_called()
        assert await handler("good", environ("session-a")) == "accepted"
        server.originals["connect"].assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("name", EVENTS)
def test_each_event_revalidates_cookie_and_target(setup, name):
    async def exercise():
        server, sessions, _ = setup
        server.environ["good"] = environ("session-a")
        await server.handlers["/"]["connect"]("good", server.environ["good"])
        handler = server.handlers["/"][name]
        payload = {"client_id": "client-a", "listener_id": "test"}
        assert await handler("good", payload) == "accepted"
        server.originals[name].assert_awaited_once_with("good", payload)
        server.originals[name].reset_mock()
        assert await handler("good", {"client_id": "client-b"}) is False
        server.originals[name].assert_not_called()
        sessions.pop("session-a")
        assert await handler("good", payload) is False
        server.originals[name].assert_not_called()
        assert server.disconnect.await_count == 2

    asyncio.run(exercise())


def test_socket_remains_pinned_to_initial_client_even_with_same_owner(setup):
    async def exercise():
        server, sessions, clients = setup
        clients["same-owner"] = FakeClient("same-owner")
        auth_socket.bind_client(clients["same-owner"], sessions["session-a"])
        server.environ["good"] = environ("session-a")
        await server.handlers["/"]["connect"]("good", server.environ["good"])
        assert await server.handlers["/"]["handshake"]("good", {"client_id": "same-owner"}) is False
        server.originals["handshake"].assert_not_called()
        server.disconnect.assert_awaited_once_with("good")

    asyncio.run(exercise())


def test_malformed_event_cannot_reach_framework(setup):
    async def exercise():
        server, _, _ = setup
        server.environ["good"] = environ("session-a")
        await server.handlers["/"]["connect"]("good", server.environ["good"])
        for value in (None, [], "client-a", {"client_id": []}, {}):
            assert await server.handlers["/"]["event"]("good", value) is False
        server.originals["event"].assert_not_called()

    asyncio.run(exercise())


def test_client_deletion_removes_authority_and_cannot_bind_expired_session(setup):
    server, sessions, clients = setup
    for callback in clients["client-a"].delete_handlers:
        callback()
    assert "client-a" not in auth_socket._client_sessions
    with pytest.raises(ValueError, match="其他登录会话"):
        auth_socket.bind_client(clients["client-b"], sessions["session-a"])
    old_session = sessions.pop("session-a")
    with pytest.raises(ValueError, match="失效"):
        auth_socket.bind_client(clients["client-a"], old_session)
    server.originals["event"].assert_not_called()


def test_install_is_idempotent_and_preserves_disconnect_cleanup(setup):
    async def exercise():
        server, _, _ = setup
        handlers = dict(server.handlers["/"])
        auth_socket.install_socket_guard(server)
        assert server.handlers["/"] == handlers
        server.environ["good"] = environ("session-a")
        await server.handlers["/"]["connect"]("good", server.environ["good"])
        await server.handlers["/"]["disconnect"]("good")
        assert "good" not in auth_socket._socket_clients
        server.originals["disconnect"].assert_awaited_once_with("good")

    asyncio.run(exercise())


def test_synchronous_framework_event_handler_is_preserved(setup):
    async def exercise():
        server, _, _ = setup
        # 新服务上的原同步处理器也必须在校验后原样调用。
        fresh = FakeServer()
        original = Mock(return_value="sync accepted")
        fresh.handlers["/"]["event"] = original
        auth_socket.install_socket_guard(fresh)
        fresh.environ["sync"] = environ("session-a")
        await fresh.handlers["/"]["connect"]("sync", fresh.environ["sync"])
        assert await fresh.handlers["/"]["event"]("sync", {"client_id": "client-a"}) == (
            "sync accepted"
        )
        original.assert_called_once_with("sync", {"client_id": "client-a"})
        server.originals["event"].assert_not_called()

    asyncio.run(exercise())


def test_missing_framework_handler_fails_closed_before_registration():
    server = FakeServer()
    del server.handlers["/"]["event"]
    original_connect = server.handlers["/"]["connect"]
    with pytest.raises(RuntimeError, match="事件合同不兼容"):
        auth_socket.install_socket_guard(server)
    assert server.handlers["/"]["connect"] is original_connect
