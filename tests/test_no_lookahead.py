"""バックテスターが「未来を覗いていない」ことを検証する。

このプロジェクトで最も重要なテスト。

戦略が儲かるかどうかは市場次第で保証できないが、**バックテスターが
ありもしない利益を作り出さないこと**は保証できる。ここが壊れていると、
どんな戦略でも「勝てる」と表示され、その数字を信じて実弾を入れることになる。

実際に起きたこと:
    流動性で銘柄を絞る際、検証期間の **末尾** 60 本の売買代金で判定していた。
    売買代金は株価×出来高なので、これは「期間の終わりに値上がりしていた銘柄」を
    期間の初めから売買することに等しい。完全なノイズでも +0.15R の期待値が出た。

対策:
    上がりも下がりもしない乱数データを流し、期待値がプラスにならないことを確認する。
    ノイズから利益が出るなら、それは戦略の実力ではなく実装の欠陥である。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from hinotane.backtest import run_backtest
from hinotane.config import ExecutionConfig, RiskConfig, ScreenerConfig
from hinotane.db import Database


def _driftless_market(db: Database, n_codes: int = 120, n_days: int = 320, seed: int = 7) -> None:
    """上がりも下がりもしない市場を作る。

    価格は幾何ブラウン運動だが、**算術平均リターンがゼロになるよう**
    log ドリフトを -σ²/2 に設定する。ここを 0 にすると価格自体は
    σ²/2 だけ上に漂い、買い戦略が有利になってしまう。
    """
    rng = np.random.default_rng(seed)
    sigma = 0.018
    dates = pd.bdate_range("2024-01-01", periods=n_days).date

    codes = [f"{1000 + i}0" for i in range(n_codes)]
    db.upsert_listed(
        pd.DataFrame(
            {
                "code": codes,
                "name": [f"銘柄{i}" for i in range(n_codes)],
                "market_code": "0111",
                "sector17_code": "1",
                "sector33_code": "1",
                "scale_category": "TOPIX Mid400",
            }
        )
    )

    steps = rng.normal(-(sigma**2) / 2, sigma, (n_codes, n_days))
    close = 1500 * np.exp(np.cumsum(steps, axis=1))
    open_ = close * (1 + rng.normal(0, 0.004, close.shape))
    high = np.maximum(close, open_) * (1 + np.abs(rng.normal(0, 0.007, close.shape)))
    low = np.minimum(close, open_) * (1 - np.abs(rng.normal(0, 0.007, close.shape)))
    volume = rng.integers(300_000, 3_000_000, close.shape).astype(float)

    db.upsert_quotes(
        pd.DataFrame(
            {
                "code": np.repeat(np.array(codes), n_days),
                "date": np.tile(dates, n_codes),
                "open": open_.ravel(),
                "high": high.ravel(),
                "low": low.ravel(),
                "close": close.ravel(),
                "volume": volume.ravel(),
                "turnover_value": (close * volume).ravel(),
            }
        )
    )


@pytest.fixture
def noise_cfg(cfg):
    return replace(
        cfg,
        risk=RiskConfig(equity_jpy=1_000_000, risk_per_trade=0.01, max_position_pct=0.3),
        screener=ScreenerConfig(
            strategies=["breakout", "pullback", "reversal"],
            min_turnover_jpy=0,
            min_price=1,
            max_price=1_000_000,
            min_history_days=120,
            market_codes=["0111"],
        ),
        execution=ExecutionConfig(live_trading=False, broker="paper"),
    )


def test_noise_market_is_not_profitable(noise_cfg, db):
    """ノイズから利益を作り出していないこと。

    ここが失敗したら、バックテストの数字はすべて信用できない。
    戦略を疑う前に、まず実装を疑うこと。
    """
    _driftless_market(db)
    result = run_backtest(noise_cfg, db, label="ノイズ市場", max_symbols=60)

    assert result.trades, "取引が 1 件も発生せず、検証になっていない"
    assert result.expectancy_r <= 0.02, (
        f"上がりも下がりもしない市場で期待値 {result.expectancy_r:+.2f}R が出ている。"
        " 実装が未来を覗いている疑いが濃厚（過去に流動性フィルタで発生）。"
    )


def test_symbol_selection_uses_only_information_available_at_the_start(noise_cfg, db):
    """銘柄の絞り込みに、検証期間の後半の情報を使っていないこと。

    「期間の終わりに流動性が高かった銘柄」で絞ると、売買代金＝株価×出来高
    である以上、値上がりした銘柄を選ぶことになる。
    """
    from hinotane.backtest import _build_signal_table

    n_days = 320
    dates = pd.bdate_range("2024-01-01", periods=n_days).date
    rng = np.random.default_rng(1)

    # early: 最初から最後まで一貫して流動性が高い
    # late : 前半は閑散だが、後半に急騰して流動性が上がる（＝後知恵でしか選べない）
    ramp = np.concatenate([np.ones(n_days // 2), np.linspace(1, 50, n_days - n_days // 2)])
    rows = []
    for code, factor in (("10000", np.full(n_days, 10.0)), ("20000", ramp)):
        close = 1000 * factor
        volume = np.full(n_days, 1_000_000.0)
        rows.append(
            pd.DataFrame(
                {
                    "code": code,
                    "date": dates,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": volume,
                    "turnover_value": close * volume,
                }
            )
        )
    db.upsert_listed(
        pd.DataFrame(
            {
                "code": ["10000", "20000"],
                "name": ["一貫して厚い", "後半だけ厚い"],
                "market_code": "0111",
                "sector17_code": "1",
                "sector33_code": "1",
                "scale_category": "TOPIX Mid400",
            }
        )
    )
    db.upsert_quotes(pd.concat(rows, ignore_index=True))
    assert rng  # 乱数は使わないが、意図しない非決定性を持ち込まないことの明示

    _, prices, _ = _build_signal_table(
        noise_cfg, db, ["breakout"], max_symbols=1
    )

    assert set(prices) == {"10000"}, (
        "検証期間の後半に流動性が上がった銘柄を選んでいる。"
        " 開始時点で知り得ない情報で銘柄を選別している。"
    )
