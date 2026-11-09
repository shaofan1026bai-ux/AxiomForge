"""小麦账号认证与工作台会话边界；密码和上游令牌不进入持久状态。"""

import asyncio
import html
import os
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from axiomforge.xiaomai_client import (
    PUBLIC_KEY,
    Settings,
    XiaomaiClient,
    XiaomaiSourceError,
    http_client,
)

SESSION_COOKIE = COOKIE_NAME = "AXIOMFORGE_SESSION"
CSRF_COOKIE = "AXIOMFORGE_LOGIN_CSRF"
SESSION_SECONDS = 8 * 60 * 60
MAX_BODY_BYTES = 8192


@dataclass(frozen=True, slots=True)
class Session:
    token: str = field(repr=False)
    account: str
    expires_at: float
    csrf: str = field(repr=False)


class SessionStore:
    """不透明会话仅存在当前进程，固定过期，不因读取续期。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._sessions: dict[str, Session] = {}
        self._callbacks: dict[str, dict[object, Callable[[], None]]] = {}
        self._lock = threading.RLock()

    def create(self, account: str) -> Session:
        # 清理到期状态并设置上限，避免长期运行时保留无效浏览器引用。
        for token in list(self._sessions):
            self.get(token)
        if len(self._sessions) >= 1024:
            self.revoke(next(iter(self._sessions)))
        session = Session(
            secrets.token_urlsafe(32),
            account,
            self.clock() + SESSION_SECONDS,
            secrets.token_urlsafe(32),
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not isinstance(token, str):
            return None
        with self._lock:
            session = self._sessions.get(token)
        if session and session.expires_at <= self.clock():
            self.revoke(token)
            return None
        return session

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)
            callbacks = self._callbacks.pop(token, {})
        for callback in callbacks.values():
            try:
                callback()
            except Exception:
                # 即使某个断线客户端清理失败，也必须完成其余会话撤销。
                continue

    def on_revoke(self, token: str, callback: Callable[[], None]) -> Callable[[], None]:
        key = object()
        with self._lock:
            if token not in self._sessions:
                callback()
                return lambda: None
            self._callbacks.setdefault(token, {})[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._callbacks.get(token)
                if callbacks is not None:
                    callbacks.pop(key, None)
                    if not callbacks:
                        self._callbacks.pop(token, None)

        return unsubscribe


sessions = SessionStore()


def cookie_from_scope(scope: Scope, name: str = SESSION_COOKIE) -> str | None:
    """只解析受控Cookie名，畸形cookie不视为已登录。"""
    try:
        cookies = SimpleCookie()
        for key, value in scope.get("headers", ()):
            if key.lower() == b"cookie":
                cookies.load(value.decode("latin-1"))
        item = cookies.get(name)
        return item.value if item else None
    except Exception:
        return None


def session_from_scope(scope: Scope, store: SessionStore = sessions) -> Session | None:
    return store.get(cookie_from_scope(scope))


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        # 原生表单在部分客户端省略Origin，此时必须有同源Referer。
        origin = request.headers.get("referer", "")
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    scheme = {"ws": "http", "wss": "https"}.get(request.url.scheme, request.url.scheme)
    return parsed.scheme == scheme and parsed.netloc == request.url.netloc


class AuthMiddleware:
    """所有业务HTTP和Socket均需有效会话，不整体豁免NiceGUI内部路径。"""

    def __init__(self, app: ASGIApp, store: SessionStore = sessions) -> None:
        self.app, self.store = app, store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        public = path in {"/login", "/favicon.ico"} and scope["type"] == "http"
        session = session_from_scope(scope, self.store)
        if not public and session is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            elif scope.get("method") == "GET" and not path.startswith("/_nicegui"):
                await RedirectResponse(
                    "/login", status_code=303, headers={"Cache-Control": "no-store"}
                )(scope, receive, send)
            else:
                await Response(
                    "请先登录。", status_code=401, headers={"Cache-Control": "no-store"}
                )(scope, receive, send)
            return
        scope.setdefault("state", {})["auth_session"] = session
        if path.startswith("/_nicegui_ws"):
            # Engine.IO轮询会复用最初的environ，必须核对本次HTTP／升级请求的Cookie。
            from axiomforge.auth_socket import transport_session_matches

            if not transport_session_matches(scope, session):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await Response(
                        "连接会话无效。", status_code=403, headers={"Cache-Control": "no-store"}
                    )(scope, receive, send)
                return
        if scope["type"] == "http" and path.startswith("/_nicegui_ws"):
            request = Request(scope)
            if request.headers.get("origin") and not _same_origin(request):
                await Response("连接来源无效。", status_code=403)(scope, receive, send)
                return
        if scope["type"] == "websocket":
            request = Request({**scope, "type": "http"})
            if request.headers.get("origin") and not _same_origin(request):
                await send({"type": "websocket.close", "code": 1008})
                return
            token = session.token if session else ""
            closed = False

            async def close() -> None:
                nonlocal closed
                if not closed:
                    closed = True
                    await send({"type": "websocket.close", "code": 1008})

            async def guarded_receive() -> Message:
                message = await receive()
                if self.store.get(token) is None and message["type"] != "websocket.disconnect":
                    await close()
                    return {"type": "websocket.disconnect", "code": 1008}
                return message

            async def guarded_send(message: Message) -> None:
                if self.store.get(token) is None:
                    await close()
                elif not closed:
                    await send(message)

            await self.app(scope, guarded_receive, guarded_send)
            return

        protect_response = not public and path != "/logout"
        pending_start: Message | None = None
        response_finished = False

        async def no_cache_send(message: Message) -> None:
            nonlocal pending_start, response_finished
            if response_finished:
                return
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", ())
                    if k.lower() not in {b"cache-control", b"pragma"}
                ]
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"pragma", b"no-cache"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"same-origin"),
                    ]
                )
                # 先等待第一块响应体，避免长轮询在注销后才返回排队数据。
                pending_start = {**message, "headers": headers}
                return
            valid = not protect_response or (
                session is not None and self.store.get(session.token) is not None
            )
            if not valid:
                if pending_start is not None:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 401,
                            "headers": [(b"content-length", b"0"), (b"cache-control", b"no-store")],
                        }
                    )
                    pending_start = None
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                response_finished = True
                return
            if pending_start is not None:
                # 注销可发生在头部与正文发送之间，不承诺尚未发出的固定长度。
                if protect_response:
                    pending_start["headers"] = [
                        (k, v)
                        for k, v in pending_start.get("headers", ())
                        if k.lower() != b"content-length"
                    ]
                await send(pending_start)
                pending_start = None
            if protect_response and session is not None and self.store.get(session.token) is None:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                response_finished = True
                return
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True

        await self.app(scope, receive, no_cache_send)


class LoginError(ValueError):
    """允许向登录表单展示的中文文案。"""


def authenticate_account(
    account: str,
    password: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """只验证本次输入和项目授权，不使用服务端账号代替用户身份。"""
    if not account or len(account) > 128 or not password or len(password) > 512:
        raise LoginError("请输入有效的小麦账号和密码。")
    path = os.environ.get("AXIOMFORGE_XIAOMAI_ENV_FILE", "")
    try:
        if path and not Path(path).expanduser().is_file():
            raise LoginError("登录配置暂不可用，请联系服务管理员。")
        values = dotenv_values(Path(path).expanduser(), interpolate=False) if path else {}
        key = os.environ.get("XIAOMAI_PUBLIC_KEY") or values.get("XIAOMAI_PUBLIC_KEY") or PUBLIC_KEY
        with http_client(transport) as client:
            source = XiaomaiClient(client, time.monotonic() + 30, 30)
            source.authenticate(Settings(account, password, key.replace("\\n", "\n")), 832)
            source.token = ""
    except LoginError:
        raise
    except httpx.TimeoutException:
        raise LoginError("小麦登录请求超时，请稍后重试。") from None
    except httpx.HTTPError:
        raise LoginError("暂时无法连接小麦平台，请检查网络后重试。") from None
    except XiaomaiSourceError as error:
        if "项目权限" in str(error):
            raise LoginError("该小麦账号没有芒市机场项目权限，无法进入工作台。") from None
        raise LoginError("小麦账号验证未通过，请检查账号、密码及平台状态。") from None
    except Exception:
        raise LoginError("小麦账号验证暂不可用，请稍后重试。") from None


class AttemptLimiter:
    """按来源限制一分钟内的登录尝试，登录成功也不重置频率上限。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.attempts: dict[str, deque[float]] = {}

    def allow(self, address: str) -> bool:
        now = self.clock()
        self.attempts = {k: v for k, v in self.attempts.items() if v and v[-1] > now - 60}
        if address not in self.attempts and len(self.attempts) >= 256:
            return False
        values = self.attempts.setdefault(address, deque())
        while values and values[0] <= now - 60:
            values.popleft()
        if len(values) >= 10:
            return False
        values.append(now)
        return True


