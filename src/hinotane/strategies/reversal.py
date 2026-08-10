"""逆張り: 売られすぎからの反発狙い。

考え方: 中長期の上昇基調は崩れていない（終値 > 75日線 × 0.92）銘柄が、
短期的に RSI 30 以下まで叩き売られ、当日に下ヒゲ陽線で下げ止まった日を拾う。

逆張りは「落ちるナイフ」を掴むリスクがあるため、
 - 長期トレンドが完全に崩れた銘柄は除外
 - 損切りは当日安値の直下と、ATR の小さいほう
という二重の歯止めを入れている。

損切り: min(当日安値 × 0.99, エントリー - 1.5×ATR)
利確  : リスク幅の 1.8 倍（逆張りは利確を早めに）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy, register


@register
class ReversalStrategy(Strategy):
    name = "reversal"
    label = "売られすぎ反発"
    max_holding_days = 10
    warmup_bars = 100

    atr_stop_mult = 1.5
    reward_risk = 1.8
    rsi_threshold = 30.0

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._blank(df)
        close, low, open_ = out["close"], out["low"], out["open"]

        # 長期トレンドが完全崩壊していない
        cond_not_broken = close > out["sma75"] * 0.92
        # 売られすぎ
        cond_oversold = out["rsi14"] <= self.rsi_threshold
        # 当日は陽線で下げ止まり
        cond_bull_bar = close > open_
        # 下ヒゲが実体より長い＝押し目を買われた形
        body = (close - open_).abs()
        lower_wick = pd.concat([close, open_], axis=1).min(axis=1) - low
        cond_wick = lower_wick > body * 0.5
        # 出来高が伴っている＝セリングクライマックスの可能性
        cond_volume = out["vol_ratio"] >= 1.2
        cond_atr = out["atr14"].notna() & (out["atr14"] > 0)

        entry = (
            cond_not_broken & cond_oversold & cond_bull_bar
            & cond_wick & cond_volume & cond_atr
        ).fillna(False)

        atr_stop = close - self.atr_stop_mult * out["atr14"]
        day_low_stop = low * 0.99
        stop = pd.concat([atr_stop, day_low_stop], axis=1).min(axis=1)
        target = close + self.reward_risk * (close - stop)

        # スコア: RSI が低いほど、下ヒゲが長いほど良い
        score = (
            (self.rsi_threshold - out["rsi14"]).clip(lower=0).fillna(0) * 3
            + (lower_wick / close.replace(0, np.nan)).fillna(0) * 500
            + (out["vol_ratio"].clip(0, 5) - 1) * 5
        )

        reason = pd.Series("", index=out.index, dtype=object)
        reason = reason.where(
            ~entry,
            "RSI"
            + out["rsi14"].round(0).astype("Int64").astype(str)
            + "の売られすぎ|下ヒゲ陽線で反発|出来高"
            + out["vol_ratio"].round(1).astype(str)
            + "倍|75日線から-8%以内で下げ止まり",
        )

        out["entry"] = entry
        out["stop_price"] = np.where(entry, stop, np.nan)
        out["target_price"] = np.where(entry, target, np.nan)
        out["score"] = score.fillna(0.0)
        out["reason"] = reason
        return out
