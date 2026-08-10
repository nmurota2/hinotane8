from __future__ import annotations

import pandas as pd
import pytest
from factories import synthetic_bars

from hinotane.indicators import enrich
from hinotane.strategies import available, get_strategy

ALL = available()


@pytest.mark.parametrize("name", ALL)
def test_evaluate_returns_required_columns(name):
    strategy = get_strategy(name)
    out = strategy.evaluate(enrich(synthetic_bars("10000", n=300, seed=1)))
    for col in ("entry", "stop_price", "target_price", "score", "reason"):
        assert col in out.columns
    assert out["entry"].dtype == bool


@pytest.mark.parametrize("name", ALL)
def test_stop_is_always_below_entry_when_signalled(name):
    """損切り価格がエントリー価格以上になっていたら、リスク計算が破綻する。"""
    strategy = get_strategy(name)
    for seed in range(8):
        out = strategy.evaluate(enrich(synthetic_bars("10000", n=400, seed=seed, trend=0.002)))
        hits = out[out["entry"]]
        if hits.empty:
            continue
        assert (hits["stop_price"] < hits["close"]).all(), f"{name}: 損切りが終値以上"
        assert (hits["target_price"] > hits["close"]).all(), f"{name}: 利確目標が終値以下"


@pytest.mark.parametrize("name", ALL)
def test_no_lookahead_bias(name):
    """未来のデータを見ていないことの確認。

    途中で打ち切ったデータで評価しても、共通部分の判定は一致するはず。
    一致しないなら、その戦略は未来の情報を使っている。
    """
    strategy = get_strategy(name)
    bars = synthetic_bars("10000", n=400, seed=3, trend=0.0018)

    full = strategy.evaluate(enrich(bars))
    truncated = strategy.evaluate(enrich(bars.iloc[:300].copy()))

    common = min(len(truncated), len(full))
    pd.testing.assert_series_equal(
        full["entry"].iloc[:common].reset_index(drop=True),
        truncated["entry"].iloc[:common].reset_index(drop=True),
        check_names=False,
    )


@pytest.mark.parametrize("name", ALL)
def test_latest_signal_returns_none_for_short_history(name):
    strategy = get_strategy(name)
    short = enrich(synthetic_bars("10000", n=30))
    assert strategy.latest_signal(short, "10000", "テスト") is None


def test_breakout_fires_on_clean_uptrend():
    """明確な上昇＋高値更新＋出来高増の作為的なデータでシグナルが出ること。"""
    bars = synthetic_bars("10000", n=300, seed=7, trend=0.003)
    # 最終日に高値更新と出来高急増を作る
    bars.loc[bars.index[-1], "close"] = bars["high"].iloc[-60:-1].max() * 1.03
    bars.loc[bars.index[-1], "high"] = bars["close"].iloc[-1] * 1.005
    bars.loc[bars.index[-1], "open"] = bars["close"].iloc[-1] * 0.995
    bars.loc[bars.index[-1], "low"] = bars["open"].iloc[-1] * 0.995
    bars.loc[bars.index[-1], "volume"] = bars["volume"].iloc[-30:-1].mean() * 3

    out = get_strategy("breakout").evaluate(enrich(bars))
    row = out.iloc[-1]
    # 直前日比 +10% 以上だと「行き過ぎ」で弾かれる仕様なので、そこだけ確認して分岐
    jump = bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1
    if jump < 0.10:
        assert bool(row["entry"]), "上昇トレンドでの高値ブレイクを検出できていない"
        assert row["stop_price"] < row["close"] < row["target_price"]
