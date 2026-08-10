"""順張り: 上昇トレンド中の押し目買い。

考え方: 長期は明確な上昇トレンド（終値 > 75日線、25日線が上向き）だが、
短期的に 25 日線付近まで押して RSI が中立以下に落ちたところを拾う。
ブレイクアウトより高値掴みになりにくく、損切り幅も小さく取れる。

損切り: min(直近20日安値, エントリー - 1.5×ATR) の少し下
利確  : リスク幅の 2.0 倍
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy, register


@register
class PullbackStrategy(Strategy):
    name = "pullback"
    label = "上昇トレンドの押し目"
    max_holding_days = 15
    warmup_bars = 100

    atr_stop_mult = 1.5
    reward_risk = 2.0
    rsi_low = 35.0
    rsi_high = 55.0

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._blank(df)
        close = out["close"]

        # 長期上昇トレンド
        cond_trend = (close > out["sma75"]) & (out["sma25"] > out["sma75"])
        # 25 日線が上向き（5 日前より上）
        cond_slope = out["sma25"] > out["sma25"].shift(5)
        # 25 日線まで押している（終値が 25 日線の ±4% 以内）
        dist = (close - out["sma25"]) / out["sma25"]
        cond_near_ma = dist.between(-0.04, 0.04)
        # RSI が中立以下に落ちている＝短期的に売られた
        cond_rsi = out["rsi14"].between(self.rsi_low, self.rsi_high)
        # 当日が陽線＝下げ止まりの兆し
        cond_bull_bar = close > out["open"]
        cond_atr = out["atr14"].notna() & (out["atr14"] > 0)

        entry = (
            cond_trend & cond_slope & cond_near_ma & cond_rsi & cond_bull_bar & cond_atr
        ).fillna(False)

        atr_stop = close - self.atr_stop_mult * out["atr14"]
        swing_stop = out["low20"] * 0.995      # 直近安値をわずかに割ったら撤退
        stop = pd.concat([atr_stop, swing_stop], axis=1).min(axis=1)
        target = close + self.reward_risk * (close - stop)

        # スコア: 長期モメンタムが強く、押しが浅いほど良い
        score = (
            out["mom60"].clip(-1, 3) * 80
            - dist.abs().fillna(0) * 300
            + (60 - out["rsi14"]).clip(lower=0).fillna(0)
        )

        reason = pd.Series("", index=out.index, dtype=object)
        reason = reason.where(
            ~entry,
            "75日線上の上昇トレンド|25日線まで押し目("
            + (dist * 100).round(1).astype(str)
            + "%)|RSI"
            + out["rsi14"].round(0).astype("Int64").astype(str)
            + "|陽線で反発",
        )

        out["entry"] = entry
        out["stop_price"] = np.where(entry, stop, np.nan)
        out["target_price"] = np.where(entry, target, np.nan)
        out["score"] = score.fillna(0.0)
        out["reason"] = reason
        return out
