"""バックテスターが「未来を覗いていない」ことを検証する。

このプロジェクトで最も重要なテスト。

戦略が儲かるかどうかは市場次第で保証できないが、**バックテスターが
ありもしない利益を作り出さないこと**は保証できる。ここが壊れていると、
どんな戦略でも「勝てる」と表示され、その数字を信じて実弾を入れることになる。

実際に起きたこと:
    流動性で銘柄を絞る際、検証期間の **末尾** 60 本の売買代金で判定していた。
    売買代金は株価×出来高なので、これは「期間の終わりに値上がりしていた銘柄」を
    期間の初めから売買することに等しい。完全なノイズでも +0.15R の期待値が出た。

対策:
    上がりも下がりもしない乱数データを流し、期待値がプラスにならないことを確認する。
    ノイズから利益が出るなら、それは戦略の実力ではなく実装の欠陥である。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from hinotane.backtest import run_backtest
from hinotane.config import ExecutionConfig, RiskConfig, ScreenerConfig
from hinotane.db import Database


def _driftless_market(db: Database, n_codes: int = 120, n_days: int = 320, seed: int = 7) -> None:
    """上がりも下がりもしない市場を作る。

    価格は幾何ブラウン運動だが、**算術平均リターンがゼロになるよう**
    log ドリフトを -σ²/2 に設定する。ここを 0 にすると価格自体は
    σ²/2 だけ上に漂い、買い戦略が有利になってしまう。
    """
    rng = np.random.default_rng(seed)
    sigma = 0.018
    dates = pd.bdate_range("2024-01-01", periods=n_days).date

    codes = [f"{1000 + i}0" for i in range(n_codes)]
    db.upsert_listed(
        pd.DataFrame(
            {
                "code": codes,
                "name": [f"銘柄{i}" for i in range(n_codes)],
                "market_code": "0111",
                "sector17_code": "1",
                "sector33_code": "1",
                "scale_category": "TOPIX Mid400",
            }
        )
    )

    steps = rng.normal(-(sigma**2) / 2, sigma, (n_codes, n_days))
    close = 1500 * np.exp(np.cumsum(steps, axis=1))
    open_ = close * (1 + rng.normal(0, 0.004, close.shape))
    high = np.maximum(close, open_) * (1 + np.abs(rng.normal(0, 0.007, close.shape)))
    low = np.minimum(close, open_) * (1 - np.abs(rng.normal(0, 0.007, close.shape)))
    volume = rng.integers(300_000, 3_000_000, close.shape).astype(float)

    db.upsert_quotes(
        pd.DataFrame(
            {
                "code": np.repeat(np.array(codes), n_days),
                "date": np.tile(dates, n_codes),
                "open": open_.ravel(),
                "high": high.ravel(),
                "low": low.ravel(),
                "close": close.ravel(),
                "volume": volume.ravel(),
                "turnover_value": (close * volume).ravel(),
            }
        )
    )


@pytest.fixture
def noise_cfg(cfg):
    return replace(
        cfg,
        risk=RiskConfig(equity_jpy=1_000_000, risk_per_trade=0.01, max_position_pct=0.3),
        screener=ScreenerConfig(
            # ⚠️ 全戦略を対象にする。ここに載っていない戦略は、
            # プロジェクトで最も重要なこの検査を通っていないことになる。
            strategies=["breakout", "pullback", "reversal", "trend"],
            min_turnover_jpy=0,
            min_price=1,
            max_price=1_000_000,
            min_history_days=120,
            market_codes=["0111"],
        ),
        execution=ExecutionConfig(live_trading=False, broker="paper"),
    )


def test_noise_market_is_not_profitable(noise_cfg, db):
    """ノイズから利益を作り出していないこと。

    ここが失敗したら、バックテストの数字はすべて信用できない。
    戦略を疑う前に、まず実装を疑うこと。
    """
    _driftless_market(db)
    result = run_backtest(noise_cfg, db, label="ノイズ市場", max_symbols=60)

    assert result.trades, "取引が 1 件も発生せず、検証になっていない"
    assert result.expectancy_r <= 0.02, (
        f"上がりも下がりもしない市場で期待値 {result.expectancy_r:+.2f}R が出ている。"
        " 実装が未来を覗いている疑いが濃厚（過去に流動性フィルタで発生）。"
    )


def test_symbol_selection_uses_only_information_available_at_the_start(noise_cfg, db):
    """銘柄の絞り込みに、検証期間の後半の情報を使っていないこと。

    「期間の終わりに流動性が高かった銘柄」で絞ると、売買代金＝株価×出来高
    である以上、値上がりした銘柄を選ぶことになる。
    """
    from hinotane.backtest import _build_signal_table

    n_days = 320
    dates = pd.bdate_range("2024-01-01", periods=n_days).date
    rng = np.random.default_rng(1)

    # early: 最初から最後まで一貫して流動性が高い
    # late : 前半は閑散だが、後半に急騰して流動性が上がる（＝後知恵でしか選べない）
    ramp = np.concatenate([np.ones(n_days // 2), np.linspace(1, 50, n_days - n_days // 2)])
    rows = []
    for code, factor in (("10000", np.full(n_days, 10.0)), ("20000", ramp)):
        close = 1000 * factor
        volume = np.full(n_days, 1_000_000.0)
        rows.append(
            pd.DataFrame(
                {
                    "code": code,
                    "date": dates,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": volume,
                    "turnover_value": close * volume,
                }
            )
        )
    db.upsert_listed(
        pd.DataFrame(
            {
                "code": ["10000", "20000"],
                "name": ["一貫して厚い", "後半だけ厚い"],
                "market_code": "0111",
                "sector17_code": "1",
                "sector33_code": "1",
                "scale_category": "TOPIX Mid400",
            }
        )
    )
    db.upsert_quotes(pd.concat(rows, ignore_index=True))
    assert rng  # 乱数は使わないが、意図しない非決定性を持ち込まないことの明示

    _, prices, _ = _build_signal_table(
        noise_cfg, db, ["breakout"], max_symbols=1
    )

    assert set(prices) == {"10000"}, (
        "検証期間の後半に流動性が上がった銘柄を選んでいる。"
        " 開始時点で知り得ない情報で銘柄を選別している。"
    )


# ------------------------------------------------------------ 判定ロジック


def _result(
    *,
    net_pnl: float,
    expectancy: float,
    pf: float,
    n_trades: int = 50,
    forced: int = 0,
    top3: float = 0.3,
    bench: float = 0.0,
    bench_dd: float = 0.5,
    max_dd: float = 0.1,
):
    """判定に必要な指標だけを持つスタブ。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        trades=list(range(n_trades)),
        net_pnl_jpy=net_pnl,
        expectancy_r=expectancy,
        profit_factor=pf,
        total_return=net_pnl / 1_000_000,
        forced_exits=forced,
        top3_profit_share=top3,
        benchmark_curve=[1.0, 1.0 + bench],
        benchmark_return=bench,
        benchmark_max_drawdown=bench_dd,
        max_drawdown=max_dd,
    )


