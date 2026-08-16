"""テクニカル指標。外部ライブラリ（TA-Lib 等）に依存せず pandas だけで計算する。

TA-Lib は C ライブラリのビルドが必要で、Docker イメージが太るうえに
環境構築で詰まりやすい。ここで使う程度の指標は自前で十分。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder の RSI。"""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # 下落がゼロ＝一本調子の上昇は RSI 100 とみなす
    return out.fillna(100.0).where(avg_gain.notna(), np.nan)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Average True Range。損切り幅の決定に使う。

    「何円下がったら損切りか」を固定値ではなくボラティリティ基準で決めることで、
    値動きの荒い銘柄と穏やかな銘柄を同じ土俵でリスク比較できる。
    """
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def rolling_max(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).max()


def rolling_min(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).min()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """日足に、全戦略が共通で使う指標列を足して返す。

    引数の DataFrame は date 昇順で open/high/low/close/volume/turnover_value を持つこと。
    """
    out = df.copy()
    close, high, low = out["close"], out["high"], out["low"]

    out["sma5"] = sma(close, 5)
    out["sma25"] = sma(close, 25)
    out["sma75"] = sma(close, 75)
    out["sma50"] = sma(close, 50)
    out["sma200"] = sma(close, 200)
    out["ema20"] = ema(close, 20)
    out["rsi14"] = rsi(close, 14)
    out["atr14"] = atr(high, low, close, 14)
    out["high20"] = rolling_max(high, 20)
    out["high60"] = rolling_max(high, 60)
    out["low20"] = rolling_min(low, 20)
    out["vol_ma20"] = sma(out["volume"], 20)
    out["turnover_ma20"] = sma(out["turnover_value"], 20)

    # ボラティリティを株価に対する比率で持っておくと銘柄間で比較できる
    out["atr_pct"] = out["atr14"] / close
    out["vol_ratio"] = out["volume"] / out["vol_ma20"]
    # 直近 60 営業日の騰落率（モメンタム）
    out["mom60"] = close / close.shift(60) - 1.0
    out["mom20"] = close / close.shift(20) - 1.0
    # 相対的な強さの尺度。直近 1 か月を除いた騰落率。
    # 直近は短期反転が効きやすいので外すのが定石。
    #
    # ⚠️ 名前と中身に注意。以前ここを「12-1 モメンタム」と書いていたが、
    # 140 営業日は約 6.7 か月なので実際は **7-1 モメンタム**だった。
    # 文献で機能が確認されているのは 12-1（約 252 営業日）のほうなので、
    # 両方を持って比較できるようにしてある。
    out["mom120"] = close.shift(20) / close.shift(140) - 1.0   # 7-1
    out["mom250"] = close.shift(21) / close.shift(252) - 1.0   # 12-1（文献の定義）
    out["high120"] = rolling_max(high, 120)
    out["high50"] = rolling_max(high, 50)

    return out
