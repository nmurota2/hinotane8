"""J-Quants クライアントのテスト。

外部 API は呼ばず、レスポンスを差し替えて検証する。重視しているのは 2 点:

1. **失敗の分類** — 「認証の失敗」と「ネットワーク不通」を取り違えると、
   ユーザーに間違った直し方を案内してしまう。
2. **カラム名の正規化** — V2 で `Open` → `O` のように短縮されたため、
   どちらの命名でも読めることを保証する。
"""

from __future__ import annotations

from datetime import date

import pytest
import requests

from hinotane.config import JQuantsConfig
from hinotane.datasource.jquants import (
    JQuantsAuthError,
    JQuantsClient,
    JQuantsError,
    JQuantsGoneError,
    JQuantsNetworkError,
    display_code,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """呼び出し回数を数えつつ、あらかじめ決めた応答を順に返すセッション。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """リトライの待ち時間でテストが遅くならないようにする。"""
    monkeypatch.setattr("hinotane.datasource.jquants.time.sleep", lambda _: None)


def _client(session, **cfg_kwargs) -> JQuantsClient:
    cfg = JQuantsConfig(api_key="dummy-key", max_retries=3, **cfg_kwargs)
    client = JQuantsClient(cfg)
    client._session = session
    return client


# ------------------------------------------------------------------ 失敗の分類


def test_403_raises_auth_error_without_retrying():
    """APIキー方式で 403 なら再試行しても無駄。即座に認証エラーにする。"""
    session = FakeSession([FakeResponse(403, text="Forbidden")])
    with pytest.raises(JQuantsAuthError):
        _client(session).listed_info()
    assert len(session.calls) == 1, "認証エラーで無駄なリトライをしている"


def test_410_raises_gone_error_pointing_at_base_url():
    """V1 の URL が残っていると 410 が返る。原因が URL だと分かるメッセージにする。"""
    session = FakeSession(
        [FakeResponse(410, text='{"message": "J-QuantsはV2に移行しました。"}')]
    )
    with pytest.raises(JQuantsGoneError) as excinfo:
        _client(session).listed_info()
    assert "v2" in str(excinfo.value)
    assert len(session.calls) == 1, "410 は再試行しても無駄"


def test_401_raises_auth_error():
    session = FakeSession([FakeResponse(401, text="Unauthorized")])
    with pytest.raises(JQuantsAuthError):
        _client(session).listed_info()


def test_connection_failure_raises_network_error_not_auth_error():
    """ネットワーク不通を認証エラーと取り違えないこと。

    ここを間違えると「APIキーを疑ってください」と案内してしまい、
    ユーザーが延々と正しいキーを貼り直すことになる。
    """
    session = FakeSession([requests.ConnectionError("proxy unreachable")])
    with pytest.raises(JQuantsNetworkError):
        _client(session).listed_info()
    assert len(session.calls) == 3, "ネットワーク障害はリトライすべき"


def test_timeout_raises_network_error():
    session = FakeSession([requests.Timeout("timed out")])
    with pytest.raises(JQuantsNetworkError):
        _client(session).listed_info()


def test_network_error_and_auth_error_are_both_jquants_error():
    """呼び出し側が JQuantsError だけ捕まえれば済むようにしておく。"""
    assert issubclass(JQuantsAuthError, JQuantsError)
    assert issubclass(JQuantsNetworkError, JQuantsError)


def test_rate_limit_is_retried_then_succeeds():
    session = FakeSession(
        [
            FakeResponse(429, text="Too Many Requests"),
            FakeResponse(429, text="Too Many Requests"),
            FakeResponse(200, {"data": [{"Code": "13010", "CompanyName": "テスト"}]}),
        ]
    )
    df = _client(session).listed_info()
    assert len(df) == 1
    assert len(session.calls) == 3


# ------------------------------------------------------------------ 正規化


# V2 の調整後カラム（推奨。分割をまたいでも系列が連続する）
V2_ADJ_ROW = {
    "Code": "72030",
    "Date": "2026-08-07",
    "O": 1500.0, "H": 1550.0, "L": 1475.0, "C": 1525.0, "Vo": 500_000.0,
    "AdjO": 3000.0,
    "AdjH": 3100.0,
    "AdjL": 2950.0,
    "AdjC": 3050.0,
    "AdjVo": 1_000_000.0,
    "Va": 3_050_000_000.0,
}

# 調整後カラムを持たないケース（未調整のみ）
V2_RAW_ROW = {
    "Code": "72030",
    "Date": "2026-08-07",
    "O": 3000.0,
    "H": 3100.0,
    "L": 2950.0,
    "C": 3050.0,
    "Vo": 1_000_000.0,
    "Va": 3_050_000_000.0,
}

# V1 時代の名前（古いデータを読ませたときの保険）
V1_ROW = {
    "Code": "72030",
    "Date": "2026-08-07",
    "AdjustmentOpen": 3000.0,
    "AdjustmentHigh": 3100.0,
    "AdjustmentLow": 2950.0,
    "AdjustmentClose": 3050.0,
    "AdjustmentVolume": 1_000_000.0,
    "TurnoverValue": 3_050_000_000.0,
}


@pytest.mark.parametrize(
    ("label", "row"),
    [("V2調整後", V2_ADJ_ROW), ("V2未調整のみ", V2_RAW_ROW), ("V1互換", V1_ROW)],
)
def test_quote_columns_normalised_for_both_api_versions(label, row):
    session = FakeSession([FakeResponse(200, {"data": [row]})])
    df = _client(session).daily_quotes_by_date(date(2026, 8, 7))
    assert list(df.columns) == [
        "code", "date", "open", "high", "low", "close", "volume", "turnover_value"
    ], label
    assert df.iloc[0]["close"] == 3050.0
    assert df.iloc[0]["open"] == 3000.0


def test_missing_required_column_reports_actual_columns():
    """未知の形式でも、何が返ってきたかが分かるエラーにする。"""
    session = FakeSession([FakeResponse(200, {"data": [{"Foo": 1, "Bar": 2}]})])
    with pytest.raises(JQuantsError) as excinfo:
        _client(session).daily_quotes_by_date(date(2026, 8, 7))
    message = str(excinfo.value)
    assert "Foo" in message and "Bar" in message, "実際のカラム一覧がエラーに含まれていない"


def test_turnover_value_falls_back_to_close_times_volume():
    """売買代金を返さない契約プランでも、流動性フィルタが機能すること。"""
    row = dict(V2_ADJ_ROW)
    del row["Va"]
    session = FakeSession([FakeResponse(200, {"data": [row]})])
    df = _client(session).daily_quotes_by_date(date(2026, 8, 7))
    assert df.iloc[0]["turnover_value"] == pytest.approx(3050.0 * 1_000_000)


def test_rows_without_close_are_dropped():
    """売買停止などで終値が無い日は捨てる（指標計算が壊れるため）。"""
    bad = dict(V2_ADJ_ROW)
    bad["AdjC"] = None
    session = FakeSession([FakeResponse(200, {"data": [bad, V2_ADJ_ROW]})])
    df = _client(session).daily_quotes_by_date(date(2026, 8, 7))
    assert len(df) == 1


# ------------------------------------------------------------------ ページング


def test_pagination_key_is_followed_until_exhausted():
    session = FakeSession(
        [
            FakeResponse(200, {"data": [{"Code": "1", "CompanyName": "A"}], "pagination_key": "k1"}),
            FakeResponse(200, {"data": [{"Code": "2", "CompanyName": "B"}], "pagination_key": "k2"}),
            FakeResponse(200, {"data": [{"Code": "3", "CompanyName": "C"}]}),
        ]
    )
    df = _client(session).listed_info()
    assert len(df) == 3
    assert session.calls[1]["params"]["pagination_key"] == "k1"
    assert session.calls[2]["params"]["pagination_key"] == "k2"


def test_api_key_is_sent_in_header():
    session = FakeSession([FakeResponse(200, {"data": []})])
    _client(session).listed_info()
    assert session.calls[0]["headers"]["x-api-key"] == "dummy-key"


def test_v2_endpoint_paths_are_used():
    """V1 のパス（/listed/info, /prices/daily_quotes）を叩かないこと。"""
    session = FakeSession([FakeResponse(200, {"data": []})])
    _client(session).listed_info()
    assert session.calls[0]["url"] == "https://api.jquants.com/v2/equities/master"

    session = FakeSession([FakeResponse(200, {"data": []})])
    _client(session).daily_quotes_by_date(date(2026, 8, 7))
    assert session.calls[0]["url"] == "https://api.jquants.com/v2/equities/bars/daily"
    assert session.calls[0]["params"]["date"] == "2026-08-07"


def test_adjusted_columns_win_over_raw():
    """分割の影響を受けない調整後価格を優先すること。"""
    session = FakeSession([FakeResponse(200, {"data": [V2_ADJ_ROW]})])
    df = _client(session).daily_quotes_by_date(date(2026, 8, 7))
    assert df.iloc[0]["close"] == 3050.0, "未調整の C を拾っている"
    assert df.iloc[0]["volume"] == 1_000_000.0


def test_listed_info_maps_v2_master_columns():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "data": [
                        {
                            "Code": "72030",
                            "CoName": "トヨタ自動車",
                            "Mkt": "0111",
                            "S17": "6",
                            "S33": "3700",
                            "ScaleCat": "TOPIX Core30",
                        }
                    ]
                },
            )
        ]
    )
    df = _client(session).listed_info()
    row = df.iloc[0]
    assert row["name"] == "トヨタ自動車"
    assert row["market_code"] == "0111"
    assert row["sector33_code"] == "3700"


# ------------------------------------------------------------------ コード表記


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("72030", "7203"), ("13010", "1301"), ("9432", "9432"), ("130A0", "130A")],
)
def test_display_code_strips_jquants_padding(raw, expected):
    assert display_code(raw) == expected