def _report(in_s, out_s, *, in_window: int = 400, max_hold: int = 20):
    """WalkForwardReport のスタブ。judge が見る構造情報だけを持つ。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        full=out_s,
        in_sample=in_s,
        out_sample=out_s,
        boundary=None,
        in_window_days=in_window,
        out_window_days=in_window,
        warmup_bars=80,
        max_holding_days=max_hold,
    )


def test_losing_strategy_is_never_approved():
    """お金が減っているものを合格にしないこと。

    以前は期待値（R の単純平均）だけを見ていたため、
    「両期間とも資産が減っているのに ✅」という表示が出た。
    R はリスク額で割った比率なので、取引ごとにリスク額がばらつくと
    円の損益と符号が食い違いうる。
    """
    from hinotane.backtest import FAIL, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=-13_000, expectancy=0.07, pf=0.99),
            _result(net_pnl=-53_000, expectancy=0.11, pf=0.84),
        )
    )
    assert verdict == FAIL, f"資産が減っているのに合格になった: {lines}"
    text = "\n".join(lines)
    assert "❌" in text
    assert "資産が減っています" in text


def test_profit_factor_below_one_is_rejected():
    from hinotane.backtest import FAIL, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=100_000, expectancy=0.3, pf=1.5),
            _result(net_pnl=1_000, expectancy=0.01, pf=0.9),
        )
    )
    assert verdict == FAIL
    assert any("プロフィットファクター" in line for line in lines)


def test_overfitting_is_flagged():
    """前半だけ強く後半で崩れる戦略は落とすこと。"""
    from hinotane.backtest import FAIL, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=300_000, expectancy=0.80, pf=2.5),
            _result(net_pnl=5_000, expectancy=0.05, pf=1.05),
        )
    )
    assert verdict == FAIL
    assert any("過剰最適化" in line for line in lines)


def test_genuinely_profitable_strategy_passes():
    from hinotane.backtest import PASS, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=180_000, expectancy=0.35, pf=1.8),
            _result(net_pnl=150_000, expectancy=0.31, pf=1.7),
        )
    )
    assert verdict == PASS, lines
    text = "\n".join(lines)
    assert "✅" in text
    assert "生存者バイアス" in text, "合格時こそ限界を明示すべき"


def test_too_few_trades_is_warned():
    from hinotane.backtest import judge

    _verdict, lines = judge(
        _report(
            _result(net_pnl=180_000, expectancy=0.35, pf=1.8),
            _result(net_pnl=150_000, expectancy=0.31, pf=1.7, n_trades=12),
        )
    )
    assert any("偶然の影響" in line for line in lines)


def test_expectancy_sign_always_matches_the_money(noise_cfg, db):
    """期待値の符号が、実際の円の損益と必ず一致すること。"""
    _driftless_market(db)
    r = run_backtest(noise_cfg, db, label="符号の一致", max_symbols=60)
    assert r.trades
    assert (r.expectancy_r > 0) == (r.net_pnl_jpy > 0), (
        f"期待値 {r.expectancy_r:+.3f}R と損益 {r.net_pnl_jpy:+,.0f}円 の符号が食い違っている"
    )


# ------------------------------------------------- 期末に残った建玉の扱い


def _rising_market(db: Database, n_codes: int = 40, n_days: int = 340, seed: int = 3) -> None:
    """はっきりと右肩上がりの市場。順張りが建玉を持ち越す状況を作る。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days).date
    codes = [f"{3000 + i}0" for i in range(n_codes)]
    db.upsert_listed(
        pd.DataFrame(
            {
                "code": codes,
                "name": [f"上昇{i}" for i in range(n_codes)],
                "market_code": "0111",
                "sector17_code": "1",
                "sector33_code": "1",
                "scale_category": "TOPIX Mid400",
            }
        )
    )
    steps = rng.normal(0.0018, 0.012, (n_codes, n_days))
    close = 1500 * np.exp(np.cumsum(steps, axis=1))
    open_ = close * (1 + rng.normal(0, 0.003, close.shape))
    high = np.maximum(close, open_) * (1 + np.abs(rng.normal(0, 0.005, close.shape)))
    low = np.minimum(close, open_) * (1 - np.abs(rng.normal(0, 0.005, close.shape)))
    volume = rng.integers(300_000, 3_000_000, close.shape).astype(float)
    db.upsert_quotes(
        pd.DataFrame(
            {
                "code": np.repeat(np.array(codes), n_days),
                "date": np.tile(dates, n_codes),
                "open": open_.ravel(),
                "high": high.ravel(),
                "low": low.ravel(),
                "close": close.ravel(),
                "volume": volume.ravel(),
                "turnover_value": (close * volume).ravel(),
            }
        )
    )


