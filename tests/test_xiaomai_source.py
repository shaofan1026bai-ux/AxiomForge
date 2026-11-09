"""用假上游验证认证边界与失败行为，不读取真实配置或连接现场。"""

import base64
import json
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from test_power_temperature import START, query, samples

from axiomforge.power_temperature import (
    INTERVAL,
    POINTS,
    POWER,
    PROJECT_ID,
    SHANGHAI,
    TEMPERATURE,
    QueryRange,
)
from axiomforge.xiaomai_source import (
    HISTORY_PATH,
    LOGIN_PATH,
    POINT_PATH,
    PROJECT_PATH,
    Settings,
    XiaomaiSourceError,
    encrypt_password,
    load_power_temperature,
    load_settings,
)


@pytest.fixture
def config():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return Settings("test-account", "test-password", public)


def metadata(spec):
    return {
        "deviceId": spec.device_id,
        "projectId": PROJECT_ID,
        "pointId": spec.point_id,
        "unit": spec.unit,
    }


def handler(overrides=None, calls=None):
    overrides = overrides or {}

    def respond(request):
        path = request.url.path
        body = json.loads(request.content)
        if calls is not None:
            calls.append((path, body))
        if path in overrides:
            return overrides[path](request)
        headers = {}
        if path == LOGIN_PATH:
            data = {"userId": 10, "companyId": 49}
            headers = {"Access-Token": "test-token"}
            assert body["pwd"] != "test-password"
        else:
            assert request.headers["Authorization"] == "Bearer test-token"
            if path == PROJECT_PATH:
                data = [{"projectId": PROJECT_ID}]
            elif path == POINT_PATH:
                data = [metadata(spec) for spec in POINTS]
            elif path == HISTORY_PATH:
                data = [
                    {**metadata(spec), "pointDataList": samples([1, 2, 3, None])} for spec in POINTS
                ]
            else:
                raise AssertionError(path)
        return httpx.Response(
            200, json={"success": True, "code": 200, "data": data}, headers=headers
        )

    return respond


def test_rsa_ciphertext_decrypts_and_is_randomized():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    first, second = encrypt_password("test密码", public), encrypt_password("test密码", public)
    assert first != second
    assert key.decrypt(base64.b64decode(first), padding.PKCS1v15()).decode() == "test密码"


def test_full_flow_uses_token_validates_mapping_and_batches_points(config):
    calls = []
    result = load_power_temperature(
        query(), settings=config, transport=httpx.MockTransport(handler(calls=calls))
    )
    assert [path for path, _ in calls] == [LOGIN_PATH, PROJECT_PATH, POINT_PATH, HISTORY_PATH]
    assert calls[-1][1]["pointIdList"] == [POWER.point_id, TEMPERATURE.point_id]
    assert result.rows[0].power == 1
    assert not hasattr(result, "token")
    assert "test-password" not in repr(config)


@pytest.mark.parametrize(
    "path, response",
    [
        (
            LOGIN_PATH,
            lambda _: httpx.Response(
                200, json={"success": False, "code": 400, "message": "private"}
            ),
        ),
        (
            LOGIN_PATH,
            lambda _: httpx.Response(
                200, json={"success": True, "code": 200, "data": {"userId": 1, "companyId": 49}}
            ),
        ),
        (
            PROJECT_PATH,
            lambda _: httpx.Response(200, json={"success": True, "code": 200, "data": []}),
        ),
        (
            POINT_PATH,
            lambda _: httpx.Response(
                200, json={"success": True, "code": 200, "data": [metadata(POWER)]}
            ),
        ),
        (
            HISTORY_PATH,
            lambda _: httpx.Response(200, json={"success": True, "code": 200, "data": [{}]}),
        ),
        (
            HISTORY_PATH,
            lambda _: httpx.Response(302, headers={"location": "https://other.example"}),
        ),
        (HISTORY_PATH, lambda _: httpx.Response(401, text="private token test-token")),
        (HISTORY_PATH, lambda _: httpx.Response(200, text="private invalid JSON")),
    ],
)
def test_errors_are_not_empty_success_or_sensitive(config, path, response):
    with pytest.raises(XiaomaiSourceError) as error:
        load_power_temperature(
            query(), settings=config, transport=httpx.MockTransport(handler({path: response}))
        )
    for secret in ["test-password", "test-token", "private", "other.example"]:
        assert secret not in str(error.value)


