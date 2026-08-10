from __future__ import annotations

from datetime import date

import pytest

from hinotane.config import RiskConfig
from hinotane.risk import RiskManager
from hinotane.strategies.base import StrategySignal


def make_signal(code="10000", entry=1000.0, stop=950.0, target=1150.0, score=1.0, strategy="breakout"):
    return StrategySignal(
        code=code,
        name="テスト",
        signal_date=date(2025, 6, 2),
        strategy=strategy,
        ref_price=entry,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        score=score,
        reasons=["テスト"],
    )


def test_position_size_follows_fixed_fractional_risk(db):
    # 資金100万 × リスク1% = 1万円まで許容。1株あたり損失50円 → 200株 → 100株単位で200株
    cfg = RiskConfig(equity_jpy=1_000_000, risk_per_trade=0.01, max_position_pct=1.0)
    sized = RiskManager(cfg, db).size(make_signal(entry=1000, stop=950))
    assert sized is not None
    assert sized.quantity == 200
    assert sized.risk_jpy == pytest.approx(10_000)
    assert sized.cost_jpy == pytest.approx(200_000)


def test_position_capped_by_max_position_pct(db):
    # 損切り幅が狭いと数量が膨らむが、1銘柄あたり上限で頭打ちになる
    cfg = RiskConfig(equity_jpy=1_000_000, risk_per_trade=0.01, max_position_pct=0.10)
    sized = RiskManager(cfg, db).size(make_signal(entry=1000, stop=995))
    assert sized is not None
    assert sized.cost_jpy <= 100_000


def test_rejects_when_stop_too_wide_for_one_lot(db):
    # 1株あたり損失が大きすぎて100株買えない
    cfg = RiskConfig(equity_jpy=100_000, risk_per_trade=0.01, max_position_pct=1.0)
    assert RiskManager(cfg, db).size(make_signal(entry=10_000, stop=5_000)) is None


def test_deduplicates_same_code_keeping_highest_score(db):
    cfg = RiskConfig(equity_jpy=1_000_000, max_signals_per_day=5, max_position_pct=1.0)
    signals = [
        make_signal(code="10000", score=1.0, strategy="breakout"),
        make_signal(code="10000", score=9.0, strategy="pullback"),
        make_signal(code="20000", score=5.0, strategy="breakout"),
    ]
    accepted, rejections = RiskManager(cfg, db).apply(signals)
    assert [a.code for a in accepted] == ["10000", "20000"]
    # 採用されたのはスコアの高い pullback のほう
    assert accepted[0].signal.strategy == "pullback"
    assert any("重複" in r.reason for r in rejections)


def test_respects_max_signals_per_day(db):
    cfg = RiskConfig(equity_jpy=10_000_000, max_signals_per_day=2, max_position_pct=1.0)
    signals = [make_signal(code=f"{i}0000", score=float(i)) for i in range(1, 6)]
    accepted, _ = RiskManager(cfg, db).apply(signals)
    assert len(accepted) == 2


def test_skips_codes_already_held(db):
    db.execute(
        """
        INSERT INTO positions (id, code, name, side, quantity, status, is_paper)
        VALUES ('p1', '10000', 'テスト', 'long', 100, 'open', TRUE)
        """
    )
    cfg = RiskConfig(equity_jpy=1_000_000, max_position_pct=1.0)
    accepted, rejections = RiskManager(cfg, db).apply([make_signal(code="10000")])
    assert accepted == []
    assert any("既に保有中" in r.reason for r in rejections)


def test_kill_switch_blocks_everything_after_drawdown(db):
    # 資金100万・上限15% → 15万円のドローダウンで停止
    for i, pnl in enumerate([-80_000, -80_000]):
        db.execute(
            """
            INSERT INTO positions (id, code, name, side, quantity, status,
                                   is_paper, pnl_jpy, exit_date)
            VALUES (?, '90000', 'テスト', 'long', 100, 'closed', TRUE, ?, ?)
            """,
            [f"c{i}", pnl, date(2025, 5, 1 + i)],
        )
    cfg = RiskConfig(equity_jpy=1_000_000, kill_switch_drawdown_pct=0.15)
    manager = RiskManager(cfg, db)
    halted, why = manager.kill_switch_active()
    assert halted
    assert "ドローダウン" in why

    accepted, rejections = manager.apply([make_signal()])
    assert accepted == []
    assert "キルスイッチ" in rejections[0].reason


def test_max_open_positions_blocks_new_entries(db):
    for i in range(3):
        db.execute(
            """
            INSERT INTO positions (id, code, name, side, quantity, status, is_paper)
            VALUES (?, ?, 'テスト', 'long', 100, 'open', TRUE)
            """,
            [f"p{i}", f"{i}1000"],
        )
    cfg = RiskConfig(equity_jpy=1_000_000, max_open_positions=3)
    accepted, rejections = RiskManager(cfg, db).apply([make_signal(code="99000")])
    assert accepted == []
    assert "上限" in rejections[0].reason