def test_positions_held_at_the_end_are_not_dropped(noise_cfg, db):
    """検証期間の終わりに残った建玉を、集計から取りこぼさないこと。

    集計（確定損益・プロフィットファクター・期待値）は決済済みの取引しか
    数えない。期末の建玉を放置すると、**伸びている勝ち馬だけが集計から消え、
    損切りされた負けは全部数えられる**という偏りが生まれる。
    保有期間の長い順張り戦略ほど不当に悪く見え、
    本当は機能している戦略を捨てることになる。
    """
    _rising_market(db)
    cfg = replace(
        noise_cfg, screener=replace(noise_cfg.screener, strategies=["trend"])
    )
    result = run_backtest(cfg, db, label="上昇市場", max_symbols=40)

    assert result.trades, "取引が 1 件も発生せず、検証になっていない"
    assert any(t.exit_reason == "期末" for t in result.trades), (
        "右肩上がりの市場で順張りが 1 件も持ち越していない。テストの前提が崩れている。"
    )
    assert result.net_pnl_jpy == pytest.approx(
        result.final_equity - result.initial_equity, abs=1.0
    ), (
        f"確定損益 {result.net_pnl_jpy:+,.0f}円 と資産の増減"
        f" {result.final_equity - result.initial_equity:+,.0f}円 が一致しない。"
        " 集計されていない建玉が残っている。"
    )


