"""負けている戦略の「どこが悪いのか」をデータで特定する。

戦略を作り直す前に、必ずここを通す。推測で書き換えても同じことの繰り返しになる。

見るのはこの 5 点:

1. **市況（ベンチマーク）**
   対象銘柄を等ウェイトで買い持ちしたらどうだったか。市場自体が下げた期間に
   買い戦略が負けるのは当たり前で、戦略の欠陥とは言えない。逆に市場が上げた
   のに負けているなら、戦略が明確に足を引っ張っている。

2. **戦略ごとの成績**
   3 つのうち 1 つだけが大負けしているなら、それを外すだけで改善する。

3. **決済理由の内訳**
   損切りばかりなら損切りが浅すぎる。時間切ればかりなら利確目標が遠すぎる。

4. **シグナルの取りこぼし**
   枠（同時保有数・1日の上限）で弾かれた数。多いなら、そもそも
   良いシグナルを選べていない可能性がある。

5. **資金の稼働率**
   建玉がない日が多いなら、そもそも勝負していない。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

from .backtest import _build_signal_table, run_backtest
from .config import AppConfig
from .db import Database


@dataclass
class Diagnosis:
    lines: list[str]

    def __str__(self) -> str:
        return "\n".join(self.lines)


def _distinct_opportunities(table: pd.DataFrame, gap_days: int = 5) -> int:
    """連日続いたシグナルを 1 つの機会としてまとめた件数。

    順張りの条件は一度成立すると数日〜数十日続けて成立する。生の件数を
    「取りこぼし率」の分母にすると、同じ銘柄の同じ上昇局面が何十回もの
    機会として数えられ、「99% 取りこぼしている」という誤った診断になる。
    """
    if table.empty:
        return 0
    count = 0
    for (_code, _strategy), group in table.groupby(["code", "strategy"]):
        days = sorted(pd.to_datetime(group["date"]).dt.normalize().unique())
        count += 1
        for prev, cur in pairwise(days):
            if (cur - prev).days > gap_days:
                count += 1
    return count


def diagnose(
    cfg: AppConfig,
    db: Database,
    *,
    strategy_names: list[str] | None = None,
    max_symbols: int = 600,
) -> Diagnosis:
    strategy_names = strategy_names or cfg.screener.strategies
    out: list[str] = []

    table, prices, _names = _build_signal_table(cfg, db, strategy_names, max_symbols)
    if table.empty or not prices:
        return Diagnosis(["シグナルが 1 件も生成されませんでした。条件が厳しすぎます。"])

    result = run_backtest(
        cfg,
        db,
        strategy_names=strategy_names,
        label="診断",
        max_symbols=max_symbols,
        prebuilt=(table, prices, _names),
    )

    # ---------------------------------------------------------- 1. 市況
    bh = result.benchmark_return
    bh_dd = result.benchmark_max_drawdown
    out.append("=" * 62)
    out.append("【1】この期間の市況（戦略のせいか、相場のせいかを切り分ける）")
    out.append("=" * 62)
    out.append(f"  等ウェイトで買い持ち : {bh:+.1%}（最大DD {bh_dd:.1%}）")
    out.append(
        f"  戦略                 : {result.total_return:+.1%}（最大DD {result.max_drawdown:.1%}）"
    )
    gap = result.total_return - bh
    out.append(f"  リターンの差         : {gap:+.1%}")
    out.append("")
    out.append("  ⚠️ この比較は買い持ち側に有利に出来ています。並べて読むときは差し引くこと:")
    out.append("     ・買い持ちは常時100%投資。戦略は建玉が埋まっていない時間があります")
    out.append("     ・買い持ちには手数料もスリッページもかかっていません")
    out.append("     ・上場廃止になった銘柄が DB に無いので、買い持ち側は生存者バイアスを丸取り")
    out.append("     ・買い持ちには損切りがありません（そのぶん最大DDは深く出ます）")
    if bh < 0 and result.total_return > bh:
        out.append("  → 下げ相場で、買い持ちよりはマシ。戦略が悪いとは言い切れません。")
    elif result.total_return < bh and result.max_drawdown >= bh_dd:
        out.append(
            "  → ⚠️ リターンでもドローダウンでも買い持ちに負けています。"
            " 売買する意味がありません。"
        )
    elif bh > 0.05 and result.total_return < 0:
        out.append("  → ⚠️ 相場は上げているのに負けています。戦略が明確に足を引っ張っています。")
    elif result.total_return < bh:
        out.append(
            "  → リターンでは買い持ちに負けていますが、ドローダウンは浅く済んでいます。"
        )
    elif bh <= 0.05:
        out.append("  → 相場自体がほぼ横ばい。買い戦略には厳しい期間でした。")

    # ------------------------------------------------------ 2. 戦略ごと
    out.append("")
    out.append("=" * 62)
    out.append("【2】戦略ごとの成績（足を引っ張っているものを特定する）")
    out.append("=" * 62)
    by_strategy: dict[str, list] = defaultdict(list)
    for t in result.trades:
        by_strategy[t.strategy].append(t)

    if not by_strategy:
        out.append("  取引が発生していません。")
    else:
        out.append(f"  {'戦略':<10} {'取引':>5} {'勝率':>7} {'期待値':>8} {'損益(円)':>12}")
        out.append("  " + "-" * 48)
        for name in sorted(by_strategy):
            ts = by_strategy[name]
            wins = sum(1 for t in ts if t.pnl_jpy > 0)
            risk = sum(t.risk_jpy for t in ts)
            pnl = sum(t.pnl_jpy for t in ts)
            exp = pnl / risk if risk > 0 else 0.0
            out.append(
                f"  {name:<10} {len(ts):>5} {wins / len(ts):>6.1%} "
                f"{exp:>+7.2f}R {pnl:>+12,.0f}"
            )
        worst = min(by_strategy, key=lambda n: sum(t.pnl_jpy for t in by_strategy[n]))
        worst_pnl = sum(t.pnl_jpy for t in by_strategy[worst])
        if worst_pnl < 0 and len(by_strategy) > 1:
            out.append(f"  → 最も負けているのは「{worst}」（{worst_pnl:+,.0f} 円）")

    # -------------------------------------------------- 3. 決済理由
    out.append("")
    out.append("=" * 62)
    out.append("【3】決済理由の内訳（損切り・利確の設定が適切か）")
    out.append("=" * 62)
    total = len(result.trades) or 1
    # ⚠️ exit_reason が "stop" でも、損失とは限らない。
    # トレーリングストップは利益が乗ると損切りラインを買値の上まで切り上げるので、
    # 「ストップに当たって利益確定」が正常な決済のかたちになる。
    # これを一括りに「損切り」と表示すると、うまくいっている戦略に対して
    # 「損切り幅が狭すぎる」という逆の診断が出る。
    buckets = [
        ("stop", lambda t: t.pnl_jpy < 0, "損切り（買値より下）"),
        ("stop", lambda t: t.pnl_jpy >= 0, "ストップで利確（切り上げ後）"),
        ("target", lambda t: True, "利確目標"),
        ("timeout", lambda t: True, "時間切れ"),
        ("期末", lambda t: True, "期末で打ち切り"),
    ]
    for reason, cond, label in buckets:
        ts = [t for t in result.trades if t.exit_reason == reason and cond(t)]
        n = len(ts)
        pnl = sum(t.pnl_jpy for t in ts)
        out.append(f"  {label:<22} {n:>4} 件 ({n / total:>5.1%})  損益 {pnl:>+11,.0f} 円")

    losing_stops = sum(1 for t in result.trades if t.exit_reason == "stop" and t.pnl_jpy < 0)
    stop_rate = losing_stops / total
    timeout_rate = sum(1 for t in result.trades if t.exit_reason == "timeout") / total
    if stop_rate > 0.55:
        out.append("  → ⚠️ 損失での損切りが過半。損切り幅が狭すぎて、ノイズで振り落とされている疑い。")
    if timeout_rate > 0.35:
        out.append("  → ⚠️ 時間切れが多い。利確目標が遠すぎるか、そもそも動かない銘柄を掴んでいる。")
    if result.forced_exits / total > 0.25:
        out.append(
            "  → ⚠️ 期末持越が多い。検証期間が戦略の保有期間に対して短く、"
            "成績が途中経過に近い。データ期間を延ばすまで数字は仮のもの。"
        )

    holding = [
        (t.exit_date - t.entry_date).days for t in result.trades if t.exit_date and t.entry_date
    ]
    if holding:
        out.append(f"  平均保有日数: {np.mean(holding):.1f} 日（暦日）")

    # ------------------------------------------ 4. シグナルの取りこぼし
    out.append("")
    out.append("=" * 62)
    out.append("【4】シグナルの取りこぼし（枠が足りているか）")
    out.append("=" * 62)
    generated = len(table)
    taken = len(result.trades)
    # ⚠️ 生の件数は「取りこぼし」の分母にならない。
    # 順張りの条件は一度成立すると連日成立し続けるので、同じ銘柄の同じ上昇局面が
    # 何十件にも数えられる。連続したシグナルを 1 つの機会としてまとめた数のほうが、
    # 「本当は何回チャンスがあったのか」に近い。
    opportunities = _distinct_opportunities(table)
    per_day = table.groupby("date").size()
    out.append(f"  生成されたシグナル : {generated:,} 件")
    out.append(f"  独立した機会（連日の重複をまとめた数）: {opportunities:,} 件")
    out.append(f"  実際に取れた取引   : {taken:,} 件（独立した機会の {taken / max(opportunities, 1):.1%}）")
    out.append(f"  シグナルが出た日   : {len(per_day):,} 日（1日あたり平均 {per_day.mean():.1f} 件）")
    if taken / max(opportunities, 1) < 0.2:
        out.append(
            f"  → 大半を取りこぼしています。同時保有 {cfg.risk.max_open_positions} 銘柄 /"
            f" 1日 {cfg.risk.max_signals_per_day} 件の枠がボトルネックです。"
        )
        out.append("    枠を広げるより、シグナルの選別を厳しくして質を上げるほうが有効です。")

    # ---------------------------------------------------- 5. 稼働率
    out.append("")
    out.append("=" * 62)
    out.append("【5】資金の稼働状況")
    out.append("=" * 62)
    if result.trades:
        span_days = (result.end - result.start).days or 1
        exposure = sum(holding) / (span_days * cfg.risk.max_open_positions)
        out.append(f"  建玉が埋まっていた割合（概算）: {exposure:.0%}")
        out.append(f"  1取引の平均リスク額           : {result.avg_risk_jpy:,.0f} 円")
        target_risk = cfg.risk.equity_jpy * cfg.risk.risk_per_trade
        out.append(f"  設定上のリスク額               : {target_risk:,.0f} 円")
        if result.avg_risk_jpy < target_risk * 0.8:
            out.append(
                "  → ⚠️ 実際のリスク額が設定より小さいです。1銘柄あたりの投資上限"
                f"（{cfg.risk.max_position_pct:.0%}）に頭を抑えられ、"
                "狙ったリスクを取れていません。"
            )
        if exposure < 0.3:
            out.append("  → ⚠️ 資金の大半が遊んでいます。勝負回数が足りません。")

    return Diagnosis(out)
