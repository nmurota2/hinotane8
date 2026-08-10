import numpy as np
import pandas as pd
import pytest

from hinotane.indicators import atr, enrich, rsi, sma


def _bars(n: int = 200, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 1000 + np.cumsum(rng.normal(0, 10, n))
    close = np.maximum(close, 100)
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    open_ = close + rng.normal(0, 5, n)
    volume = rng.integers(100_000, 1_000_000, n).astype(float)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=n).date,
            "open": open_,
            "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]),
            "close": close,
            "volume": volume,
            "turnover_value": close * volume,
        }
    )


def test_sma_matches_manual_mean():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert sma(s, 3).iloc[-1] == pytest.approx(4.0)
    # window 未満は NaN
    assert pd.isna(sma(s, 3).iloc[1])


def test_rsi_bounded_0_100():
    values = rsi(_bars()["close"]).dropna()
    assert not values.empty
    assert values.between(0, 100).all()


def test_rsi_all_up_is_100():
    s = pd.Series(np.arange(1, 60, dtype=float))
    assert rsi(s).iloc[-1] == pytest.approx(100.0)


def test_atr_is_positive():
    df = _bars()
    values = atr(df["high"], df["low"], df["close"]).dropna()
    assert (values > 0).all()


def test_enrich_adds_expected_columns():
    out = enrich(_bars())
    for col in ("sma25", "sma75", "rsi14", "atr14", "high20", "vol_ratio", "mom60", "atr_pct"):
        assert col in out.columns
    # 行数は変わらない
    assert len(out) == 200