# ------------------------------------------------ 「判定できなかった」の扱い


def test_short_in_sample_window_is_undetermined_not_pass():
    """前半が短すぎて過剰最適化を検査できないとき、合格にしないこと。

    ここが設計の要。以前は bool を返していたので、検査できなかった項目が
    あっても ✅ が出た。✅ は「実運用に載せてよい」の合図として読まれるため、
    未検査を ✅ に混ぜるのは注意書きを添えても免罪符にしかならない。

    発動条件は **取引数ではなく期間の構造** で決めること。取引数は回して
    みないと分からない出力なので、それを条件にすると「たまたま取引が
    少なかった」あらゆる戦略が唯一の過剰最適化チェックを回避できてしまう。
    """
    from hinotane.backtest import PASS, UNDETERMINED, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=60_000, expectancy=0.82, pf=14.2, n_trades=10),
            _result(net_pnl=135_000, expectancy=0.41, pf=2.19, n_trades=39),
            in_window=94,      # 前半の売買可能期間
            max_hold=90,       # 最大保有。94 < 90×2 なので売買が 1 巡もしない
        )
    )
    assert verdict != PASS, f"検証できていないものを合格にした: {lines}"
    assert verdict == UNDETERMINED
    text = "\n".join(lines)
    assert "✅" not in text, "判定保留で ✅ を出してはいけない"
    assert "未検査" in text
    assert "1 巡もしません" in text


def test_enough_window_but_few_trades_is_also_undetermined():
    """期間は足りていても取引数が少なければ、期待値は推定できない。"""
    from hinotane.backtest import PASS, UNDETERMINED, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=60_000, expectancy=0.82, pf=14.2, n_trades=10),
            _result(net_pnl=135_000, expectancy=0.41, pf=2.19, n_trades=39),
            in_window=400,
            max_hold=20,
        )
    )
    assert verdict == UNDETERMINED, lines
    assert verdict != PASS
    assert any("推定できません" in line for line in lines)


def test_overfitting_check_still_fires_with_enough_data():
    """標本が足りているときは、従来どおり過剰最適化で落とすこと。

    「判定保留」を入れたことで、大標本の過剰最適化まで見逃すようになっては
    本末転倒。免除が効くのは標本不足のときだけであることを固定する。
    """
    from hinotane.backtest import FAIL, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=300_000, expectancy=0.80, pf=2.5, n_trades=60),
            _result(net_pnl=5_000, expectancy=0.05, pf=1.05, n_trades=60),
            in_window=400,
            max_hold=20,
        )
    )
    assert verdict == FAIL
    assert any("過剰最適化" in line for line in lines)


