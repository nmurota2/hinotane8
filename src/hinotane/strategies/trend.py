"""順張り: 長期上昇トレンドのモメンタム追随。

診断（`hinotane diagnose`）で判明した、既存 3 戦略の敗因への回答:

| 診断された事実 | この戦略での対応 |
|---|---|
| 市場 +56% の期間に戦略 -8.5% | 長期上昇トレンドの銘柄しか買わない |
| 損切りが 54%（浅すぎてノイズで振られる） | 損切りを 3×ATR に広げ、ノイズの外に置く |
| 平均保有 13.7 日（勝ち馬を手放す） | トレーリングストップで伸ばし、最大 90 日 |
| シグナル 11,248 件から 170 件しか取れない | 相対的な強さ（モメンタム）で順位づけして選ぶ |

考え方は「強いものを買って、弱くなるまで持つ」。
上昇相場では買い持ちに近い挙動になり、崩れたらトレーリングで降りる。

エントリー条件:
  * 終値 > 200日線（長期上昇トレンド）
  * 50日線 > 200日線（トレンドの向きが揃っている）
  * 200日線が上向き（20日前と比較）
  * 50日高値を更新（動き出している）
  * 直近 1 日で行き過ぎていない（+8% 未満）

損切り  : エントリー価格 - 3.0×ATR、以後はトレーリングで切り上げのみ
利確目標: 置かない（トレーリングに任せる）。表示上は 10R を入れておく
順位づけ: 12-1 モメンタム（半年の上昇率から直近 1 か月を除いたもの）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Strategy, register


@register
class TrendStrategy(Strategy):
    name = "trend"
    label = "長期上昇トレンド追随"
    max_holding_days = 90
    warmup_bars = 220          # 200日線の計算に必要

    atr_stop_mult = 3.0        # ノイズの外に置く
    trailing_atr_mult = 3.0    # 同じ幅で切り上げていく
    has_profit_target = False  # 利確目標は置かない。通知にも出さない
    max_daily_jump = 0.08

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = self._blank(df)
        close = out["close"]

        # 長期上昇トレンドにあるか
        cond_above_200 = close > out["sma200"]
        cond_aligned = out["sma50"] > out["sma200"]
        cond_rising = out["sma200"] > out["sma200"].shift(20)

        # 動き出しているか（当日を含めない 50 日高値を上抜け）
        cond_breakout = close > out["high50"].shift(1)

        # 一日で行き過ぎた日は高値掴みになりやすい
        cond_not_overextended = (close / close.shift(1) - 1.0) < self.max_daily_jump

        cond_atr = out["atr14"].notna() & (out["atr14"] > 0)
        cond_mom = out["mom120"].notna()

        entry = (
            cond_above_200
            & cond_aligned
            & cond_rising
            & cond_breakout
            & cond_not_overextended
            & cond_atr
            & cond_mom
        ).fillna(False)

        stop = close - self.atr_stop_mult * out["atr14"]
        # 利確目標は置かない。トレーリングで降りるまで持つ。
        # 表示と R 計算のために遠い値を入れておく。
        target = close + 10.0 * (close - stop)

        # 相対的な強さで順位づけする。1 日に 30 件以上シグナルが出て
        # 3 件しか取れないので、「何を選ぶか」が成績をほぼ決める。
        score = out["mom120"].fillna(-1.0) * 100

        reason = pd.Series("", index=out.index, dtype=object)
        reason = reason.where(
            ~entry,
            "200日線の上で長期上昇トレンド|50日線＞200日線|50日高値を更新|半年騰落率"
            + (out["mom120"] * 100).round(1).astype(str)
            + "%|損切りは3×ATRでトレーリング",
        )

        out["entry"] = entry
        out["stop_price"] = np.where(entry, stop, np.nan)
        out["target_price"] = np.where(entry, target, np.nan)
        out["score"] = score.fillna(0.0)
        out["reason"] = reason
        return out