def _set_cookie(response: Response, request: Request, name: str, value: str, age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=age,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


def login_response(
    request: Request, error: str = "", account: str = "", status: int = 200
) -> Response:
    token = secrets.token_urlsafe(32)
    template = Path(__file__).with_name("ui").joinpath("login.html").read_text()
    content = template.replace("{{csrf}}", token).replace(
        "{{account}}", html.escape(account, quote=True)
    )
    content = content.replace("{{error}}", html.escape(error)).replace(
        "{{error_hidden}}",
        "" if error else "hidden",
    )
    response = HTMLResponse(content, status_code=status, headers={"Cache-Control": "no-store"})
    _set_cookie(response, request, CSRF_COOKIE, token, 600)
    return response


async def _form(request: Request) -> dict[str, str]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise LoginError("登录表单格式无效，请刷新后重试。")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BODY_BYTES:
            raise LoginError("登录表单过大，请刷新后重试。")
    try:
        parsed = parse_qs(content.decode("utf-8"), keep_blank_values=True, max_num_fields=6)
    except (UnicodeError, ValueError):
        raise LoginError("登录表单格式无效，请刷新后重试。") from None
    if any(len(values) != 1 for values in parsed.values()):
        raise LoginError("登录表单格式无效，请刷新后重试。")
    return {key: values[0] for key, values in parsed.items()}


def install_auth(app: FastAPI, store: SessionStore = sessions) -> None:
    """在ui.run前安装一次；登录不创建匿名NiceGUI客户端。"""
    limiter = AttemptLimiter()
    slots = asyncio.Semaphore(2)
    app.add_middleware(AuthMiddleware, store=store)

    @app.get("/login", response_model=None)
    async def login_page(request: Request) -> Response:
        if session_from_scope(request.scope, store):
            return RedirectResponse("/", status_code=303)
        return login_response(request)

    @app.post("/login", response_model=None)
    async def login(request: Request) -> Response:
        try:
            form = await _form(request)
            csrf = form.get("csrf", "")
            cookie = request.cookies.get(CSRF_COOKIE, "")
            if (
                not _same_origin(request)
                or not csrf
                or not secrets.compare_digest(csrf.encode("utf-8"), cookie.encode("utf-8"))
            ):
                return login_response(request, "登录页面已失效，请重新提交。", status=403)
            account = form.get("account", "").strip()
            password = form.get("password", "")
            if not limiter.allow(request.client.host if request.client else "unknown"):
                return login_response(request, "登录尝试过于频繁，请一分钟后重试。", account, 429)
            if slots.locked():
                return login_response(request, "当前登录请求较多，请稍后重试。", account, 429)
            async with slots:
                await run_in_threadpool(authenticate_account, account, password)
            # 认证完成后旋转随机会话，旧浏览器页不能继续复用旧身份。
            old = request.cookies.get(SESSION_COOKIE)
            if old:
                store.revoke(old)
            session = store.create(account)
            response = RedirectResponse("/", status_code=303)
            _set_cookie(response, request, SESSION_COOKIE, session.token, SESSION_SECONDS)
            response.delete_cookie(CSRF_COOKIE, path="/")
            return response
        except LoginError as error:
            return login_response(request, str(error), status=400)

    @app.post("/logout", response_model=None)
    async def logout(request: Request) -> Response:
        session = session_from_scope(request.scope, store)
        if session is None:
            return RedirectResponse("/login", status_code=303)
        try:
            form = await _form(request)
        except LoginError:
            return Response("退出表单无效。", status_code=403)
        if not _same_origin(request) or not secrets.compare_digest(
            form.get("csrf", "").encode("utf-8"), session.csrf.encode("utf-8")
        ):
            return Response("退出验证失败，请刷新后重试。", status_code=403)
        store.revoke(session.token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response
