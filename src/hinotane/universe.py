"""検証対象から何を落としているのかを、数字で出す。

## なぜ要るのか

「日足のテクニカルでは勝てない」と結論したが、**それは検証した範囲での話**。
実際に見ていたのは 4,487 銘柄のうち 600 銘柄だけで、しかも次の 3 段階で
絞り込んでいた:

  1. 市場区分（SCREENER_MARKET_CODES）  既定はプライム＋スタンダード
     → **グロース市場は最初から入っていない**
  2. 売買代金の下限（SCREENER_MIN_TURNOVER_JPY）  既定 1 億円
  3. 流動性上位 N 銘柄（--max-symbols）  既定 600

どこで何件落ちているかを見ないまま「勝てない」と言うのは範囲の誤認になる。

## 生存者バイアスについて

`daily_quotes` は「その日に取引があった全銘柄」を日付単位で取り込んでいる。
一方 `listed` は取得時点の上場銘柄一覧なので、**途中で上場廃止になった
銘柄は listed に無い**。検証は listed と JOIN しているため、
上場廃止銘柄は黙って対象外になっている。

これは成績を実態より良く見せる方向に効く。特に小型株・新興株では
上場廃止の頻度が高いので、影響は大型株よりずっと大きい。
その差がどれくらいかを、ここで件数として出す。

なお `/equities/master` は日付を指定できるので、過去時点の上場一覧を
取り直せば原理的には直せる。直す前に、まず影響の大きさを測る。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import AppConfig
from .db import Database

MARKET_NAMES = {
    "0101": "東証一部（旧）",
    "0102": "東証二部（旧）",
    "0104": "マザーズ（旧）",
    "0105": "TOKYO PRO MARKET",
    "0106": "JASDAQ スタンダード（旧）",
    "0107": "JASDAQ グロース（旧）",
    "0109": "その他",
    "0111": "プライム",
    "0112": "スタンダード",
    "0113": "グロース",
}


def report(cfg: AppConfig, db: Database, *, max_symbols: int = 600) -> str:
    db.init_schema()
    out: list[str] = []
    sc = cfg.risk
    scr = cfg.screener

    out.append("=" * 74)
    out.append("検証対象から何を落としているか")
    out.append("=" * 74)

    # ---------------------------------------------------- 生存者バイアス
    quoted = db.query("SELECT count(DISTINCT code) AS n FROM daily_quotes").iloc[0]["n"]
    listed = db.query("SELECT count(*) AS n FROM listed").iloc[0]["n"]
    missing = db.query(
        """
        SELECT count(DISTINCT q.code) AS n
        FROM daily_quotes q
        LEFT JOIN listed l ON l.code = q.code
        WHERE l.code IS NULL
        """
    ).iloc[0]["n"]

    out.append("")
    out.append("【1】生存者バイアス（上場廃止銘柄の扱い）")
    out.append(f"  株価データがある銘柄   : {int(quoted):,}")
    out.append(f"  上場銘柄一覧にある銘柄 : {int(listed):,}")
    out.append(f"  一覧に無い銘柄         : {int(missing):,}")
    if missing:
        out.append("")
        out.append(f"  → ⚠️ {int(missing):,} 銘柄が検証対象から黙って外れています。")
        out.append("     上場廃止・統合・改称などで現在の一覧に載っていない銘柄です。")
        out.append("     これらは「消えた」銘柄なので、除くと成績は実態より良く出ます。")
        out.append("     小型株・新興株ほど影響が大きくなります。")
    else:
        out.append("  → 一覧に無い銘柄はありません。")

    # ---------------------------------------------------- 市場区分ごと
    per_market = db.query(
        """
        SELECT l.market_code, count(*) AS n
        FROM listed l
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    out.append("")
    out.append("【2】市場区分ごとの銘柄数と、いま検証対象にしているか")
    out.append(f"  設定 SCREENER_MARKET_CODES = {','.join(scr.market_codes)}")
    out.append("")
    out.append(f"  {'市場':<22} {'銘柄数':>7}   検証対象")
    out.append("  " + "-" * 46)
    for _, r in per_market.iterrows():
        code = str(r["market_code"])
        name = MARKET_NAMES.get(code, code)
        mark = "✅ 含む" if code in scr.market_codes else "❌ 除外"
        out.append(f"  {name:<22} {int(r['n']):>7,}   {mark}")

    excluded = per_market[~per_market["market_code"].isin(scr.market_codes)]["n"].sum()
    if excluded:
        out.append("")
        out.append(f"  → 市場区分だけで {int(excluded):,} 銘柄を除外しています。")

    # ---------------------------------------------------- 絞り込みの段階
    latest = db.latest_quote_date()
    snap = db.query(
        """
        SELECT q.code, l.market_code, q.close, q.turnover_value
        FROM daily_quotes q
        JOIN listed l ON l.code = q.code
        WHERE q.date = ?
        """,
        [latest],
    )
    out.append("")
    out.append(f"【3】絞り込みの各段階で何件残るか（{pd.Timestamp(latest).date()} 時点）")
    if snap.empty:
        out.append("  データがありません。")
        return "\n".join(out)

    steps = [("全上場銘柄", snap)]
    s1 = snap[snap["market_code"].isin(scr.market_codes)]
    steps.append((f"市場区分で絞る（{','.join(scr.market_codes)}）", s1))
    s2 = s1[s1["turnover_value"] >= scr.min_turnover_jpy]
    steps.append((f"売買代金 {scr.min_turnover_jpy / 1e8:.0f}億円以上", s2))
    s3 = s2[s2["close"].between(scr.min_price, scr.max_price)]
    steps.append((f"株価 {scr.min_price:,}〜{scr.max_price:,}円", s3))
    s4 = s3.nlargest(min(max_symbols, len(s3)), "turnover_value")
    steps.append((f"流動性の上位 {max_symbols} 銘柄", s4))

    out.append("")
    for label, frame in steps:
        out.append(f"  {label:<32} {len(frame):>6,} 銘柄")

    # ---------------------------------------------------- 資金で買えるか
    out.append("")
    out.append("【4】いまの資金で買えるか（1単元＝100株）")
    cap = sc.equity_jpy * sc.max_position_pct
    out.append(f"  1銘柄あたりの投資上限: {cap:,.0f} 円"
               f"（{sc.equity_jpy:,.0f} 円 × {sc.max_position_pct:.0%}）")
    out.append("")
    out.append(f"  {'市場':<22} {'銘柄数':>7} {'1単元の中央値':>14} {'上限内で買える':>12}")
    out.append("  " + "-" * 60)
    for code in sorted(snap["market_code"].dropna().unique()):
        sub = snap[snap["market_code"] == code]
        lots = sub["close"].dropna() * sc.lot_size
        if lots.empty:
            continue
        reach = float((lots <= cap).mean())
        out.append(
            f"  {MARKET_NAMES.get(str(code), str(code)):<22} {len(sub):>7,}"
            f" {np.median(lots):>13,.0f}円 {reach:>11.0%}"
        )

    growth = snap[snap["market_code"] == "0113"]
    if not growth.empty:
        lots = growth["close"].dropna() * sc.lot_size
        reach = float((lots <= cap).mean())
        holdable = int(sc.equity_jpy // max(np.median(lots), 1))
        out.append("")
        out.append("  → グロース市場について:")
        out.append(f"     1単元の中央値 {np.median(lots):,.0f} 円 → 上限内で買えるのは {reach:.0%}")
        out.append(f"     運用資金 {sc.equity_jpy:,.0f} 円なら、中央値ベースで約 {holdable} 銘柄まで持てます")
        if holdable >= 10:
            out.append("     プライムより多くの銘柄を持てます。分散が要る戦略には有利です。")

    # ---------------------------------------------------- 注意
    out.append("")
    out.append("=" * 74)
    out.append("新興株を対象にする前に知っておくこと")
    out.append("=" * 74)
    out.append("  1. 上場廃止銘柄が入っていないと、成績は実態より良く出ます。")
    out.append("     新興株は上場廃止の頻度が高いので、この歪みは大型株よりずっと大きい。")
    out.append("     いまのデータでは上の【1】のとおり欠けています。先に直す必要があります。")
    out.append("")
    out.append("  2. 売買コストの前提が変わります。")
    out.append("     いまのスリッページ 0.2% は大型株には過大でしたが、")
    out.append("     新興株には **不足** の可能性があります。板が薄いぶん不利に約定します。")
    out.append("")
    out.append("  3. 「爆発する株」は分布が極端に偏ります。")
    out.append("     数十銘柄に 1 つが大化けし、残りは負ける、という形になりやすい。")
    out.append("     5 銘柄しか持てないと、その 1 つを引けるかどうかが運で決まります。")
    out.append("     この形の戦略は、**多く持てること** が前提条件です。")
    out.append("")
    out.append("  4. 検証に使える未使用のデータが、いまありません。")
    out.append("     2021-2026 は 17 回の検定で使い切りました。")
    out.append("     新しい対象で試すなら、履歴を伸ばして未使用の期間を作る必要があります。")

    return "\n".join(out)
