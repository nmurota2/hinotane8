"""擬似発注（ペーパートレード）。

実弾を入れずに、承認〜約定〜損益確定までの一連の流れを本番と同じ経路で動かす。
Phase 1〜3 はこれで運用し、成績が確認できてから実発注に切り替える。

約定価格は「翌営業日の始値 × (1 + スリッページ)」で近似する。
終値で約定したことにすると成績が実態より良く出てしまうため。
"""

from __future__ import annotations

import logging
import uuid
from datetime import date

from ..config import ExecutionConfig
from ..db import Database
from .base import BrokerAdapter, OrderRequest, OrderResult

log = logging.getLogger(__name__)


class PaperBroker(BrokerAdapter):
    name = "paper"
    is_paper = True

    def __init__(self, cfg: ExecutionConfig, db: Database):
        self.cfg = cfg
        self.db = db

    def _next_open(self, code: str, after: date) -> tuple[date, float] | None:
        """シグナル日の翌営業日の始値を取得する（無ければ None）。"""
        df = self.db.query(
            """
            SELECT date, open, close
            FROM daily_quotes
            WHERE code = ? AND date > ?
            ORDER BY date
            LIMIT 1
            """,
            [code, after],
        )
        if df.empty:
            return None
        row = df.iloc[0]
        price = row["open"] if row["open"] and row["open"] > 0 else row["close"]
        return row["date"], float(price)

    def buy(self, req: OrderRequest) -> OrderResult:
        signal = self.db.query(
            "SELECT signal_date, stop_price, target_price, strategy FROM signals WHERE id = ?",
            [req.signal_id],
        )
        if signal.empty:
            return OrderResult(False, None, None, 0, "シグナルが見つかりません")

        row = signal.iloc[0]
        fill = self._next_open(req.code, row["signal_date"])
        if fill is None:
            return OrderResult(
                False, None, None, 0,
                "翌営業日の株価データがまだありません（データ取得後に再実行してください）",
            )

        fill_date, raw_price = fill
        # 買いは不利な方向（高く）に滑る想定
        price = raw_price * (1 + self.cfg.slippage_pct)
        position_id = uuid.uuid4().hex[:16]

        self.db.execute(
            """
            INSERT INTO positions (
                id, signal_id, code, name, strategy, side, quantity,
                entry_date, entry_price, stop_price, target_price,
                is_paper, status
            ) VALUES (?, ?, ?, ?, ?, 'long', ?, ?, ?, ?, ?, TRUE, 'open')
            """,
            [
                position_id, req.signal_id, req.code, req.name, row["strategy"],
                req.quantity, fill_date, price, row["stop_price"], row["target_price"],
            ],
        )
        log.info("擬似買い約定: %s %d株 @ %.1f (%s)", req.code, req.quantity, price, fill_date)
        return OrderResult(
            True, position_id, price, req.quantity,
            f"擬似約定 {price:,.1f}円 × {req.quantity}株", fill_date,
        )

    def sell(self, req: OrderRequest) -> OrderResult:
        """決済。position_id は signal_id フィールドに入れて渡す。"""
        pos = self.db.query(
            "SELECT * FROM positions WHERE id = ? AND status = 'open'", [req.signal_id]
        )
        if pos.empty:
            return OrderResult(False, None, None, 0, "建玉が見つかりません")

        price = req.limit_price
        if price is None:
            return OrderResult(False, None, None, 0, "決済価格が指定されていません")
        price = price * (1 - self.cfg.slippage_pct)  # 売りは安く滑る

        row = pos.iloc[0]
        gross = (price - float(row["entry_price"])) * int(row["quantity"])
        commission = (
            (price + float(row["entry_price"])) * int(row["quantity"]) * self.cfg.commission_pct
        )
        pnl = gross - commission

        self.db.execute(
            """
            UPDATE positions
            SET status = 'closed', exit_price = ?, pnl_jpy = ?
            WHERE id = ?
            """,
            [price, pnl, req.signal_id],
        )
        return OrderResult(True, req.signal_id, price, int(row["quantity"]), f"擬似決済 損益 {pnl:,.0f}円")
