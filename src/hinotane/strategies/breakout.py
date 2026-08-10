"""順張り: 20 日高値ブレイクアウト。

考え方: 上昇トレンドにある銘柄が直近 20 日の高値を、出来高を伴って上抜けた日を買う。
古典的なドンチアン・ブレイクアウトの日本株向け調整版。

損切り: エントリー価格 - 2×ATR（ボラティリティ基準）
利確  : リスク幅の 2.5 倍（リワード・リスク比 2.5）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy, register


@register
class BreakoutStrategy(Strategy):
    name = "breakout"
    label = "20日高値ブレイク"
    max_holding_days = 20
    warmup_bars = 80

    atr_stop_mult = 2.0
    reward_risk = 2.5
    min_volume_ratio = 1.5      # 出来高が 20 日平均の 1.5 倍以上
    min_mom60 = 0.0             # 直近 60 日でプラス（下降トレンドでは買わない）

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._blank(df)

        prev_high20 = out["high20"].shift(1)   # 当日を含めない直近 20 日高値
        close = out["close"]

        cond_break = close > prev_high20
        cond_trend = (out["sma25"] > out["sma75"]) & (close > out["sma25"])
        cond_volume = out["vol_ratio"] >= self.min_volume_ratio
        cond_mom = out["mom60"] >= self.min_mom60
        # 一日で行き過ぎた（＋10%以上）日は高値掴みになりやすいので見送る
        cond_not_overextended = (close / close.shift(1) - 1.0) < 0.10
        cond_atr = out["atr14"].notna() & (out["atr14"] > 0)

        entry = (
            cond_break & cond_trend & cond_volume & cond_mom
            & cond_not_overextended & cond_atr
        ).fillna(False)

        stop = close - self.atr_stop_mult * out["atr14"]
        target = close + self.reward_risk * (close - stop)

        # スコア: モメンタムと出来高の強さ。ボラが高すぎる銘柄は割り引く。
        score = (
            out["mom60"].clip(-1, 3) * 100
            + (out["vol_ratio"].clip(0, 5) - 1) * 10
            - out["atr_pct"].fillna(0) * 200
        )

        reason = pd.Series("", index=out.index, dtype=object)
        reason = reason.where(
            ~entry,
            "20日高値を上抜け|出来高"
            + (out["vol_ratio"].round(1)).astype(str)
            + "倍|25日線＞75日線の上昇トレンド|60日騰落率"
            + (out["mom60"] * 100).round(1).astype(str)
            + "%",
        )

        out["entry"] = entry
        out["stop_price"] = np.where(entry, stop, np.nan)
        out["target_price"] = np.where(entry, target, np.nan)
        out["score"] = score.fillna(0.0)
        out["reason"] = reason
        return out
