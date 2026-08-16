"""「同じ日に出た候補の中から、どれを選ぶべきか」を直接測る。

バックテストでこれを測るのは筋が悪い。枠が 1 日 3 件・同時保有 5 銘柄なので、
3 年半で 172 件しか取引が発生しない。172 件では、多少の優位性があっても
ノイズに埋もれて検出できない（実際、期待値の 99% 信頼区間は
[-0.23, +0.30] と、ほぼ何も言えない幅だった）。

一方、シグナル自体は 15,671 件ある。**「枠に入れて売買する」のをやめて、
「候補の順位と、その後の値動きの順位が、どれくらい一致するか」だけを測れば、
全部のシグナルを使える。** 標本が 100 倍近くになるので、
小さな優位性でも検出できる。

これは情報係数（Information Coefficient）と呼ばれる、
ファクター運用の標準的な測り方。手順は単純:

  1. 各日について、その日の候補それぞれの指標値を並べる
  2. 同じ候補の「その後 N 日の値上がり率」を並べる
  3. 2 つの順位がどれくらい一致するか（順位相関）を計算する
  4. それを全日ぶん平均する

平均がプラスなら「その指標で上から選ぶと、平均的に良い銘柄が取れる」。
ゼロなら「その指標には情報が無い」。

⚠️ 限界を承知して読むこと:
  * 保有期間が重なるので、日ごとの相関は互いに独立でない。
    t 値は素朴に計算すると大きく出すぎる。保有日数で割った補正値も併記する。
  * 指標を複数試すぶんの多重検定がある。ボンフェローニ補正した基準を出す。
  * ここで良く見えた指標が、売買コストと枠の制約を通しても残るとは限らない。
    次の段階として必ずバックテストにかけること。
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from .config import AppConfig
from .db import Database
from .indicators import enrich
from .strategies.base import get_strategy

#: 試す指標。**「上から選ぶ」向きに符号を揃えてある**（大きいほど良い候補）。
#: 増やすたびに多重検定の代償が増えるので、機構の説明がつくものだけ入れる。
FACTORS: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "12-1モメンタム（いま使用中）": lambda d: d["mom120"],
    "3か月モメンタム": lambda d: d["mom60"],
    "1か月モメンタム": lambda d: d["mom20"],
    "低ボラティリティ": lambda d: -d["atr_pct"],
    "200日線に近い（伸びきっていない）": lambda d: -(d["close"] / d["sma200"] - 1.0),
    "出来高の増加": lambda d: d["vol_ratio"],
    "売買代金が大きい": lambda d: d["turnover_ma20"],
    "RSIが低い（短期の押し目）": lambda d: -d["rsi14"],
}

#: 検定の基準となる最低候補数。2〜3 件では順位相関がほとんど情報にならない。
MIN_CANDIDATES = 5


def _forward_return(df: pd.DataFrame, horizon: int) -> pd.Series:
    """翌営業日の始値で買い、horizon 営業日後の始値で売った場合の騰落率。

    実際の売買と同じ「翌寄り」を使う。終値基準にすると、その日の終値を見て
    その日に買えることになり、測っている優位性が実在しないものになる。
    """
    entry = df["open"].shift(-1)
    exit_ = df["open"].shift(-(1 + horizon))
    return exit_ / entry - 1.0


def _collect(
    cfg: AppConfig, db: Database, *, max_symbols: int, horizons: tuple[int, ...], end
) -> pd.DataFrame:
    """全銘柄のシグナル発生日について、指標値とその後の騰落率を集める。"""
    sc = cfg.screener
    universe = db.universe(sc.market_codes, sc.min_history_days)
    if universe.empty:
        return pd.DataFrame()

    codes = universe["code"].tolist()
    bars_by_code = db.bars_bulk(codes, limit_days=100_000)
    liquidity = {
        c: float(df["turnover_value"].head(60).mean() or 0) for c, df in bars_by_code.items()
    }
    codes = sorted(liquidity, key=lambda c: liquidity[c], reverse=True)[:max_symbols]

    strategy = get_strategy("trend")
    rows: list[pd.DataFrame] = []
    for code in codes:
        bars = bars_by_code.get(code)
        if bars is None or len(bars) < sc.min_history_days:
            continue
        d = enrich(bars)
        out = strategy.evaluate(d)
        tradable = (d["turnover_ma20"] >= sc.min_turnover_jpy) & d["close"].between(
            sc.min_price, sc.max_price
        )
        hit = out["entry"] & tradable
        if not hit.any():
            continue

        piece = {"date": d["date"], "code": code}
        for name, fn in FACTORS.items():
            piece[name] = fn(d)
        for h in horizons:
            piece[f"fwd{h}"] = _forward_return(d, h)
        frame = pd.DataFrame(piece)[hit.to_numpy()]
        if end is not None:
            frame = frame[frame["date"] <= end]
        rows.append(frame)

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _ic_series(panel: pd.DataFrame, factor: str, fwd: str) -> pd.Series:
    """日ごとの順位相関（スピアマン）。候補が少ない日は捨てる。

    ``method="spearman"`` は scipy を必要とする。数式としては
    **順位に変換してからのピアソン相関** と同じなので、自分で順位にしてから
    計算する。依存を 1 つ増やすと、そのぶん環境構築で詰まる箇所が増える。
    """
    values: dict = {}
    for day, g in panel.groupby("date"):
        sub = g[[factor, fwd]].dropna()
        if len(sub) < MIN_CANDIDATES:
            continue
        if sub[factor].nunique() < 2 or sub[fwd].nunique() < 2:
            continue
        values[day] = sub[factor].rank().corr(sub[fwd].rank())
    return pd.Series(values).dropna()


def run_factor_scan(
    cfg: AppConfig,
    db: Database,
    *,
    max_symbols: int = 600,
    split: float = 0.6,
    horizons: tuple[int, ...] = (20, 60, 90),
) -> str:
    out: list[str] = []

    # 後半は封印したまま。ここでも触らない。
    dates = db.query("SELECT DISTINCT date FROM daily_quotes ORDER BY date")["date"].tolist()
    if len(dates) < 300:
        return "日足が足りません。先に `hinotane backfill` を実行してください。"
    warmup = get_strategy("trend").warmup_bars
    tradable = dates[warmup:]
    boundary = tradable[min(max(int(len(tradable) * split), 1), len(tradable) - 1)]

    panel = _collect(cfg, db, max_symbols=max_symbols, horizons=horizons, end=boundary)
    if panel.empty:
        return "シグナルが 1 件も生成されませんでした。"

    n_tests = len(FACTORS) * len(horizons)

    out.append("=" * 78)
    out.append("候補の順位づけに、そもそも情報があるか")
    out.append("=" * 78)
    out.append(f"  対象期間 : {panel['date'].min()} 〜 {boundary}（前半のみ・後半は封印中）")
    out.append(f"  シグナル : {len(panel):,} 件 / {panel['date'].nunique():,} 日")
    out.append(f"  検定回数 : {n_tests} 回（指標 {len(FACTORS)} 種 × 保有期間 {len(horizons)} 通り）")
    out.append("")
    out.append("  バックテストでは 172 取引しか発生せず、期待値の信頼区間は")
    out.append("  [-0.23, +0.30] とほぼ何も言えない幅でした。ここでは枠の制約を外し、")
    out.append("  シグナル全件を使って「順位に情報があるか」だけを測ります。")
    out.append("")
    out.append("  読み方: 情報係数(IC)は「候補の順位」と「その後の値動きの順位」の一致度。")
    out.append("          +0.03〜0.05 あれば実務的には十分とされます。ゼロなら情報なし。")

    out.append("")
    out.append("=" * 78)
    out.append("結果")
    out.append("=" * 78)
    header = f"  {'指標':<34}" + "".join(f"{'+' + str(h) + '日':>16}" for h in horizons)
    out.append(header)
    out.append("  " + "-" * (34 + 16 * len(horizons)))

    # ボンフェローニ補正した |t| の目安（両側 5%）
    from math import sqrt

    t_threshold = 1.96 + 0.5 * np.log(max(n_tests, 1))  # 近似。表示用の目安

    findings: list[tuple[str, int, float, float]] = []
    usable_days: dict[int, int] = {}
    for factor in FACTORS:
        cells = []
        for h in horizons:
            ic = _ic_series(panel, factor, f"fwd{h}")
            usable_days[h] = max(usable_days.get(h, 0), len(ic))
            if len(ic) < 20:
                cells.append(f"{'—':>16}")
                continue
            mean, sd = float(ic.mean()), float(ic.std(ddof=1))
            # 保有期間が重なるぶん、日ごとの IC は独立でない。
            # 実効的な標本数を「日数 ÷ 保有日数」まで割り引く（保守側）。
            n_eff = max(len(ic) / h, 2.0)
            t = mean / sd * sqrt(n_eff) if sd > 0 else 0.0
            cells.append(f"{mean:>+9.3f}(t{t:>+5.1f})")
            findings.append((factor, h, mean, t))
        out.append(f"  {factor:<34}" + "".join(cells))

    out.append("  " + "-" * (34 + 16 * len(horizons)))
    out.append(
        "  ※ 相関を計算できた日数: "
        + " / ".join(f"+{h}日 {usable_days.get(h, 0):,}日" for h in horizons)
        + f"（候補が {MIN_CANDIDATES} 件以上あった日のみ）"
    )
    out.append(f"  ※ t は保有期間の重なりを割り引いた値。|t| が {t_threshold:.1f} 以上で")
    out.append(f"     ようやく「{n_tests} 回検定したうちの偶然」とは言いにくくなります。")

    out.append("")
    out.append("=" * 78)
    out.append("総括")
    out.append("=" * 78)
    strong = [f for f in findings if abs(f[3]) >= t_threshold and f[2] > 0]
    weak = [f for f in findings if f[2] > 0.02 and abs(f[3]) < t_threshold]

    if strong:
        out.append("  基準を超えた指標:")
        for name, h, mean, t in sorted(strong, key=lambda x: -x[2]):
            out.append(f"    ・{name}（+{h}日）  IC {mean:+.3f} / t {t:+.1f}")
        out.append("")
        out.append("  → この指標で順位づけし直した戦略を作り、**前半だけで**")
        out.append("     バックテストしてください。そこも通れば、最後に封印中の後半で 1 回だけ検証します。")
    elif weak:
        out.append("  基準は超えませんでしたが、方向がプラスで大きめのもの:")
        for name, h, mean, t in sorted(weak, key=lambda x: -x[2])[:5]:
            out.append(f"    ・{name}（+{h}日）  IC {mean:+.3f} / t {t:+.1f}")
        out.append("")
        out.append("  → 弱い手がかりです。これを追うかどうかは、外れたときに失う時間と")
        out.append("     天秤にかけて決めてください。統計的には「無い」と区別できていません。")
    else:
        out.append("  **どの指標にも、候補を選ぶための情報がありませんでした。**")
        out.append("")
        out.append("  シグナル全件・数千の観測を使っても検出できないなら、")
        out.append("  枠が 1 日 3 件しかないバックテストで検出できるはずがありません。")
        out.append("  日足のテクニカル指標で銘柄を選び分けるのは、この期間・この銘柄群では")
        out.append("  機能しないと結論するのが妥当です。")
        out.append("")
        out.append("  次に試す価値があるのは、値動き **以外** のデータです。")
        out.append("  J-Quants の Light プランには財務情報（サマリー）が含まれています。")

    return "\n".join(out)
