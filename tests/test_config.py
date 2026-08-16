"""時刻まわりの回帰テスト。

VPS のシステム時刻が UTC のままでも、承認の有効期限が 9 時間ずれないこと。
ここが壊れると「夜に承認したのに翌朝の執行時点で期限切れ」という
静かな事故が起きるので、テストで固定しておく。
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from hinotane.config import JST, now, today


@pytest.fixture
def utc_host():
    """ホストのタイムゾーンを UTC に切り替える（VPS 初期状態の再現）。"""
    original = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if original is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_now_returns_jst_wall_clock_even_on_utc_host(utc_host):
    expected = datetime.now(JST).replace(tzinfo=None)
    assert abs(now() - expected) < timedelta(seconds=2)


def test_now_is_ahead_of_utc_by_nine_hours(utc_host):
    utc_naive = datetime.now(UTC).replace(tzinfo=None)
    delta = now() - utc_naive
    assert timedelta(hours=8, minutes=59) < delta < timedelta(hours=9, minutes=1)


def test_now_is_naive_so_it_compares_with_duckdb_timestamps():
    # DuckDB の TIMESTAMP 列は naive で返るため、aware だと比較で TypeError になる
    assert now().tzinfo is None


def test_today_matches_jst_date(utc_host):
    assert today() == datetime.now(JST).date()


# ---------------------------------------------------------------- ベースURL


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # V1 は 2026/6/1 に廃止済み。古い .env が残っていても動くよう読み替える
        ("https://api.jquants.com/v1", "https://api.jquants.com/v2"),
        ("https://api.jquants.com/v1/", "https://api.jquants.com/v2"),
        # 正しい指定はそのまま
        ("https://api.jquants.com/v2", "https://api.jquants.com/v2"),
        ("https://api.jquants.com/v2/", "https://api.jquants.com/v2"),
        # バージョン無しなら補う
        ("https://api.jquants.com", "https://api.jquants.com/v2"),
        # 未設定なら既定値
        ("", "https://api.jquants.com/v2"),
        (None, "https://api.jquants.com/v2"),
    ],
)
def test_base_url_is_normalised_to_v2(configured, expected):
    from hinotane.config import _normalize_jquants_base_url

    assert _normalize_jquants_base_url(configured) == expected


# ------------------------------------------------------- 二重ペーストの検出


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # 秘密情報の入力は画面に出ないため、不安になって二重に貼る事故が多い
        ("abcd1234abcd1234", "abcd1234"),
        ("x" * 86, "x" * 43),
        # 二重ではないものを誤検出しない
        ("abcd1234xyz", None),
        ("abcd1234", None),
        # 短すぎるものは判定しない（偶然一致しうるため）
        ("abab", None),
        ("", None),
        (None, None),
    ],
)
def test_doubled_secret_detection(value, expected):
    from hinotane.cli import _looks_doubled

    assert _looks_doubled(value) == expected


def test_package_is_runnable_as_module():
    """`python -m hinotane` の経路が壊れていないこと。

    pip のインストール登録が壊れても動く逃げ道なので、
    ここが折れると復旧手段がなくなる。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "hinotane", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root / "src")},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "doctor" in result.stdout


def test_request_interval_keeps_a_safety_margin():
    """上限ちょうどの間隔で撃たないこと。

    境界で 429 を食らうと 60 秒待たされ、結局そのほうが遅くなる。
    """
    from hinotane.config import JQuantsConfig

    cfg = JQuantsConfig(api_key="k", requests_per_min=5)
    # 60 秒の窓に上限ちょうど（5 回）入る間隔だと境界で弾かれる。
    # 窓あたり 4 回以下に収まる間隔でなければならない。
    assert cfg.min_request_interval_sec > 60.0 / 5, "上限ちょうどの間隔になっている"
    assert cfg.min_request_interval_sec > 60.0 / 4 * 0.99, "60秒の窓に5回入ってしまう"
    # ただし極端に遅くはしない
    assert cfg.min_request_interval_sec < 20.0

    fast = JQuantsConfig(api_key="k", requests_per_min=60)
    assert 1.0 < fast.min_request_interval_sec < 2.0


def test_plan_command_writes_the_rate_even_when_the_key_is_missing(tmp_path, monkeypatch):
    """.env にその行が無くても、取得レートを確実に書き込むこと。

    回帰テスト: 以前は `sed -i 's/^JQUANTS_REQUESTS_PER_MIN=.*/.../' .env` と
    案内していたが、sed は該当行が無いと **何も言わずに何もしない**。
    有料プランに切り替えたのに 5 回/分のまま 5 年分を取りに行き、
    30 分で済むはずの取得が 237 分と表示される事故が実際に起きた。
    """
    from argparse import Namespace

    from hinotane import cli

    env = tmp_path / ".env"
    env.write_text("JQUANTS_API_KEY=dummy\n")   # レートの行は無い
    monkeypatch.setattr(cli_config_module(), "PROJECT_ROOT", tmp_path)

    assert cli.cmd_plan(Namespace(plan="light"), None, None) == 0
    assert "JQUANTS_REQUESTS_PER_MIN=60" in env.read_text()
    assert "JQUANTS_API_KEY=dummy" in env.read_text(), "既存の設定を消してはいけない"


def test_plan_command_replaces_an_existing_value(tmp_path, monkeypatch):
    """既にある行は重複させず書き換えること。"""
    from argparse import Namespace

    from hinotane import cli

    env = tmp_path / ".env"
    env.write_text("JQUANTS_REQUESTS_PER_MIN=5\nRISK_EQUITY_JPY=1000000\n")
    monkeypatch.setattr(cli_config_module(), "PROJECT_ROOT", tmp_path)

    assert cli.cmd_plan(Namespace(plan="standard"), None, None) == 0
    text = env.read_text()
    assert text.count("JQUANTS_REQUESTS_PER_MIN=") == 1
    assert "JQUANTS_REQUESTS_PER_MIN=60" in text
    assert "RISK_EQUITY_JPY=1000000" in text


def cli_config_module():
    from hinotane import config

    return config
