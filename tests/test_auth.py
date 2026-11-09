"""认证边界只使用假上游；不读取真实凭据，不访问现场。"""

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient

from axiomforge.auth import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_SECONDS,
    AttemptLimiter,
    AuthMiddleware,
    LoginError,
    SessionStore,
    authenticate_account,
    install_auth,
)
from axiomforge.xiaomai_client import LOGIN_PATH, PROJECT_PATH


@pytest.fixture(autouse=True)
def no_real_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AXIOMFORGE_XIAOMAI_ENV_FILE", "XIAOMAI_PUBLIC_KEY"):
        monkeypatch.delenv(key, raising=False)


def app_client(
    monkeypatch: pytest.MonkeyPatch,
    validator: Callable[..., None] | None = None,
) -> tuple[TestClient, SessionStore]:
    store = SessionStore()
    app = FastAPI()
    install_auth(app, store)
    monkeypatch.setattr("axiomforge.auth.authenticate_account", validator or (lambda *_: None))

    @app.get("/")
    async def protected(request: Request) -> PlainTextResponse:
        return PlainTextResponse("已登录：" + request.state.auth_session.account)

    @app.get("/_nicegui/client/download")
    async def download() -> PlainTextResponse:
        return PlainTextResponse("private-report")

    @app.get("/_nicegui_ws/socket.io")
    async def polling() -> PlainTextResponse:
        return PlainTextResponse("polling")

    return TestClient(app, base_url="http://127.0.0.1:8081"), store


def login_form(client: TestClient) -> dict[str, str]:
    response = client.get("/login")
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match
    return {"account": "operator", "password": "private-password", "csrf": match.group(1)}


def login(client: TestClient) -> httpx.Response:
    return client.post(
        "/login",
        data=login_form(client),
        headers={"Origin": "http://127.0.0.1:8081"},
        follow_redirects=False,
    )


def test_session_is_opaque_fixed_expiry_and_restart_has_no_session() -> None:
    clock = [10.0]
    store = SessionStore(clock=lambda: clock[0])
    session = store.create("operator")
    assert len(session.token) >= 40 and session.csrf != session.token
    assert "operator" not in session.token
    assert session.token not in repr(session) and session.csrf not in repr(session)
    assert SessionStore().get(session.token) is None
    clock[0] += SESSION_SECONDS - 1
    assert store.get(session.token) is session
    clock[0] += 1
    assert store.get(session.token) is None


def test_revoke_runs_cleanup_once_and_supports_unsubscribe() -> None:
    store = SessionStore()
    session = store.create("operator")
    seen = []
    unsubscribe = store.on_revoke(session.token, lambda: seen.append("removed"))
    unsubscribe()
    store.on_revoke(session.token, lambda: seen.append("cancelled"))
    store.revoke(session.token)
    store.revoke(session.token)
    assert seen == ["cancelled"]


@pytest.mark.parametrize("cookie", [None, "made-up", "<invalid>"])
def test_unauthed_pages_and_downloads_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
    cookie: str | None,
) -> None:
    client, _ = app_client(monkeypatch)
    if cookie:
        client.cookies.set(SESSION_COOKIE, cookie)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert response.headers["cache-control"] == "no-store"
    download = client.get("/_nicegui/client/download")
    assert download.status_code == 401 and "private-report" not in download.text
    assert download.headers["cache-control"] == "no-store"


