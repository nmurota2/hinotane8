"""リスク管理とポジションサイジング。

このモジュールが実質的な安全装置。戦略が「買いたい」と言っても、
ここを通らなければ通知も発注もされない。

ポジションサイジングの考え方（固定比率リスク法）:
    1 トレードで失ってよい金額 = 運用資金 × risk_per_trade（既定 1%）
    1 株あたりの損失幅         = エントリー価格 - 損切り価格
    数量 = 失ってよい金額 ÷ 1株あたり損失幅 （100株単位に切り下げ）

「いくら買うか」ではなく「いくら失う覚悟か」から数量を決めるので、
値動きの荒い銘柄は自動的に少なく、穏やかな銘柄は多く持つことになる。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .config import RiskConfig
from .db import Database
from .ids import signal_id
from .strategies.base import StrategySignal

log = logging.getLogger(__name__)


@dataclass
class SizedSignal:
    """数量まで決まったシグナル。"""

    signal: StrategySignal
    quantity: int
    risk_jpy: float       # 損切りに当たった場合に失う金額
    cost_jpy: float       # 必要資金

    @property
    def code(self) -> str:
        return self.signal.code

    @property
    def signal_key(self) -> str:
        """DB の signals.id と一致する決定的な ID。LINE の postback にも載せる。"""
        return signal_id(self.signal.signal_date, self.signal.code, self.signal.strategy)


@dataclass
class Rejection:
    code: str
    strategy: str
    reason: str


class RiskManager:
    def __init__(self, cfg: RiskConfig, db: Database):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------------ サイジング

    def size(self, signal: StrategySignal) -> SizedSignal | None:
        risk_per_share = signal.risk_per_share
        if risk_per_share <= 0:
            return None

        budget = self.cfg.equity_jpy * self.cfg.risk_per_trade
        raw_qty = budget / risk_per_share
        lot = self.cfg.lot_size
        quantity = int(math.floor(raw_qty / lot) * lot)

        if quantity < lot:
            return None  # 1 単元も買えないほど損切り幅が広い＝見送り

        # 1 銘柄あたりの投資上限でさらに切り下げる
        max_cost = self.cfg.equity_jpy * self.cfg.max_position_pct
        if quantity * signal.entry_price > max_cost:
            quantity = int(math.floor(max_cost / signal.entry_price / lot) * lot)
            if quantity < lot:
                return None

        return SizedSignal(
            signal=signal,
            quantity=quantity,
            risk_jpy=quantity * risk_per_share,
            cost_jpy=quantity * signal.entry_price,
        )

    # ------------------------------------------------------------------ ガード

    def kill_switch_active(self) -> tuple[bool, str]:
        """累計ドローダウンが閾値を超えていたら全シグナルを止める。"""
        df = self.db.query(
            """
            SELECT exit_date, pnl_jpy
            FROM positions
            WHERE status = 'closed' AND pnl_jpy IS NOT NULL
            ORDER BY exit_date
            """
        )
        if df.empty:
            return False, ""

        cumulative = df["pnl_jpy"].cumsum()
        peak = cumulative.cummax().clip(lower=0.0)
        drawdown = peak - cumulative
        max_dd = float(drawdown.max())
        limit = self.cfg.equity_jpy * self.cfg.kill_switch_drawdown_pct

        if max_dd >= limit:
            return True, (
                f"最大ドローダウン {max_dd:,.0f}円 が上限 {limit:,.0f}円"
                f"（運用資金の{self.cfg.kill_switch_drawdown_pct:.0%}）に到達しました"
            )
        return False, ""

    def apply(
        self, signals: list[StrategySignal]
    ) -> tuple[list[SizedSignal], list[Rejection]]:
        """シグナル群にリスク制約を適用し、通知してよいものだけを返す。"""
        rejections: list[Rejection] = []

        halted, why = self.kill_switch_active()
        if halted:
            log.warning("キルスイッチ作動: %s", why)
            return [], [Rejection("-", "-", f"キルスイッチ作動: {why}")]

        open_codes = self.db.open_position_codes()
        open_count = self.db.count_open_positions()
        slots = max(self.cfg.max_open_positions - open_count, 0)
        if slots == 0:
            return [], [
                Rejection(
                    "-", "-",
                    f"保有銘柄数が上限 {self.cfg.max_open_positions} に達しています",
                )
            ]

        # スコアの高い順に、同一銘柄は最良の戦略ひとつだけ採用する
        ordered = sorted(signals, key=lambda s: s.score, reverse=True)
        seen: set[str] = set()
        accepted: list[SizedSignal] = []

        for signal in ordered:
            if signal.code in open_codes:
                rejections.append(Rejection(signal.code, signal.strategy, "既に保有中"))
                continue
            if signal.code in seen:
                rejections.append(
                    Rejection(signal.code, signal.strategy, "同一銘柄の別戦略シグナルと重複")
                )
                continue

            sized = self.size(signal)
            if sized is None:
                rejections.append(
                    Rejection(signal.code, signal.strategy, "損切り幅が広すぎて1単元も買えない")
                )
                continue
            if sized.cost_jpy > self.cfg.equity_jpy:
                rejections.append(Rejection(signal.code, signal.strategy, "必要資金が運用資金を超過"))
                continue

            seen.add(signal.code)
            accepted.append(sized)

            if len(accepted) >= min(self.cfg.max_signals_per_day, slots):
                break

        return accepted, rejections
