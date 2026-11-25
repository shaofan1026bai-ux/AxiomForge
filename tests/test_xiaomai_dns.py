"""验证小麦 DNS 回退只处理指定上游的解析失败，并保持 TLS 域名。"""

import socket
import ssl
from time import monotonic

import httpx
import pytest

from axiomforge import xiaomai_client

_URL = httpx.URL(xiaomai_client.BASE_URL + xiaomai_client.HISTORY_PATH)
_RESOLVER = "192.0.2.53"
_ADDRESS = "192.0.2.42"


def _connect_error(request: httpx.Request, cause: Exception) -> None:
    try:
        raise cause
    except (socket.gaierror, ssl.SSLCertVerificationError) as error:
        raise httpx.ConnectError("连接失败", request=request) from error


def test_dns_fallback_preserves_hostname_and_request(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []
    resolutions = []
    context = ssl.create_default_context()

    def handle_request(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == _URL.host:
            _connect_error(request, socket.gaierror(socket.EAI_NONAME, "无法解析"))
        return httpx.Response(200, request=request)

    def resolve(host: str, resolver: str) -> str:
        resolutions.append((host, resolver))
        return _ADDRESS

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(xiaomai_client, "_resolve_fallback", resolve)
    transport = xiaomai_client._XiaomaiTransport(context, _RESOLVER)
    with httpx.Client(transport=transport) as client:
        response = client.post(_URL, json={"pointIdList": ["point"]})

    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert resolutions == [(_URL.host, _RESOLVER)]
    assert len(requests) == 2
    original, retry = requests
    assert response.request.url == original.url == _URL
    assert retry.url == _URL.copy_with(host=_ADDRESS)
    assert retry.headers["Host"] == f"{_URL.host}:{_URL.port}"
    assert retry.extensions["sni_hostname"] == _URL.host
    assert b"".join(retry.stream) == original.content


@pytest.mark.parametrize(
    "url, cause",
    [
        (_URL, ssl.SSLCertVerificationError(1, "证书错误")),
        (_URL.copy_with(host="example.invalid"), socket.gaierror(socket.EAI_NONAME)),
        (_URL.copy_with(port=443), socket.gaierror(socket.EAI_NONAME)),
    ],
)
def test_dns_fallback_skips_tls_failures_and_other_origins(
    monkeypatch: pytest.MonkeyPatch,
    url: httpx.URL,
    cause: Exception,
) -> None:
    def handle_request(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        _connect_error(request, cause)
        raise AssertionError("连接错误应向上传递")

    def unexpected_resolution(*_: str) -> str:
        pytest.fail("当前错误不应触发小麦备用 DNS")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(xiaomai_client, "_resolve_fallback", unexpected_resolution)
    transport = xiaomai_client._XiaomaiTransport(ssl.create_default_context(), _RESOLVER)
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get(url)


def test_unresolved_dns_failure_is_reported_as_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def handle_request(_: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        _connect_error(request, socket.gaierror(socket.EAI_NONAME, "private upstream detail"))
        raise AssertionError("解析错误应向上传递")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(xiaomai_client, "_resolve_fallback", lambda *_: None)
    transport = xiaomai_client._XiaomaiTransport(ssl.create_default_context(), _RESOLVER)
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(xiaomai_client.XiaomaiDnsError) as error,
    ):
        client.get(_URL)
    assert "DNS" in str(error.value)
    assert "private upstream detail" not in str(error.value)


def test_readonly_client_classifies_system_dns_failure_without_fallback() -> None:
    def handle_request(request: httpx.Request) -> httpx.Response:
        _connect_error(request, socket.gaierror(socket.EAI_AGAIN, "private resolver detail"))
        raise AssertionError("解析错误应向上传递")

    with httpx.Client(transport=httpx.MockTransport(handle_request)) as client:
        source = xiaomai_client.XiaomaiClient(client, monotonic() + 5, 5)
        with pytest.raises(xiaomai_client.XiaomaiDnsError) as error:
            source.post(xiaomai_client.HISTORY_PATH, {})
    assert "DNS" in str(error.value)
    assert "private resolver detail" not in str(error.value)


def test_httpcore_wrapped_system_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise socket.gaierror(socket.EAI_NONAME, "private resolver detail")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    monkeypatch.delenv("AXIOMFORGE_XIAOMAI_DNS_SERVER", raising=False)
    with xiaomai_client.http_client() as client:
        source = xiaomai_client.XiaomaiClient(client, monotonic() + 5, 5)
        with pytest.raises(xiaomai_client.XiaomaiDnsError, match="本机无法解析"):
            source.post(xiaomai_client.HISTORY_PATH, {})