def test_login_uses_typed_credentials_sets_http_only_cookie_and_rotates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []
    client, store = app_client(monkeypatch, lambda *values: seen.append(values))
    form = login_form(client)
    form["password"] = " password with spaces "
    response = client.post(
        "/login", data=form, headers={"Origin": "http://127.0.0.1:8081"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert seen == [("operator", " password with spaces ")]
    cookie = response.headers.get_list("set-cookie")[0]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Max-Age=28800" in cookie
    first = client.cookies.get(SESSION_COOKIE)
    session = store.get(first)
    assert session and session.account == "operator"
    assert session.token not in response.text and "password" not in response.text
    protected = client.get("/")
    assert protected.text == "已登录：operator"
    assert protected.headers["cache-control"] == "no-store"
    # 再认证必须旋转服务端会话，已复制的旧cookie不能继续使用。
    client.cookies.set(CSRF_COOKIE, "second-form")
    response = client.post(
        "/login",
        data={**form, "csrf": "second-form"},
        headers={"Origin": "http://127.0.0.1:8081"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and client.cookies.get(SESSION_COOKIE) != first
    assert store.get(first) is None


def test_login_form_never_contains_password_and_escapes_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from axiomforge.auth import login_response

    scope: Any = {
        "type": "http",
        "scheme": "http",
        "path": "/login",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8081")],
    }
    response = login_response(Request(scope), "<bad>", '"><script>bad</script>')
    body = bytes(response.body).decode()
    assert "&lt;bad&gt;" in body and "<script>bad</script>" not in body
    assert 'name="password" type="password"' in body
    assert 'name="password" value=' not in body
    assert "账号由小麦平台验证" in body


@pytest.mark.parametrize(
    "origin,csrf",
    [
        ("http://127.0.0.1:9000", "valid"),
        ("https://attacker.example", "valid"),
        ("null", "valid"),
        ("http://127.0.0.1:8081", "wrong"),
    ],
)
def test_login_csrf_and_cross_origin_fail_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    csrf: str,
) -> None:
    seen = []
    client, _ = app_client(monkeypatch, lambda *args: seen.append(args))
    client.cookies.set(CSRF_COOKIE, "valid")
    response = client.post(
        "/login",
        data={"account": "operator", "password": "private", "csrf": csrf},
        headers={"Origin": origin},
    )
    assert response.status_code == 403 and not seen
    assert not client.cookies.get(SESSION_COOKIE)


def test_invalid_credentials_or_network_failure_do_not_authenticate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> None:
        raise LoginError("账号验证未通过。")

    client, _ = app_client(monkeypatch, fail)
    response = login(client)
    assert response.status_code == 400 and "账号验证未通过" in response.text
    assert "private-password" not in response.text
    assert not client.cookies.get(SESSION_COOKIE)


def test_logout_revokes_copied_cookie_and_requires_csrf(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store = app_client(monkeypatch)
    assert login(client).status_code == 303
    token = client.cookies.get(SESSION_COOKIE)
    session = store.get(token)
    assert session
    other = TestClient(client.app, base_url="http://127.0.0.1:8081")
    other.cookies.set(SESSION_COOKIE, session.token)
    response = client.post(
        "/logout", data={"csrf": "wrong"}, headers={"Origin": "http://127.0.0.1:8081"}
    )
    assert response.status_code == 403 and store.get(token)
    response = client.post(
        "/logout",
        data={"csrf": session.csrf},
        headers={"Origin": "http://127.0.0.1:8081"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and not client.cookies.get(SESSION_COOKIE)
    assert store.get(token) is None
    assert other.get("/_nicegui/client/download").status_code == 401


def test_login_rate_limit_bounds_upstream_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    client, _ = app_client(monkeypatch, lambda *args: seen.append(args))
    form = login_form(client)
    for _ in range(10):
        response = client.post(
            "/login", data=form, headers={"Origin": "http://127.0.0.1:8081"}, follow_redirects=False
        )
        # 认证成功清除CSRF cookie，重新设置同一表单token进行限速验证。
        client.cookies.set(CSRF_COOKIE, form["csrf"])
        assert response.status_code == 303
    response = client.post(
        "/login", data=form, headers={"Origin": "http://127.0.0.1:8081"}, follow_redirects=False
    )
    assert response.status_code == 429 and len(seen) == 10


def test_attempt_window_expires() -> None:
    clock = [1.0]
    limiter = AttemptLimiter(lambda: clock[0])
    assert all(limiter.allow("local") for _ in range(10))
    assert not limiter.allow("local")
    clock[0] += 61
    assert limiter.allow("local")


def test_huge_or_duplicate_form_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = app_client(monkeypatch)
    huge = client.post(
        "/login",
        content="a=" + "x" * 9000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert huge.status_code == 400
    duplicate = client.post(
        "/login",
        content="account=a&account=b",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert duplicate.status_code == 400


def test_real_protocol_checks_user_project_and_discards_token() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(request.url.path)
        if request.url.path == LOGIN_PATH:
            assert body["account"] == "typed-account" and body["pwd"] != "typed-password"
            return httpx.Response(
                200,
                json={"success": True, "code": 200, "data": {"companyId": 49, "userId": 12}},
                headers={"Access-Token": "private-upstream-token"},
            )
        assert request.url.path == PROJECT_PATH
        assert request.headers["Authorization"] == "Bearer private-upstream-token"
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": [{"projectId": 832}]}
        )

    assert (
        authenticate_account(
            "typed-account", "typed-password", transport=httpx.MockTransport(handler)
        )
        is None
    )
    assert seen == [LOGIN_PATH, PROJECT_PATH]


def test_account_without_project_cannot_login() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        data: object = {"companyId": 49, "userId": 12} if request.url.path == LOGIN_PATH else []
        return httpx.Response(
            200,
            json={"success": True, "code": 200, "data": data},
            headers={"Access-Token": "private-token"},
        )

    with pytest.raises(LoginError, match="没有芒市机场项目权限"):
        authenticate_account("typed", "private", transport=httpx.MockTransport(handler))


def test_source_network_errors_are_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret password private-host", request=request)

    with pytest.raises(LoginError) as error:
        authenticate_account("typed", "private", transport=httpx.MockTransport(handler))
    assert "无法连接小麦平台" in str(error.value)
    assert "secret" not in str(error.value)


def test_websocket_same_origin_is_accepted_and_cross_port_is_denied() -> None:
    store = SessionStore()
    session = store.create("operator")
    app = FastAPI()
    app.add_middleware(AuthMiddleware, store=store)

    @app.websocket("/_nicegui_ws/socket.io")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    client = TestClient(app, base_url="http://127.0.0.1:8081")
    headers = {"Cookie": f"{SESSION_COOKIE}={session.token}", "Origin": "http://127.0.0.1:8081"}
    with client.websocket_connect(
        "ws://127.0.0.1:8081/_nicegui_ws/socket.io", headers=headers
    ) as ws:
        assert ws.receive_text() == "ready"
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            "ws://127.0.0.1:8081/_nicegui_ws/socket.io",
            headers={**headers, "Origin": "http://127.0.0.1:9000"},
        ):
            pass
    assert error.value.code == 1008


def test_revoked_websocket_cannot_emit_queued_data() -> None:
    store = SessionStore()
    session = store.create("operator")
    app = FastAPI()
    app.add_middleware(AuthMiddleware, store=store)

    @app.websocket("/socket")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        store.revoke(session.token)
        await websocket.send_text("private-report")

    client = TestClient(app)
    with client.websocket_connect(
        "/socket", headers={"Cookie": f"{SESSION_COOKIE}={session.token}"}
    ) as ws:
        response = ws.receive()
        assert response["type"] == "websocket.close" and response["code"] == 1008
        assert "private-report" not in str(response)


def test_polling_rejects_cross_port_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = app_client(monkeypatch)
    assert login(client).status_code == 303
    response = client.get("/_nicegui_ws/socket.io", headers={"Origin": "http://127.0.0.1:9999"})
    assert response.status_code == 403
