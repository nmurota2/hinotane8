"""テスト用の合成データ生成。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def synthetic_bars(code: str, n: int = 300, seed: int = 0, trend: float = 0.0015) -> pd.DataFrame:
    """テスト用の合成日足。``trend`` を上げると上昇トレンドになる。"""
    rng = np.random.default_rng(seed)
    steps = rng.normal(trend, 0.015, n)
    close = 1500 * np.exp(np.cumsum(steps))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(close, open_) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(close, open_) * (1 - np.abs(rng.normal(0, 0.006, n)))
    volume = rng.integers(500_000, 3_000_000, n).astype(float)
    return pd.DataFrame(
        {
            "code": code,
            "date": pd.bdate_range("2024-01-01", periods=n).date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "turnover_value": close * volume,
        }
    )
