"""検証用の対照群（コントロール）。実運用には使わない。

`trend` は「長期上昇トレンドの銘柄を、モメンタムの強い順に買う」戦略だが、
5 年・256 取引で優位性が確認できなかった。ここで問題になるのは
**どこが効いていないのか** が分からないこと。候補は少なくとも 3 つある:

  1. エントリー条件（そもそも買う場面が悪い）
  2. 順位づけ（1 日 16 件の候補から 3 件選ぶ基準が意味を成していない）
  3. 決済（損切りが上げ相場で不利に働いている）

ひとつずつ潰すには、**1 か所だけ変えた対照群** と比べるしかない。
本家と対照群の差が、その 1 か所の寄与そのものになる。

ここに置くクラスはすべて `trend` を継承し、変更点をひとつだけ持つ。
名前は `研究:` で始めて、本番のスクリーニングに紛れ込んでも
人間が気づけるようにしてある。
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

from .base import register
from .trend import TrendStrategy


def _stable_random(codes: pd.Series, dates: pd.Series) -> pd.Series:
    """銘柄コードと日付から決まる、再現可能な疑似乱数（0〜1）。

    `numpy.random` を使うと、実行順や銘柄数が変わるたびに結果が動いて
    比較にならない。ハッシュから作れば、いつどの順で計算しても同じ値になる。
    """
    keys = codes.astype(str) + "|" + pd.to_datetime(dates).dt.strftime("%Y%m%d")
    return keys.map(
        lambda k: int(hashlib.blake2b(k.encode(), digest_size=8).hexdigest(), 16)
        / float(1 << 64)
    )


@register
class TrendRandomPickStrategy(TrendStrategy):
    """順位づけだけを乱数にした対照群。

    **この検証でいちばん重要な実験。**
    独立した機会 7,837 件のうち取れるのは 256 件（3.3%）しかないので、
    成績のほとんどは「何を選ぶか」で決まる。

    本家と成績が変わらないなら、12-1 モメンタムによる順位づけは
    **何もしていない**ということ。エントリー条件を磨いても無駄で、
    選び方から作り直す必要がある。

    逆に本家がはっきり勝つなら、順位づけには効果がある。
    そのときは順位づけを強化する方向に投資すればよい。
    """

    name = "研究:順位乱数"
    label = "trend と同条件・選ぶ順だけ乱数（対照群）"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().evaluate(df)
        code = out["code"] if "code" in out.columns else pd.Series("?", index=out.index)
        out["score"] = _stable_random(code, out["date"])
        return out


@register
class TrendNoStopStrategy(TrendStrategy):
    """損切りを外した対照群（時間切れだけで降りる）。

    上げ相場で 62.1% が買値より下で切られ、-821,143 円を失っている。
    「損切りがこの戦略の足を引っ張っている」なら、外したほうが成績は上がる。

    ⚠️ 損切りを完全に外すと 1 取引の損失が青天井になるので、実運用には使えない。
    これは **損切りの寄与を測るためだけ** の設定。

    実装の注意: 損切り価格を遠くに置く（例 20×ATR）やり方は使えない。
    数量は「損切り幅で 1% のリスク」から逆算するので、幅が巨大になると
    株数が単元に届かず **1 件も建たなくなる**（最初にそう書いて実際に 0 件になった）。
    損切り価格は本家と同じ 3×ATR のまま置き、決済の判定だけを止める。
    こうすれば数量も R 倍率も本家と同じ土俵で比較できる。
    """

    name = "研究:損切りなし"
    label = "trend から損切りを外した（対照群・実運用不可）"
    use_stop_exit = False
    trailing_atr_mult = None


@register
class TrendFixedStopStrategy(TrendStrategy):
    """トレーリングを外し、損切りを固定した対照群。

    本家との差が、トレーリング（損切りの切り上げ）の寄与そのもの。
    """

    name = "研究:トレーリングなし"
    label = "trend の損切りを固定（対照群）"
    trailing_atr_mult = None


@register
class TrendRegimeFilterStrategy(TrendStrategy):
    """相場全体が崩れているときは買わない。

    前半 3.3 年（市場 +31.1%）で優位性ゼロ、後半 1.7 年（市場 +62.5%）でプラス。
    「相場が強いときだけ機能する」なら、地合いフィルタで前半の無駄打ちが減る。

    市場指数（検証対象を等ウェイト）が 200 日線より上の日だけエントリーする。
    指数は `backtest._market_regime()` が t 日までの終値だけで作るので、
    先読みにはならない。
    """

    name = "研究:地合いフィルタ"
    label = "trend ＋ 市場が200日線より上のときだけ買う"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().evaluate(df)
        if "market_above_ma" not in out.columns:
            # 市場指数が作れていない場合は、黙って素通りさせない。
            # 「フィルタが効いた結果」と「フィルタが無かった結果」は別物なので、
            # 取り違えると実験の意味が消える。
            out["entry"] = False
            out["reason"] = "市場指数が計算できずフィルタを適用できません"
            return out
        blocked = ~out["market_above_ma"].astype(bool)
        out.loc[blocked, "entry"] = False
        out.loc[blocked, "stop_price"] = np.nan
        out.loc[blocked, "target_price"] = np.nan
        return out


@register
class TrendLooseEntryStrategy(TrendStrategy):
    """エントリー条件から「50日高値の更新」を外した対照群。

    本家は「長期上昇トレンド」かつ「50日高値を更新」を要求する。
    高値更新は、上げ相場では高値掴みになりやすい。
    外すと候補が大幅に増えるので、順位づけの効き方も変わる。

    本家との差は「高値更新を待つこと」の寄与。
    """

    name = "研究:高値更新なし"
    label = "trend から50日高値の更新条件を外した（対照群）"

    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().evaluate(df)
        close = out["close"]
        entry = (
            (close > out["sma200"])
            & (out["sma50"] > out["sma200"])
            & (out["sma200"] > out["sma200"].shift(20))
            & ((close / close.shift(1) - 1.0) < self.max_daily_jump)
            & out["atr14"].notna()
            & (out["atr14"] > 0)
            & out["mom120"].notna()
        ).fillna(False)
        stop = close - self.atr_stop_mult * out["atr14"]
        out["entry"] = entry
        out["stop_price"] = np.where(entry, stop, np.nan)
        out["target_price"] = np.where(entry, close + 10.0 * (close - stop), np.nan)
        return out
