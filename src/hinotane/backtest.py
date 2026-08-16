"""バックテスト。

**本番と同じ `Strategy.evaluate()` を呼ぶ**のがこのモジュールの肝。
バックテスト専用にロジックを書き直すと、検証結果と実運用が食い違う——
個人開発の自動売買が失敗する最大の原因がこれ。ここでは構造的に避けている。

約定の扱いも本番（PaperBroker）と揃えてある:
  - エントリー: シグナル翌営業日の **始値** × (1 + スリッページ)
  - 決済      : 日足の **高値・安値** で損切り/利確の到達を判定
  - 同じ日に損切りと利確の両方に触れたら **損切り側** を採用（保守的）

⚠️ 生存者バイアスについて
このバックテストは DB にある銘柄のみを対象にする。DB は「取得時点で上場している
銘柄」で構成されるため、途中で上場廃止になった銘柄が含まれない。
結果は実態よりやや良く出る。絶対値ではなく戦略同士の相対比較として読むこと。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .config import AppConfig
from .db import Database
from .indicators import enrich
from .strategies.base import get_strategy

log = logging.getLogger(__name__)


@dataclass
class Trade:
    code: str
    name: str
    strategy: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    quantity: int
    pnl_jpy: float
    risk_jpy: float            # この取引で失う想定だった金額（損切り幅 × 数量）
    r_multiple: float          # 損切り幅の何倍取れたか
    exit_reason: str


@dataclass
class BacktestResult:
    label: str
    start: date
    end: date
    initial_equity: float
    final_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=pd.Series)

    # ------------------------------------------------------------------ 指標

    @property
    def total_return(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return self.final_equity / self.initial_equity - 1.0

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl_jpy > 0) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_jpy for t in self.trades if t.pnl_jpy > 0)
        losses = -sum(t.pnl_jpy for t in self.trades if t.pnl_jpy < 0)
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def expectancy_r(self) -> float:
        """リスク 1 単位あたりの損益（R 倍単位）。

        **単純平均ではなくリスク額で加重する。** 単元株の丸めや 1 銘柄あたりの
        投資上限により、1 取引あたりのリスク額は実際には数百円〜1万円とばらつく。
        単純平均だと「R はプラスなのに円では負けている」という矛盾が起きうる
        （実際に起きた）。加重すれば符号は必ず円の損益と一致する。
        """
        total_risk = sum(t.risk_jpy for t in self.trades)
        if total_risk <= 0:
            return 0.0
        return sum(t.pnl_jpy for t in self.trades) / total_risk

    @property
    def net_pnl_jpy(self) -> float:
        return sum(t.pnl_jpy for t in self.trades)

    @property
    def avg_risk_jpy(self) -> float:
        """1 取引あたりの実際のリスク額。設定値より大幅に小さければ
        1 銘柄あたりの投資上限に頭を抑えられている。"""
        if not self.trades:
            return 0.0
        return float(np.mean([t.risk_jpy for t in self.trades]))

    @property
    def max_drawdown(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        peak = self.equity_curve.cummax()
        return float(((peak - self.equity_curve) / peak).max())

    def summary(self) -> str:
        if not self.trades:
            return f"[{self.label}] 取引が 1 件も発生しませんでした（条件が厳しすぎる可能性）"
        return (
            f"[{self.label}] {self.start} 〜 {self.end}\n"
            f"  取引数        : {len(self.trades)}\n"
            f"  勝率          : {self.win_rate:.1%}\n"
            f"  総リターン    : {self.total_return:+.1%}\n"
            f"  最大DD        : {self.max_drawdown:.1%}\n"
            f"  プロフィットF : {self.profit_factor:.2f}\n"
            f"  期待値        : {self.expectancy_r:+.2f} R（リスク額で加重）\n"
            f"  確定損益      : {self.net_pnl_jpy:+,.0f} 円\n"
            f"  1取引の平均リスク: {self.avg_risk_jpy:,.0f} 円\n"
            f"  最終資産      : {self.final_equity:,.0f} 円"
        )


def _build_signal_table(
    cfg: AppConfig, db: Database, strategy_names: list[str], max_symbols: int
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, str]]:
    """全銘柄 × 全戦略を先に評価して、日付順のシグナル表を作る。"""
    sc = cfg.screener
    universe = db.universe(sc.market_codes, sc.min_history_days)
    if universe.empty:
        return pd.DataFrame(), {}, {}

    codes = universe["code"].tolist()
    names = dict(zip(universe["code"], universe["name"], strict=True))
    bars_by_code = db.bars_bulk(codes, limit_days=100_000)

    # 計算量を抑えるため流動性の高い順に絞る。
    #
    # ⚠️ ここは必ず **期間の先頭** の流動性で判定する。
    # 末尾（tail）で判定すると「検証期間の終わりに流動性が高かった銘柄」を
    # 期間の初めから売買することになる。売買代金は株価×出来高なので、
    # これは実質「値上がりした銘柄だけを選んで検証する」ことに等しく、
    # 何を試しても勝っているように見えてしまう。
    # 先頭 60 本なら、売買を始める時点で分かっている情報だけで選べる。
    liquidity = {
        code: float(df["turnover_value"].head(60).mean() or 0) for code, df in bars_by_code.items()
    }
    codes = sorted(liquidity, key=lambda c: liquidity[c], reverse=True)[:max_symbols]

    strategies = [get_strategy(n) for n in strategy_names]
    rows: list[pd.DataFrame] = []
    prices: dict[str, pd.DataFrame] = {}

    for code in codes:
        bars = bars_by_code.get(code)
        if bars is None or len(bars) < sc.min_history_days:
            continue
        enriched = enrich(bars)

        liquid = enriched["turnover_ma20"] >= sc.min_turnover_jpy
        in_range = enriched["close"].between(sc.min_price, sc.max_price)
        tradable = liquid & in_range

        prices[code] = enriched.set_index("date")[["open", "high", "low", "close"]]

        for strategy in strategies:
            out = strategy.evaluate(enriched)
            hit = out[out["entry"] & tradable]
            if hit.empty:
                continue
            rows.append(
                pd.DataFrame(
                    {
                        "date": hit["date"],
                        "code": code,
                        "strategy": strategy.name,
                        "ref_price": hit["close"],
                        "stop_price": hit["stop_price"],
                        "target_price": hit["target_price"],
                        "score": hit["score"],
                        "max_holding_days": strategy.max_holding_days,
                    }
                )
            )

    if not rows:
        return pd.DataFrame(), prices, names
    table = pd.concat(rows, ignore_index=True).sort_values(["date", "score"], ascending=[True, False])
    return table, prices, names


def run_backtest(
    cfg: AppConfig,
    db: Database,
    *,
    strategy_names: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    label: str = "backtest",
    max_symbols: int = 600,
) -> BacktestResult:
    strategy_names = strategy_names or cfg.screener.strategies
    table, prices, names = _build_signal_table(cfg, db, strategy_names, max_symbols)

    equity = cfg.risk.equity_jpy
    initial = equity
    if table.empty:
        return BacktestResult(label, start or date.min, end or date.max, initial, initial)

    if start:
        table = table[table["date"] >= start]
    if end:
        table = table[table["date"] <= end]
    if table.empty:
        return BacktestResult(label, start or date.min, end or date.max, initial, initial)

    all_dates = sorted({d for df in prices.values() for d in df.index})
    if start:
        all_dates = [d for d in all_dates if d >= start]
    if end:
        all_dates = [d for d in all_dates if d <= end]

    # dict(table.groupby("date")) は使えない。DataFrameGroupBy は `.keys` 属性に
    # グループ化キー名（"date"）を持つため、dict() がマッピングとして誤認して落ちる。
    signals_by_date: dict[date, pd.DataFrame] = {  # noqa: C416
        d: g for d, g in table.groupby("date")
    }
    date_index = {d: i for i, d in enumerate(all_dates)}

    open_positions: list[dict] = []
    trades: list[Trade] = []
    curve: list[tuple[date, float]] = []

    lot = cfg.risk.lot_size
    slip = cfg.execution.slippage_pct
    fee = cfg.execution.commission_pct

    for i, today in enumerate(all_dates):
        # ---- 1. 前営業日のシグナルを、当日の始値でエントリー ---------------
        #
        # 決済判定より先に行う。寄り付きの時点では、その日のうちに
        # どの建玉が決済されるかを知りようがないため、決済で空いた枠を
        # 同じ日のエントリーに使えてしまうのは未来を覗いていることになる。
        if i > 0:
            prev = all_dates[i - 1]
            candidates = signals_by_date.get(prev)
            if candidates is not None:
                held_codes = {p["code"] for p in open_positions}
                taken = 0
                for _, sig in candidates.iterrows():
                    if len(open_positions) >= cfg.risk.max_open_positions:
                        break
                    if taken >= cfg.risk.max_signals_per_day:
                        break
                    code = sig["code"]
                    if code in held_codes or today not in prices[code].index:
                        continue

                    raw_open = float(prices[code].loc[today, "open"])
                    if not math.isfinite(raw_open) or raw_open <= 0:
                        continue
                    entry_price = raw_open * (1 + slip)
                    stop = float(sig["stop_price"])
                    # 寄り付きの時点で既に損切り価格を割っているなら見送る
                    if stop >= raw_open:
                        continue

                    risk_per_share = entry_price - stop
                    qty = int(math.floor(equity * cfg.risk.risk_per_trade / risk_per_share / lot) * lot)
                    max_cost = equity * cfg.risk.max_position_pct
                    if qty * entry_price > max_cost:
                        qty = int(math.floor(max_cost / entry_price / lot) * lot)
                    if qty < lot:
                        continue

                    open_positions.append(
                        {
                            "code": code,
                            "strategy": sig["strategy"],
                            "entry_date": today,
                            "entry_price": entry_price,
                            "quantity": qty,
                            "stop": stop,
                            "target": float(sig["target_price"]),
                            "max_holding_days": int(sig["max_holding_days"]),
                        }
                    )
                    held_codes.add(code)
                    taken += 1

        # ---- 2. 建玉の決済判定（当日の高値・安値で） ----------------------
        #
        # **当日エントリーした建玉も対象に含める。** 買った初日に損切り価格を
        # 割ることは普通に起きる。ここを翌日以降からにすると、本来なら
        # その日に損切りされた取引が生き延びて、成績が実態より良く出る。
        still_open: list[dict] = []
        for pos in open_positions:
            bar = prices[pos["code"]].loc[today] if today in prices[pos["code"]].index else None
            if bar is None:
                still_open.append(pos)
                continue

            held = i - date_index[pos["entry_date"]]
            bar_open = float(bar["open"])
            exit_price = exit_reason = None

            if bar["low"] <= pos["stop"]:
                # 損切りは「その価格で必ず約定する」とは限らない。
                # 損切り価格を下回って寄り付いたら、実際の約定は寄り値になる。
                # ここを stop 固定にすると、ギャップダウンの損失が消えて
                # 成績が実態より良く出る。
                exit_price = min(bar_open, pos["stop"])
                exit_reason = "stop"
            elif bar["high"] >= pos["target"]:
                # 利確は指値。目標より上で寄り付いたらその寄り値で約定する。
                exit_price = max(bar_open, pos["target"])
                exit_reason = "target"
            elif held >= pos["max_holding_days"]:
                exit_price, exit_reason = float(bar["close"]), "timeout"

            if exit_price is None:
                still_open.append(pos)
                continue

            fill = exit_price * (1 - slip)
            gross = (fill - pos["entry_price"]) * pos["quantity"]
            commission = (fill + pos["entry_price"]) * pos["quantity"] * fee
            pnl = gross - commission
            equity += pnl
            risk = (pos["entry_price"] - pos["stop"]) * pos["quantity"]

            trades.append(
                Trade(
                    code=pos["code"],
                    name=str(names.get(pos["code"], pos["code"])),
                    strategy=pos["strategy"],
                    entry_date=pos["entry_date"],
                    entry_price=pos["entry_price"],
                    exit_date=today,
                    exit_price=fill,
                    quantity=pos["quantity"],
                    pnl_jpy=pnl,
                    risk_jpy=risk,
                    r_multiple=pnl / risk if risk > 0 else 0.0,
                    exit_reason=exit_reason,
                )
            )
        open_positions = still_open

        # ---- 3. 時価評価 ------------------------------------------------
        unrealized = 0.0
        for pos in open_positions:
            df = prices[pos["code"]]
            if today in df.index:
                unrealized += (float(df.loc[today, "close"]) - pos["entry_price"]) * pos["quantity"]
        curve.append((today, equity + unrealized))

    equity_curve = pd.Series(dict(curve)).sort_index()
    return BacktestResult(
        label=label,
        start=all_dates[0],
        end=all_dates[-1],
        initial_equity=initial,
        final_equity=float(equity_curve.iloc[-1]) if not equity_curve.empty else initial,
        trades=trades,
        equity_curve=equity_curve,
    )


def walk_forward(
    cfg: AppConfig,
    db: Database,
    *,
    strategy_names: list[str] | None = None,
    split: float = 0.6,
    max_symbols: int = 600,
) -> tuple[BacktestResult, BacktestResult]:
    """イン／アウトオブサンプル分割で検証する。

    過剰最適化を見抜くための最低限の作法。前半（イン）でだけ成績が良く、
    後半（アウト）で崩れる戦略は、過去に当てはめただけで先には効かない。
    """
    dates = db.query("SELECT DISTINCT date FROM daily_quotes ORDER BY date")["date"].tolist()
    if len(dates) < 200:
        raise ValueError("検証に十分な日足がありません。先に `hinotane backfill` を実行してください。")

    boundary = dates[int(len(dates) * split)]
    in_sample = run_backtest(
        cfg, db, strategy_names=strategy_names, end=boundary,
        label="イン・サンプル（前半）", max_symbols=max_symbols,
    )
    out_sample = run_backtest(
        cfg, db, strategy_names=strategy_names, start=boundary,
        label="アウト・オブ・サンプル（後半）", max_symbols=max_symbols,
    )
    return in_sample, out_sample


def judge(in_sample: BacktestResult, out_sample: BacktestResult) -> tuple[bool, list[str]]:
    """イン／アウトオブサンプルの結果から合否を出す。

    **お金が増えていないものは、何があっても合格にしない。**
    以前は期待値（R の単純平均）だけで判定していたため、
    「両期間とも資産が減っているのに合格」という表示が出た。
    R はリスク額で割った比率なので、取引ごとにリスク額がばらつくと
    円の損益と符号が食い違いうる。最終的な判断はお金で行う。

    Returns:
        (合格したか, 表示する行のリスト)
    """
    lines: list[str] = []

    if not out_sample.trades:
        return False, ["⚠️  アウトオブサンプルで取引が発生せず、判断できません。"]

    if len(out_sample.trades) < 30:
        lines.append(
            f"⚠️  アウトオブサンプルの取引が {len(out_sample.trades)} 件しかなく、"
            " 偶然の影響が大きい水準です。数字を強く信じないでください。"
        )

    failures: list[str] = []

    if out_sample.net_pnl_jpy <= 0:
        failures.append(
            f"アウトオブサンプルで資産が減っています"
            f"（{out_sample.net_pnl_jpy:+,.0f} 円 / {out_sample.total_return:+.1%}）"
        )
    if out_sample.expectancy_r <= 0:
        failures.append(f"アウトオブサンプルの期待値がマイナスです（{out_sample.expectancy_r:+.2f} R）")
    if out_sample.profit_factor <= 1.0:
        failures.append(
            f"アウトオブサンプルのプロフィットファクターが 1 以下です"
            f"（{out_sample.profit_factor:.2f}）＝ 損失が利益を上回っています"
        )

    if failures:
        lines.append("❌ この戦略は実運用に載せないでください。")
        lines.extend(f"   ・{reason}" for reason in failures)
        if in_sample.net_pnl_jpy > 0:
            lines.append("   ・前半では勝てていたので、過剰最適化の可能性があります。")
        return False, lines

    if in_sample.expectancy_r > 0 and out_sample.expectancy_r < in_sample.expectancy_r * 0.5:
        lines.append("⚠️  後半で期待値が半分以下に落ちています。過剰最適化の疑いが濃厚です。")
        return False, lines

    lines.append("✅ 前半・後半とも資産が増え、期待値も保たれています。")
    lines.append("   ただしこれは必要条件であって十分条件ではありません。")
    lines.append("   生存者バイアス（上場廃止銘柄を含まない）のぶん、実際はこれより悪くなります。")
    return True, lines
