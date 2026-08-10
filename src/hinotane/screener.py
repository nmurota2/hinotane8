"""スクリーニング: 全銘柄 × 全戦略を評価して候補を抽出する。"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from .config import AppConfig, now
from .db import Database
from .ids import signal_id
from .indicators import enrich
from .risk import RiskManager, SizedSignal
from .strategies.base import Strategy, StrategySignal, get_strategy

log = logging.getLogger(__name__)


class Screener:
    def __init__(self, cfg: AppConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.strategies: list[Strategy] = [
            get_strategy(name) for name in cfg.screener.strategies
        ]

    def run(self) -> list[StrategySignal]:
        sc = self.cfg.screener
        universe = self.db.universe(sc.market_codes, sc.min_history_days)
        if universe.empty:
            log.warning("対象銘柄がありません。先に `hinotane fetch` でデータを取得してください。")
            return []

        log.info("対象 %d 銘柄 × 戦略 %s を評価します", len(universe), [s.name for s in self.strategies])

        codes = universe["code"].tolist()
        names = dict(zip(universe["code"], universe["name"], strict=True))
        max_warmup = max((s.warmup_bars for s in self.strategies), default=100)
        bars_by_code = self.db.bars_bulk(codes, limit_days=max_warmup + 60)

        signals: list[StrategySignal] = []
        skipped_liquidity = 0
        skipped_price = 0

        for code in codes:
            bars = bars_by_code.get(code)
            if bars is None or len(bars) < sc.min_history_days:
                continue

            enriched = enrich(bars)
            last = enriched.iloc[-1]

            # --- 足切り: 薄い銘柄・極端な株価は最初に落とす ---------------
            turnover = last.get("turnover_ma20")
            if pd.isna(turnover) or turnover < sc.min_turnover_jpy:
                skipped_liquidity += 1
                continue
            if not (sc.min_price <= last["close"] <= sc.max_price):
                skipped_price += 1
                continue

            for strategy in self.strategies:
                try:
                    sig = strategy.latest_signal(enriched, code, str(names.get(code, code)))
                except Exception:  # 1 銘柄の失敗で全体を止めない
                    log.exception("戦略 %s の評価に失敗: %s", strategy.name, code)
                    continue
                if sig is not None:
                    signals.append(sig)

        log.info(
            "シグナル %d 件（流動性で除外 %d 件 / 価格帯で除外 %d 件）",
            len(signals), skipped_liquidity, skipped_price,
        )
        return signals

    # ------------------------------------------------------------------ 永続化

    def persist(self, sized: list[SizedSignal]) -> list[str]:
        """通知対象を signals テーブルに pending として保存し、ID を返す。"""
        if not sized:
            return []

        ttl = timedelta(hours=self.cfg.risk.approval_ttl_hours)
        expires_at = now() + ttl
        ids: list[str] = []

        with self.db.connect() as conn:
            for item in sized:
                s = item.signal
                sid = signal_id(s.signal_date, s.code, s.strategy)
                ids.append(sid)
                conn.execute("DELETE FROM signals WHERE id = ?", [sid])
                conn.execute(
                    """
                    INSERT INTO signals (
                        id, signal_date, code, name, strategy, side,
                        ref_price, entry_price, stop_price, target_price,
                        quantity, risk_jpy, score, reasons, status, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    [
                        sid, s.signal_date, s.code, s.name, s.strategy, s.side,
                        s.ref_price, s.entry_price, s.stop_price, s.target_price,
                        item.quantity, item.risk_jpy, s.score, "|".join(s.reasons),
                        expires_at,
                    ],
                )
        return ids


def screen_and_size(cfg: AppConfig, db: Database) -> tuple[list[SizedSignal], list]:
    """スクリーニング → リスク制約適用 までを一括で行う。"""
    screener = Screener(cfg, db)
    raw = screener.run()
    risk = RiskManager(cfg.risk, db)
    accepted, rejections = risk.apply(raw)
    screener.persist(accepted)
    return accepted, rejections