def test_losing_to_buy_and_hold_on_both_axes_is_rejected():
    """買い持ちにリターンでもドローダウンでも負けるなら、売買する意味がない。"""
    from hinotane.backtest import FAIL, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=100_000, expectancy=0.3, pf=1.5, n_trades=60),
            _result(
                net_pnl=50_000, expectancy=0.2, pf=1.4, n_trades=60,
                bench=0.40, bench_dd=0.10, max_dd=0.25,
            ),
            in_window=400,
        )
    )
    assert verdict == FAIL
    assert any("買い持ち" in line for line in lines)


def test_profit_concentrated_in_a_few_trades_is_rejected():
    """利益が数銘柄に集中しているものは、仕組みではなく当たりの記録。"""
    from hinotane.backtest import FAIL, judge

    verdict, lines = judge(
        _report(
            _result(net_pnl=100_000, expectancy=0.3, pf=1.5, n_trades=60),
            _result(net_pnl=150_000, expectancy=0.4, pf=2.0, n_trades=60, top3=0.85),
            in_window=400,
        )
    )
    assert verdict == FAIL
    assert any("上位 3 取引" in line for line in lines)


# --------------------------------------------------------- 決済の約定モデル


def test_exits_fill_at_the_next_open_not_at_the_stop_price(noise_cfg, db):
    """決済は「損切り価格ちょうど」ではなく、判定した翌営業日の始値であること。

    本番の `hinotane mark` は引け後 16:10 に走る。判定した時点で場は終わって
    いるので、損切り価格ちょうどで約定させるには逆指値注文を市場に置いておく
    必要があるが、その仕組みはまだ無い（OrderRequest に逆指値の欄が無く、
    立花証券のアダプタも未実装）。

    ここが崩れると **出せない注文を前提に成績を計算する**ことになる。
    トレーリングストップの戦略は利益のほぼ全てがストップ決済由来なので、
    影響は成績全体に及ぶ。
    """
    _driftless_market(db)
    result = run_backtest(noise_cfg, db, label="約定モデル", max_symbols=60)
    assert result.trades, "取引が 1 件も発生せず、検証になっていない"

    slip = noise_cfg.execution.slippage_pct
    checked = 0
    for t in result.trades:
        if t.exit_reason == "期末":
            continue  # 期末の打ち切りだけは終値で手仕舞う
        bars = db.query(
            "SELECT open FROM daily_quotes WHERE code = ? AND date = ?",
            [t.code, t.exit_date],
        )
        assert not bars.empty
        expected = float(bars.iloc[0]["open"]) * (1 - slip)
        assert t.exit_price == pytest.approx(expected, rel=1e-9), (
            f"{t.code} の決済価格 {t.exit_price:.2f} が決済日の始値 {expected:.2f} と違う。"
            " 損切り価格ちょうどで約定させている疑い（本番では出せない注文）。"
        )
        checked += 1
    assert checked > 0, "期末以外の決済が 1 件も無く、検証になっていない"


def test_position_sizing_does_not_compound(noise_cfg, db):
    """数量の計算に、増えた利益を再投資しないこと。

    本番の RiskManager は cfg.risk.equity_jpy（固定）を見ている。
    バックテストだけ実現損益で複利にすると、リターンが実態より大きく、
    ドローダウンが小さく出る。
    """
    _rising_market(db)
    cfg = replace(noise_cfg, screener=replace(noise_cfg.screener, strategies=["trend"]))
    result = run_backtest(cfg, db, label="固定サイジング", max_symbols=40)
    assert result.trades

    cap = cfg.risk.equity_jpy * cfg.risk.max_position_pct
    for t in result.trades:
        assert t.entry_price * t.quantity <= cap * 1.001, (
            f"{t.code} の建玉 {t.entry_price * t.quantity:,.0f}円 が"
            f" 1銘柄あたりの上限 {cap:,.0f}円 を超えている。"
            " 増えた資金を基準に数量を計算している（複利）疑い。"
        )
