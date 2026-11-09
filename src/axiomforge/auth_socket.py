"""将NiceGUI客户端与登录会话绑定，逐事件拒绝跨会话或已撤销的Socket。"""

import inspect
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from nicegui import Client, core
from starlette.types import Scope

from axiomforge.auth import Session, cookie_from_scope, session_from_scope, sessions

# 此表只含服务器创建的绑定；浏览器提交的client_id不是授权凭据。
_client_sessions: dict[str, str] = {}
_socket_clients: dict[str, str] = {}


def bind_client(client: Client, session: Session) -> None:
    """页面创建时绑定；删除页面即移除索引，重新登录不能复用旧客户端。"""
    current = sessions.get(session.token)
    if current is None:
        raise ValueError("登录会话已失效。")
    if client.id in _client_sessions and _client_sessions[client.id] != session.token:
        raise ValueError("客户端已属于其他登录会话。")
    _client_sessions[client.id] = session.token
    client.on_delete(lambda: _client_sessions.pop(client.id, None))


def _session(environ: object) -> Session | None:
    if not isinstance(environ, dict):
        return None
    scope = environ.get("asgi.scope")
    return session_from_scope(scope) if isinstance(scope, dict) else None


def _owns(session: Session | None, client_id: object) -> bool:
    return (
        session is not None
        and isinstance(client_id, str)
        and bool(client_id)
        and _client_sessions.get(client_id) == session.token
        and client_id in Client.instances
    )


def transport_session_matches(scope: Scope, session: Session | None) -> bool:
    """轮询和升级请求必须保留最初连接的会话，不能仅凭复制的传输sid取队列。"""
    if session is None:
        return False
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
    values = query.get("sid")
    if values is None:
        return True  # 初次连接还没有传输sid，后续connect校验client归属。
    if len(values) != 1 or not values[0]:
        return False
    environ = core.sio.environ.get(values[0])
    if not isinstance(environ, dict) or not isinstance(environ.get("asgi.scope"), dict):
        return False
    return cookie_from_scope(environ["asgi.scope"]) == session.token


def _query_client(environ: dict[str, Any]) -> str | None:
    query = parse_qs(environ.get("QUERY_STRING", ""))
    values = query.get("client_id", ())
    return values[0] if len(values) == 1 else None


async def _invoke(handler: Callable[..., Any], *args: Any) -> Any:
    result = handler(*args)
    return await result if inspect.isawaitable(result) else result


def install_socket_guard(server: Any = None) -> None:
    """包装框架原处理器而不复制其协议；多次安装不叠加包装。"""
    server = core.sio if server is None else server
    handlers = server.handlers.get("/", {})
    connect = handlers.get("connect")
    if getattr(connect, "_axiomforge_auth_guard", False) is True:
        return
    required = ("connect", "disconnect", "handshake", "event", "javascript_response", "ack", "log")
    if any(not callable(handlers.get(name)) for name in required):
        # 框架升级若改变事件合同应在启动时暴露，不能静默缺少目标归属校验。
        raise RuntimeError("NiceGUI事件合同不兼容，无法启用登录保护。")

    async def guarded_connect(sid: str, environ: dict[str, Any], *args: Any) -> Any:
        client_id = _query_client(environ)
        if client_id is None or not _owns(_session(environ), client_id):
            return False
        _socket_clients[sid] = client_id
        try:
            result = await _invoke(connect, sid, environ, *args)
        except Exception:
            _socket_clients.pop(sid, None)
            raise
        if result is False:
            _socket_clients.pop(sid, None)
        return result

    guarded_connect.__dict__["_axiomforge_auth_guard"] = True
    server.on("connect", guarded_connect)

    def protect(handler: Callable[..., Any]) -> Callable[..., Any]:
        async def guarded(sid: str, message: object, *args: Any) -> Any:
            client_id = message.get("client_id") if isinstance(message, dict) else None
            session = _session(server.get_environ(sid))
            if not _owns(session, client_id) or _socket_clients.get(sid) != client_id:
                await server.disconnect(sid)
                return False
            return await _invoke(handler, sid, message, *args)

        return guarded

    for name in ("handshake", "event", "javascript_response", "ack", "log"):
        handler = handlers.get(name)
        if handler is not None:
            server.on(name, protect(handler))

    disconnect = handlers.get("disconnect")
    if disconnect is not None:

        async def guarded_disconnect(sid: str, *args: Any) -> Any:
            _socket_clients.pop(sid, None)
            return await _invoke(disconnect, sid, *args)

        server.on("disconnect", guarded_disconnect)