def test_wrong_project_or_unit_is_rejected(config):
    for field, wrong in [("projectId", 779), ("deviceId", 1), ("unit", "kWh")]:

        def response(_, field=field, wrong=wrong):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 200,
                    "data": [
                        {**metadata(spec), field: wrong, "pointDataList": []} for spec in POINTS
                    ],
                },
            )

        with pytest.raises(XiaomaiSourceError, match="不一致"):
            load_power_temperature(
                query(),
                settings=config,
                transport=httpx.MockTransport(handler({HISTORY_PATH: response})),
            )


def test_partial_segment_failure_does_not_publish(config):
    calls = 0

    def history(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise httpx.ReadTimeout("private-token", request=request)
        return handler()(request)

    with pytest.raises(XiaomaiSourceError, match="超时"):
        load_power_temperature(
            QueryRange(START, START + timedelta(days=2)),
            settings=config,
            transport=httpx.MockTransport(handler({HISTORY_PATH: history})),
        )
    assert calls == 2


@pytest.mark.parametrize("bad_timestamp", ["invalid-date", "2026-01-02 00:01:00"])
def test_valid_segment_cannot_hide_another_segments_incompatible_times(config, bad_timestamp):
    calls = 0

    def history(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return handler()(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 200,
                "data": [
                    {**metadata(spec), "pointDataList": [{"ts": bad_timestamp, "val": 1}]}
                    for spec in POINTS
                ],
            },
        )

    with pytest.raises(XiaomaiSourceError, match="无法解析"):
        load_power_temperature(
            QueryRange(START, START + timedelta(days=2)),
            settings=config,
            transport=httpx.MockTransport(handler({HISTORY_PATH: history})),
        )
    assert calls == 2


def test_missing_series_is_valid_partial_snapshot(config):
    def history(_):
        return httpx.Response(
            200,
            json={
                "success": True,
                "code": 200,
                "data": [{**metadata(POWER), "pointDataList": samples([0, 1])}],
            },
        )

    snapshot = load_power_temperature(
        query(), settings=config, transport=httpx.MockTransport(handler({HISTORY_PATH: history}))
    )
    assert snapshot.rows[0].power == 0
    assert all(row.temperature is None for row in snapshot.rows)


def test_segment_boundaries_are_deduplicated_without_losing_requested_grid(config):
    ranged = QueryRange(START, START + timedelta(days=2, minutes=4))
    history_calls = []

    def history(request):
        body = json.loads(request.content)
        window = body["timeQuantum"]
        first = datetime.fromisoformat(window["startDate"]).replace(tzinfo=SHANGHAI)
        last = datetime.fromisoformat(window["endDate"]).replace(tzinfo=SHANGHAI)
        history_calls.append((first, last))
        stamps = QueryRange(first, last).grid() + (last,)
        data = [
            {
                **metadata(spec),
                "pointDataList": [
                    {
                        "ts": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "val": (stamp - START) / INTERVAL,
                    }
                    for stamp in stamps
                ],
            }
            for spec in POINTS
        ]
        return httpx.Response(200, json={"success": True, "code": 200, "data": data})

    snapshot = load_power_temperature(
        ranged,
        settings=config,
        transport=httpx.MockTransport(handler({HISTORY_PATH: history})),
    )
    assert len(history_calls) == 3
    assert tuple(row.time for row in snapshot.rows) == ranged.grid()
    assert all(row.paired for row in snapshot.rows)
    assert snapshot.quality.duplicates == 4
    assert snapshot.quality.conflicts == 0
    assert snapshot.quality.outside == 2


def test_total_deadline_is_checked_during_stream(config):
    ticks = iter([0, 0, 1, 2, 3, 4, 5, 6, 91, 92])
    with pytest.raises(XiaomaiSourceError, match="90秒"):
        load_power_temperature(
            query(),
            settings=config,
            transport=httpx.MockTransport(handler()),
            clock=lambda: next(ticks, 100),
        )


def test_external_configuration_does_not_expand_password(tmp_path, monkeypatch):
    path = tmp_path / "xiaomai.env"
    path.write_text("XIAOMAI_ACCOUNT='test'\nXIAOMAI_PASSWORD='${UNSET_TEST}'\n")
    monkeypatch.setenv("AXIOMFORGE_XIAOMAI_ENV_FILE", str(path))
    monkeypatch.delenv("XIAOMAI_ACCOUNT", raising=False)
    monkeypatch.delenv("XIAOMAI_PASSWORD", raising=False)
    assert load_settings().password == "${UNSET_TEST}"
    path.write_text(
        "XIAOMAI_ACCOUNT='test'\nXIAOMAI_PASSWORD='value'\nXIAOMAI_LOGIN_URL='https://iot.sinocold.net/api/UserMicro/User/UserLogin'\n"
    )
    with pytest.raises(XiaomaiSourceError, match="航信"):
        load_settings()
