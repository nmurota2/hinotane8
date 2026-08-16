"""スクリーニング → 承認 → 擬似発注 → 決済 の一連の流れを通しで確認する。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from hinotane.backtest import run_backtest
from hinotane.broker import get_broker
from hinotane.broker.base import OrderRequest
from hinotane.pipeline import run_execute, run_mark
from hinotane.screener import Screener, screen_and_size


def test_screener_runs_over_seeded_universe(cfg, seeded_db):
    signals = Screener(cfg, seeded_db).run()
    # シグナルが 0 件でも異常ではないが、型と不変条件は守られていること
    for s in signals:
        assert s.stop_price < s.entry_price < s.target_price
        assert s.risk_per_share > 0
        assert s.reward_risk > 0


def test_screen_and_size_persists_pending_signals(cfg, seeded_db):
    accepted, _ = screen_and_size(cfg, seeded_db)
    rows = seeded_db.query("SELECT id, status, quantity FROM signals")
    assert len(rows) == len(accepted)
    if accepted:
        assert set(rows["status"]) == {"pending"}
        assert (rows["quantity"] > 0).all()
        assert set(rows["id"]) == {a.signal_key for a in accepted}


def test_live_trading_off_always_yields_paper_broker(cfg, seeded_db):
    from dataclasses import replace

    from hinotane.config import ExecutionConfig

    hostile = replace(cfg, execution=ExecutionConfig(live_trading=False, broker="tachibana"))
    broker = get_broker(hostile, seeded_db)
    assert broker.is_paper, "LIVE_TRADING=false なのに実発注アダプタが選ばれた"


def test_approval_to_execution_creates_position(cfg, seeded_db):
    accepted, _ = screen_and_size(cfg, seeded_db)
    if not accepted:
        return  # 合成データ次第でシグナルが出ないことはある

    sid = accepted[0].signal_key
    # 「翌営業日の始値」が必要なので、最終日より前の日付に付け替える
    dates = seeded_db.query("SELECT DISTINCT date FROM daily_quotes ORDER BY date")["date"]
    seeded_db.execute(
        "UPDATE signals SET signal_date = ?, status = 'approved', expires_at = ? WHERE id = ?",
        [dates.iloc[-5], datetime.now() + timedelta(hours=12), sid],
    )

    assert run_execute(cfg, seeded_db) >= 1
    positions = seeded_db.query("SELECT * FROM positions WHERE signal_id = ?", [sid])
    assert len(positions) == 1
    assert positions.iloc[0]["status"] == "open"
    assert bool(positions.iloc[0]["is_paper"])
    assert seeded_db.query("SELECT status FROM signals WHERE id = ?", [sid]).iloc[0]["status"] == "executed"


def test_expired_approval_is_not_executed(cfg, seeded_db):
    accepted, _ = screen_and_size(cfg, seeded_db)
    if not accepted:
        return
    sid = accepted[0].signal_key
    seeded_db.execute(
        "UPDATE signals SET status = 'approved', expires_at = ? WHERE id = ?",
        [datetime.now() - timedelta(hours=1), sid],
    )
    run_execute(cfg, seeded_db)
    status = seeded_db.query("SELECT status FROM signals WHERE id = ?", [sid]).iloc[0]["status"]
    assert status == "expired"
    assert seeded_db.query("SELECT * FROM positions WHERE signal_id = ?", [sid]).empty


def test_mark_closes_position_on_stop_hit(cfg, seeded_db):
    """損切り価格を必ず割るように仕込んで、決済されることを確認する。"""
    dates = seeded_db.query("SELECT DISTINCT date FROM daily_quotes ORDER BY date")["date"]
    entry_date = dates.iloc[-10]
    seeded_db.execute(
        """
        INSERT INTO positions (id, signal_id, code, name, strategy, side, quantity,
                               entry_date, entry_price, stop_price, target_price,
                               is_paper, status)
        VALUES ('pos1', NULL, '10000', 'テスト銘柄0', 'breakout', 'long', 100,
                ?, 1000000, 999999, 1000001, TRUE, 'open')
        """,
        [entry_date],
    )
    assert run_mark(cfg, seeded_db) == 1
    row = seeded_db.query("SELECT * FROM positions WHERE id = 'pos1'").iloc[0]
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop"
    assert pd.notna(row["pnl_jpy"])


def test_paper_buy_uses_next_open_not_signal_close(cfg, seeded_db):
    """当日終値で約定させると成績が実態より良く出るので、翌日始値であることを検証する。"""
    dates = seeded_db.query("SELECT DISTINCT date FROM daily_quotes ORDER BY date")["date"]
    signal_date = dates.iloc[-5]
    next_date = dates.iloc[-4]
    expected_open = seeded_db.query(
        "SELECT open FROM daily_quotes WHERE code = '10000' AND date = ?", [next_date]
    ).iloc[0]["open"]

    seeded_db.execute(
        """
        INSERT INTO signals (id, signal_date, code, name, strategy, side,
                             ref_price, entry_price, stop_price, target_price,
                             quantity, risk_jpy, score, reasons, status)
        VALUES ('sig1', ?, '10000', 'テスト銘柄0', 'breakout', 'long',
                1000, 1000, 900, 1200, 100, 10000, 1.0, 'test', 'approved')
        """,
        [signal_date],
    )
    broker = get_broker(cfg, seeded_db)
    result = broker.buy(OrderRequest(code="10000", name="テスト銘柄0", side="buy",
                                     quantity=100, signal_id="sig1"))
    assert result.ok
    # スリッページ 0.2% を足した値になっているはず
    assert result.filled_price == expected_open * (1 + cfg.execution.slippage_pct)


def test_backtest_produces_consistent_metrics(cfg, seeded_db):
    result = run_backtest(cfg, seeded_db, max_symbols=10)
    assert result.initial_equity == cfg.risk.equity_jpy
    assert 0.0 <= result.win_rate <= 1.0
    assert 0.0 <= result.max_drawdown <= 1.0
    for t in result.trades:
        # 損益と R 倍率の符号は必ず一致する
        assert (t.pnl_jpy > 0) == (t.r_multiple > 0) or t.pnl_jpy == 0
        assert t.exit_date >= t.entry_date
        assert t.exit_reason in {"stop", "target", "timeout"}


def test_backfill_skips_dates_already_fetched(cfg, seeded_db, monkeypatch):
    """数十分かかる処理なので、取得済みの日付は取りに行かないこと。

    ここが効かないと、途中で中断したときに最初からやり直しになる。
    """
    from hinotane import pipeline

    requested: list = []

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def daily_quotes_by_date(self, target):
            requested.append(target)
            return pd.DataFrame()

    monkeypatch.setattr(pipeline, "JQuantsClient", FakeClient)

    existing = set(
        seeded_db.query("SELECT DISTINCT date FROM daily_quotes")["date"].tolist()
    )
    assert existing, "前提: 既存データがあること"

    pipeline.backfill(cfg, seeded_db, years=2.0)

    assert requested, "取得対象が 1 日も無いのはおかしい"
    overlap = existing & set(requested)
    assert not overlap, f"取得済みの日付を再取得している: {sorted(overlap)[:3]}"
    # 土日は取引がないので要求しない
    assert all(d.weekday() < 5 for d in requested)


def test_backfill_is_a_noop_when_everything_is_present(cfg, seeded_db, monkeypatch):
    from hinotane import pipeline

    calls: list = []

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def daily_quotes_by_date(self, target):
            calls.append(target)
            return pd.DataFrame()

    monkeypatch.setattr(pipeline, "JQuantsClient", FakeClient)
    # 合成データの範囲だけを対象にすれば、全日付が取得済みになる
    pipeline.backfill(cfg, seeded_db, years=0.0)
    assert calls == []
