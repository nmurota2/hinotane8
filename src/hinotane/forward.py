"""紙トレードの成績を、買い持ちと並べて記録する。

## なぜこれが要るのか

過去データでの検証は、2021-2026 の 5 年で **累計 17 回** の検定を使い切った。
同じデータで次に何を試しても、その数字はもう信用できない。
**汚染されていない検証手段は「これから先の値動き」だけ** になった。

## なぜ買い持ちを一緒に記録するのか

この探索で何度も起きた失敗がこれ。
「儲かった＝手法が正しい」と読んでしまう。実際には 5 年で対象銘柄を
ただ持っていただけで +112.5% だったので、上げ相場ならどんな買い戦略でも
たいてい儲かる。**勝負は「儲かったか」ではなく「持っていただけより儲かったか」**。

だから毎回、同じ期間・同じ銘柄群の買い持ちを並べて出す。
差が出ていなければ、その戦略は動かす意味がない。

## 実弾を入れる条件

ここでの記録は、判断材料であって合格証ではない。
`walkforward` の合否条件（`backtest.judge`）を、**フォワードの実績で**
満たすまでは擬似発注のまま。目安として 30 取引以上・6 か月以上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .config import AppConfig, today
from .db import Database

FORWARD_ID = "default"


@dataclass
class ForwardStatus:
    started_on: date
    start_equity: float
    strategies: str
    lines: list[str]

    def __str__(self) -> str:
        return "\n".join(self.lines)


def start(cfg: AppConfig, db: Database, *, strategies: str, note: str = "") -> str:
    """記録の開始点を固定する。すでにあれば何もしない。

    開始日を後から動かせるようにしてはいけない。動かせると
    「調子の良かった時期からの成績」を選べてしまう。
    """
    db.init_schema()
    existing = db.query("SELECT * FROM forward_test WHERE id = ?", [FORWARD_ID])
    if not existing.empty:
        row = existing.iloc[0]
        return (
            f"すでに {pd.Timestamp(row['started_on']).date()} から記録中です"
            f"（対象: {row['strategies']}）。\n"
            "開始日は変更できません。やり直したい場合は、その理由を残したうえで\n"
            "`DELETE FROM forward_test` を手で実行してください。"
        )
    db.execute(
        "INSERT INTO forward_test (id, started_on, start_equity, strategies, note)"
        " VALUES (?, ?, ?, ?, ?)",
        [FORWARD_ID, today(), cfg.risk.equity_jpy, strategies, note],
    )
    return (
        f"✅ {today()} から紙トレードの記録を開始しました\n"
        f"   対象戦略 : {strategies}\n"
        f"   運用資金 : {cfg.risk.equity_jpy:,.0f} 円（擬似）\n"
        "   比較相手 : 同じ期間・同じ銘柄群の等ウェイト買い持ち\n\n"
        "   毎日 `hinotane fetch` → `hinotane screen` → `hinotane execute` →"
        " `hinotane mark` を回してください。\n"
        "   経過は `hinotane forward` で確認できます。"
    )


def _benchmark_return(cfg: AppConfig, db: Database, since: date) -> tuple[float, float]:
    """記録開始日から今日までの、等ウェイト買い持ちのリターンと最大DD。

    対象は「開始時点で十分な履歴があった銘柄」。開始後に上場した銘柄を
    入れると、後から分かった情報で比較相手を選ぶことになる。
    """
    sc = cfg.screener
    universe = db.universe(sc.market_codes, sc.min_history_days)
    if universe.empty:
        return (0.0, 0.0)
    codes = universe["code"].tolist()
    placeholders = ",".join("?" for _ in codes)
    df = db.query(
        f"""
        SELECT code, date, close FROM daily_quotes
        WHERE date >= ? AND code IN ({placeholders})
        ORDER BY date
        """,
        [since, *codes],
    )
    if df.empty:
        return (0.0, 0.0)
    wide = df.pivot(index="date", columns="code", values="close").sort_index()
    base = wide.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan)
    curve = wide.div(base).replace([np.inf, -np.inf], np.nan).mean(axis=1, skipna=True).dropna()
    if len(curve) < 2:
        return (0.0, 0.0)
    ret = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
    peak = curve.cummax()
    dd = float(((peak - curve) / peak).max())
    return (ret, dd)


def status(cfg: AppConfig, db: Database) -> ForwardStatus:
    db.init_schema()
    row = db.query("SELECT * FROM forward_test WHERE id = ?", [FORWARD_ID])
    if row.empty:
        return ForwardStatus(
            today(), cfg.risk.equity_jpy, "",
            ["まだ記録を始めていません。`hinotane forward-start` を実行してください。"],
        )
    rec = row.iloc[0]
    since = pd.Timestamp(rec["started_on"]).date()
    start_equity = float(rec["start_equity"])

    closed = db.query(
        "SELECT * FROM positions WHERE status = 'closed' AND entry_date >= ?", [since]
    )
    open_pos = db.query(
        "SELECT * FROM positions WHERE status = 'open' AND entry_date >= ?", [since]
    )

    realized = float(closed["pnl_jpy"].fillna(0).sum()) if not closed.empty else 0.0

    # 建玉の評価損益。最新の終値で洗い替える。
    unrealized = 0.0
    for _, p in open_pos.iterrows():
        last = db.query(
            "SELECT close FROM daily_quotes WHERE code = ? ORDER BY date DESC LIMIT 1",
            [str(p["code"])],
        )
        if last.empty:
            continue
        unrealized += (float(last.iloc[0]["close"]) - float(p["entry_price"])) * int(p["quantity"])

    equity = start_equity + realized + unrealized
    total_return = equity / start_equity - 1.0 if start_equity > 0 else 0.0
    bench_ret, bench_dd = _benchmark_return(cfg, db, since)

    days = (today() - since).days
    n_closed = len(closed)
    wins = int((closed["pnl_jpy"] > 0).sum()) if n_closed else 0

    lines = [
        "=" * 66,
        f"紙トレードの経過（{since} 〜 {today()} / {days} 日）",
        "=" * 66,
        f"  対象戦略     : {rec['strategies']}",
        f"  決済済み     : {n_closed} 件"
        + (f"（勝率 {wins / n_closed:.1%}）" if n_closed else ""),
        f"  保有中       : {len(open_pos)} 件",
        "",
        f"  確定損益     : {realized:>+12,.0f} 円",
        f"  評価損益     : {unrealized:>+12,.0f} 円",
        f"  合計         : {realized + unrealized:>+12,.0f} 円"
        f"（{total_return:+.2%}）",
        "",
        f"  同期間の買い持ち: {bench_ret:>+.2%}（最大DD {bench_dd:.1%}）",
        f"  差（戦略の付加価値）: {total_return - bench_ret:>+.2%}",
    ]

    # ---- ここが判断の本体 ----------------------------------------------
    lines.append("")
    if n_closed < 30 or days < 180:
        lines.append("  ⏳ まだ判断できません。")
        lines.append(f"     目安は 30 取引以上・180 日以上（いま {n_closed} 件・{days} 日）。")
        lines.append("     いまの差は運の範囲です。数字を追いかけないでください。")
    elif total_return <= bench_ret:
        lines.append("  ❌ 買い持ちに勝てていません。")
        lines.append("     この戦略を動かす意味がありません。実弾は入れないでください。")
    else:
        lines.append("  📊 買い持ちを上回っています。ただしこれは必要条件にすぎません。")
        lines.append("     `hinotane walkforward` の合否条件を、この実績で満たすか確認してください。")
        lines.append("     （期待値の信頼区間・利益の集中度・ドローダウンも見る必要があります）")

    lines.append("")
    lines.append("  ⚠️ ここでの記録は判断材料であって合格証ではありません。")
    lines.append("     過去データでの検証は 17 回の検定で使い切りました。")
    lines.append("     汚染されていない証拠は、この記録だけです。急がないでください。")

    return ForwardStatus(since, start_equity, str(rec["strategies"]), lines)
