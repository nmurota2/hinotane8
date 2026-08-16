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
        assert t.exit_reason in {"stop", "target", "timeout", "期末"}


def _fake_backfill(cfg, db, monkeypatch, years=2.0):
    """backfill を実際の通信なしで走らせ、要求された日付を返す。"""
    from hinotane import pipeline

    requested: list = []

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def daily_quotes_by_date(self, target):
            requested.append(target)
            return pd.DataFrame()

    monkeypatch.setattr(pipeline, "JQuantsClient", FakeClient)
    pipeline.backfill(cfg, db, years=years)
    return requested


def test_backfill_skips_dates_already_fetched(cfg, seeded_db, monkeypatch):
    """取得済みの日付は取りに行かないこと。

    回帰テスト: DuckDB の DATE 列は pandas.Timestamp で返る。
    date と Timestamp を比較すると常に不一致になり、スキップが黙って
    効かなくなる（実機で「取得済み9日」と言いながら全521日を取りに行った）。
    期待値を明示的に datetime.date で作り、実装側の型変換に依存せず検証する。
    """
    from datetime import date as date_type
    from datetime import timedelta

    from hinotane.config import today

    raw = seeded_db.query("SELECT DISTINCT date FROM daily_quotes")["date"]
    existing = set(pd.to_datetime(raw).dt.date)
    assert existing and all(isinstance(d, date_type) for d in existing)

    range_start = today() - timedelta(days=730)
    overlapping = {d for d in existing if d >= range_start and d.weekday() < 5}
    assert overlapping, "前提: 既存データが対象期間と重なっていること"

    requested = _fake_backfill(cfg, seeded_db, monkeypatch)

    assert requested and all(isinstance(d, date_type) for d in requested)
    assert not (overlapping & set(requested)), (
        f"取得済みの日付を再取得している: {sorted(overlapping & set(requested))[:3]}"
    )
    assert all(d.weekday() < 5 for d in requested), "土日は取引がないので要求しない"


def test_backfill_request_count_reflects_the_skip(cfg, seeded_db, monkeypatch):
    """スキップした日数のぶん、実際の取得対象が減っていること。

    ログ上は「スキップします」と出るのに全日程を取りに行く、
    という食い違いを防ぐ。
    """
    from datetime import timedelta

    from hinotane.config import today

    raw = seeded_db.query("SELECT DISTINCT date FROM daily_quotes")["date"]
    existing = set(pd.to_datetime(raw).dt.date)

    run_date = today()
    range_start = run_date - timedelta(days=730)
    business_days = {
        range_start + timedelta(days=i)
        for i in range((run_date - range_start).days + 1)
        if (range_start + timedelta(days=i)).weekday() < 5
    }
    expected = business_days - existing

    requested = _fake_backfill(cfg, seeded_db, monkeypatch)

    assert set(requested) == expected
    assert len(requested) < len(business_days), "1 日もスキップできていない"


def test_backfill_stops_at_the_subscription_boundary(cfg, seeded_db, monkeypatch):
    """契約範囲を超えたら打ち切ること。

    実機では範囲外の 60 日ぶんを 16 秒間隔で叩き続け、約16分を空費した。
    その先も必ず同じ結果になるので、1 回分かった時点で止める。
    """
    from datetime import date as date_type

    from hinotane import pipeline
    from hinotane.datasource.jquants import JQuantsOutOfRangeError

    boundary = date_type(2025, 6, 1)
    requested: list = []

    class FakeClient:
        def __init__(self, _cfg):
            pass

        def daily_quotes_by_date(self, target):
            requested.append(target)
            if target > boundary:
                raise JQuantsOutOfRangeError(
                    "範囲外", date_type(2024, 5, 24), boundary
                )
            return pd.DataFrame()

    monkeypatch.setattr(pipeline, "JQuantsClient", FakeClient)
    pipeline.backfill(cfg, seeded_db, years=2.0)

    beyond = [d for d in requested if d > boundary]
    assert len(beyond) == 1, (
        f"境界を越えたあとも {len(beyond)} 回要求している（1 回で打ち切るべき）"
    )


def test_signal_on_the_latest_bar_survives_until_data_arrives(cfg, seeded_db):
    """当日のシグナルを執行しようとして株価がまだ無いとき、失敗で確定させないこと。

    本番のスケジュールでは、screen が引け後に走るのでシグナル日は必ず DB の
    最終バーの日付になる。その状態で execute を呼ぶと「翌営業日の始値」は
    まだ存在しない。ここを 'failed' で確定させると、'failed' は執行対象の
    ステータスから外れているため **承認したシグナルが二度と発注されず黙って
    消える**。実際にこの状態で本番スケジュールが組まれていた。
    """
    accepted, _ = screen_and_size(cfg, seeded_db)
    if not accepted:
        return

    sid = accepted[0].signal_key
    dates = seeded_db.query("SELECT DISTINCT date FROM daily_quotes ORDER BY date")["date"]
    last_bar = dates.iloc[-1]
    seeded_db.execute(
        "UPDATE signals SET signal_date = ?, status = 'approved', expires_at = ? WHERE id = ?",
        [last_bar, datetime.now() + timedelta(hours=12), sid],
    )

    assert run_execute(cfg, seeded_db) == 0
    status = seeded_db.query("SELECT status FROM signals WHERE id = ?", [sid]).iloc[0]["status"]
    assert status == "approved", (
        f"株価待ちのシグナルが '{status}' になっている。"
        " 承認済みのまま残さないと、翌日の execute が拾えず永久に発注されない。"
    )
    assert seeded_db.query("SELECT * FROM positions WHERE signal_id = ?", [sid]).empty
