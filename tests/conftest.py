from __future__ import annotations

import pandas as pd
import pytest
from factories import synthetic_bars

from hinotane.config import (
    AppConfig,
    ExecutionConfig,
    JQuantsConfig,
    LineConfig,
    RiskConfig,
    ScreenerConfig,
)
from hinotane.db import Database


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.duckdb")
    database.init_schema()
    return database


@pytest.fixture
def seeded_db(db: Database) -> Database:
    """5 銘柄ぶんの合成データを投入した DB。"""
    listed = pd.DataFrame(
        [
            {
                "code": f"1000{i}",
                "name": f"テスト銘柄{i}",
                "market_code": "0111",
                "sector17_code": "1",
                "sector33_code": "1",
                "scale_category": "TOPIX Mid400",
            }
            for i in range(5)
        ]
    )
    db.upsert_listed(listed)
    for i in range(5):
        db.upsert_quotes(synthetic_bars(f"1000{i}", seed=i, trend=0.002 if i % 2 == 0 else -0.001))
    return db


@pytest.fixture
def cfg(tmp_path) -> AppConfig:
    return AppConfig(
        db_path=tmp_path / "test.duckdb",
        jquants=JQuantsConfig(api_key="dummy"),
        line=LineConfig(),
        risk=RiskConfig(
            equity_jpy=1_000_000,
            risk_per_trade=0.01,
            max_position_pct=0.3,
            max_open_positions=5,
            max_signals_per_day=3,
        ),
        screener=ScreenerConfig(
            strategies=["breakout", "pullback", "reversal"],
            min_turnover_jpy=0,
            min_price=1,
            max_price=1_000_000,
            min_history_days=120,
            market_codes=["0111"],
        ),
        execution=ExecutionConfig(live_trading=False, broker="paper"),
    )
