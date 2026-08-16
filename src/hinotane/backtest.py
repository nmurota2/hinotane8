"""バックテスト。

**本番と同じ `Strategy.evaluate()` を呼ぶ**のがこのモジュールの肝。
バックテスト専用にロジックを書き直すと、検証結果と実運用が食い違う——
個人開発の自動売買が失敗する最大の原因がこれ。ここでは構造的に避けている。

約定の扱いも本番（PaperBroker）と揃えてある:
  - エントリー: シグナル翌営業日の **始値** × (1 + スリッページ)
  - 数量      : **シグナル日の終値** を基準に、固定の運用資金から計算
  - 決済判定  : 日足の **高値・安値** で損切り/利確の到達を判定
  - 決済約定  : 判定した **翌営業日の始値** × (1 - スリッページ)
  - 同じ日に損切りと利確の両方に触れたら **損切り側** を採用（保守的）

⚠️ 決済が「損切り価格ちょうど」ではなく翌営業日の寄りである理由
本番の `hinotane mark` は引け後 16:10 に走る。判定した時点で場は終わっており、
出せる注文は翌朝の寄り成行だけ。逆指値注文を市場に置いておく仕組みは
まだ無い（OrderRequest に逆指値の欄が無く、立花証券の実装も未完）。
損切り価格ちょうどで約定すると仮定すると、**出せない注文を前提に成績を
計算する**ことになる。逆指値を実装したら、ここも同時に直すこと。

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
    #: 同じ期間・同じ銘柄を等ウェイトで買い持ちした場合の資産推移。
    #: 「戦略のおかげで儲かったのか、相場が上げただけなのか」を切り分ける。
    benchmark_curve: pd.Series = field(default_factory=pd.Series)
    #: 候補を見送った理由ごとの件数。
    #: 「順位づけが効いていない」ように見えるとき、本当の原因が
    #: 「そもそも買える候補が枠より少ない」ことがある。それを見分けるために要る。
    skips: dict = field(default_factory=dict)

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
        """この期間に **決済した** 取引の損益合計。

        ⚠️ 期間を切り出した結果ではこれを「その期間に稼いだ額」と読んではいけない。
        期間をまたいだ建玉の利益は、前の期間で積み上がった含み益であっても
        決済した期間に全額が計上される。実際に口座が増えた額は period_pnl_jpy。
        """
        return sum(t.pnl_jpy for t in self.trades)

    @property
    def period_pnl_jpy(self) -> float:
        """この期間に **実際に口座が増減した** 額（含み損益の変化を含む）。

        期間をまたいだ建玉があると net_pnl_jpy とずれる。ずれたときは
        こちらが本物。合否はこの額で判断する。
        """
        return self.final_equity - self.initial_equity

    def expectancy_ci(
        self, *, confidence: float = 0.95, n_boot: int = 2000, seed: int = 12345
    ) -> tuple[float, float]:
        """期待値の信頼区間をブートストラップで求める。

        点推定の +0.14R が「本当に優位性がある」のか「たまたまそう出た」のかは、
        数字ひとつでは区別できない。取引を復元抽出し直して期待値を作り直す作業を
        繰り返し、そのばらつきを見る。**区間がゼロをまたぐなら、優位性は
        偶然と区別できていない。**

        ⚠️ この区間は楽観的すぎる。同時に複数の建玉を持つので取引同士が独立でなく、
        同じ相場の動きを共有している。実際のばらつきはこれより大きい。
        """
        if not self.trades:
            return (0.0, 0.0)
        pnl = np.array([t.pnl_jpy for t in self.trades], dtype=float)
        risk = np.array([t.risk_jpy for t in self.trades], dtype=float)
        if risk.sum() <= 0:
            return (0.0, 0.0)
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(pnl), size=(n_boot, len(pnl)))
        boot_risk = risk[idx].sum(axis=1)
        boot = np.divide(
            pnl[idx].sum(axis=1),
            boot_risk,
            out=np.zeros(n_boot),
            where=boot_risk > 0,
        )
        tail = (1.0 - confidence) / 2.0 * 100
        return float(np.percentile(boot, tail)), float(np.percentile(boot, 100 - tail))

    @property
    def carried_in_trades(self) -> int:
        """この期間より前に建てられた建玉の件数。

        多いほど、この期間の勝率・期待値・プロフィットファクターは
        「前の期間で積み上がった含み益」を自分の手柄として数えている。
        """
        return sum(1 for t in self.trades if t.entry_date < self.start)

    @property
    def avg_risk_jpy(self) -> float:
        """1 取引あたりの実際のリスク額。設定値より大幅に小さければ
        1 銘柄あたりの投資上限に頭を抑えられている。"""
        if not self.trades:
            return 0.0
        return float(np.mean([t.risk_jpy for t in self.trades]))

    @property
    def max_drawdown(self) -> float:
        return _max_drawdown(self.equity_curve)

    @property
    def benchmark_return(self) -> float:
        """同じ期間・同じ銘柄を等ウェイトで買い持ちした場合のリターン。"""
        if len(self.benchmark_curve) < 2:
            return 0.0
        first = float(self.benchmark_curve.iloc[0])
        if first <= 0:
            return 0.0
        return float(self.benchmark_curve.iloc[-1]) / first - 1.0

    @property
    def benchmark_max_drawdown(self) -> float:
        return _max_drawdown(self.benchmark_curve)

    @property
    def forced_exits(self) -> int:
        """検証期間の打ち切りによる決済の件数。戦略が決めた決済ではない。"""
        return sum(1 for t in self.trades if t.exit_reason == "期末")

    @property
    def top3_profit_share(self) -> float:
        """総利益のうち、上位 3 取引が占める割合。

        これが高いものは「仕組み」ではなく「数銘柄がたまたま当たった」記録。
        その数銘柄を抜けば成績は一気に崩れるので、再現性がない。
        """
        gains = sorted((t.pnl_jpy for t in self.trades if t.pnl_jpy > 0), reverse=True)
        total = sum(gains)
        if total <= 0:
            return 0.0
        return sum(gains[:3]) / total

    def summary(self) -> str:
        if not self.trades:
            return f"[{self.label}] 取引が 1 件も発生しませんでした（条件が厳しすぎる可能性）"
        forced = self.forced_exits
        lines = [
            f"[{self.label}] {self.start} 〜 {self.end}",
            f"  取引数        : {len(self.trades)}"
            + (f"（うち期末打ち切り {forced} 件）" if forced else ""),
            f"  勝率          : {self.win_rate:.1%}",
            f"  総リターン    : {self.total_return:+.1%}",
            f"  最大DD        : {self.max_drawdown:.1%}",
            f"  プロフィットF : {self.profit_factor:.2f}",
            f"  期待値        : {self.expectancy_r:+.2f} R（リスク額で加重）",
            f"  期間の損益    : {self.period_pnl_jpy:+,.0f} 円 ← この期間に実際に増えた額",
            f"  1取引の平均リスク: {self.avg_risk_jpy:,.0f} 円",
            f"  上位3取引の利益寄与: {self.top3_profit_share:.0%}",
            f"  最終資産      : {self.final_equity:,.0f} 円",
        ]
        carried = self.carried_in_trades
        if carried:
            lines.insert(
                -1,
                f"  決済した取引の損益合計: {self.net_pnl_jpy:+,.0f} 円\n"
                f"    ※ うち {carried} 件はこの期間より前に建てた建玉です。"
                " 前の期間で積み上がった\n"
                "       含み益もここに全額計上されるので、"
                "「この期間に稼いだ額」ではありません。",
            )
        if len(self.benchmark_curve) >= 2:
            lines.append(
                f"  同期間の買い持ち: {self.benchmark_return:+.1%}"
                f"（最大DD {self.benchmark_max_drawdown:.1%}）"
            )
        return "\n".join(lines)


def _max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    peak = curve.cummax()
    return float(((peak - curve) / peak).max())


def _equal_weight_curve(
    prices: dict[str, pd.DataFrame], all_dates: list[date]
) -> pd.Series:
    """対象銘柄を等ウェイトで買い持ちした場合の資産推移。

    銘柄ごとに「この期間で最初に値が付いた日」を 1.0 として正規化し、平均を取る。
    ⚠️ この比較は買い持ち側に有利であることを承知して読むこと:
      * 上場廃止銘柄が DB に無いので、生存者バイアスを丸ごと拾っている
      * 売買コストもスリッページもかからない
      * 常時 100% 投資で、損切りが無いぶんドローダウンは戦略より深くなる
    そのため最大ドローダウンも併記して、リターンだけを並べないようにしている。
    """
    if not prices or not all_dates:
        return pd.Series(dtype=float)
    frame = pd.DataFrame({code: df["close"] for code, df in prices.items()})
    frame = frame.reindex(all_dates).ffill()
    base = frame.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan)
    normed = frame.div(base).replace([np.inf, -np.inf], np.nan)
    curve = normed.mean(axis=1, skipna=True)
    return curve.dropna()


def _market_regime(
    bars_by_code: dict[str, pd.DataFrame], codes: list[str], window: int = 200
) -> pd.DataFrame:
    """検証対象の銘柄を等ウェイトした市場指数と、その移動平均より上かどうか。

    個別銘柄しか見ない戦略は「相場全体が崩れているのに買い続ける」を避けられない。
    地合いを判定するには市場全体の系列が要るが、``Strategy.evaluate()`` は
    1 銘柄ぶんの日足しか受け取らないので、ここで作って各銘柄の表に配っておく。

    ⚠️ 先読みにならないこと:
      * 対象銘柄は「期間先頭 60 本の売買代金」で選んでいる（後知恵ではない）
      * t 日の指数は t 日までの終値だけで作る。移動平均も同様
      * 判定した翌営業日の寄りで売買するので、t 日の終値を見て t 日に
        エントリーすることはない
    """
    series = {
        code: bars_by_code[code].set_index("date")["close"]
        for code in codes
        if code in bars_by_code
    }
    if not series:
        return pd.DataFrame(columns=["date", "market_index", "market_above_ma"])
    frame = pd.DataFrame(series).sort_index()
    base = frame.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else np.nan)
    index = frame.div(base).replace([np.inf, -np.inf], np.nan).mean(axis=1, skipna=True)
    ma = index.rolling(window, min_periods=window).mean()
    return pd.DataFrame(
        {
            "date": index.index,
            "market_index": index.to_numpy(),
            # 移動平均が未確定の期間は False。ウォームアップ中は買わせない。
            "market_above_ma": (index > ma).fillna(False).to_numpy(),
        }
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
    regime = _market_regime(bars_by_code, codes)

    for code in codes:
        bars = bars_by_code.get(code)
        if bars is None or len(bars) < sc.min_history_days:
            continue
        enriched = enrich(bars)
        if not regime.empty:
            enriched = enriched.merge(regime, on="date", how="left")
            enriched["market_above_ma"] = enriched["market_above_ma"].fillna(False)

        liquid = enriched["turnover_ma20"] >= sc.min_turnover_jpy
        in_range = enriched["close"].between(sc.min_price, sc.max_price)
        tradable = liquid & in_range

        prices[code] = enriched.set_index("date")[
            ["open", "high", "low", "close", "atr14"]
        ]

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
                        "trailing_atr_mult": strategy.trailing_atr_mult,
                        "use_stop_exit": strategy.use_stop_exit,
                    }
                )
            )

    if not rows:
        return pd.DataFrame(), prices, names
    table = pd.concat(rows, ignore_index=True).sort_values(["date", "score"], ascending=[True, False])
    return table, prices, names


def _bar_price(df: pd.DataFrame, day: date, column: str) -> float | None:
    """その日のバーから価格を取り出す。無い・欠損・非正なら None。

    J-Quants は終値だけを必須にして取り込んでいるため、始値が欠損した日が
    ありうる。そのまま float() すると nan が約定価格になり、損益・期待値・
    プロフィットファクターが例外も出さずに nan で汚染される。
    """
    if day not in df.index:
        return None
    value = float(df.loc[day, column])
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _close_position(
    pos: dict,
    *,
    exit_price: float,
    exit_date: date,
    exit_reason: str,
    slip: float,
    fee: float,
    names: dict[str, str],
) -> Trade:
    """建玉を 1 件決済して Trade を作る。

    手仕舞いの計算（スリッページ・手数料・R 倍率）を 1 か所に集約しておく。
    通常の決済と期末の強制決済で計算式がずれると、そこが成績の嘘になる。
    """
    fill = exit_price * (1 - slip)
    gross = (fill - pos["entry_price"]) * pos["quantity"]
    commission = (fill + pos["entry_price"]) * pos["quantity"] * fee
    pnl = gross - commission
    risk = (pos["entry_price"] - pos["initial_stop"]) * pos["quantity"]
    if risk <= 0:
        # 損切り価格を割って寄り付いた建玉。実際の損切り幅は負になるので、
        # 建てた時点で失う想定だった金額を分母にする。
        risk = pos["intended_risk_jpy"]
    return Trade(
        code=pos["code"],
        name=str(names.get(pos["code"], pos["code"])),
        strategy=pos["strategy"],
        entry_date=pos["entry_date"],
        entry_price=pos["entry_price"],
        exit_date=exit_date,
        exit_price=fill,
        quantity=pos["quantity"],
        pnl_jpy=pnl,
        risk_jpy=risk,
        r_multiple=pnl / risk if risk > 0 else 0.0,
        exit_reason=exit_reason,
    )


def run_backtest(
    cfg: AppConfig,
    db: Database,
    *,
    strategy_names: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
    label: str = "backtest",
    max_symbols: int = 600,
    prebuilt: tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, str]] | None = None,
) -> BacktestResult:
    strategy_names = strategy_names or cfg.screener.strategies
    table, prices, names = prebuilt or _build_signal_table(
        cfg, db, strategy_names, max_symbols
    )
    table = table.copy()
    if prebuilt is not None and not table.empty:
        # 使い回しの表には他の戦略のシグナルも入っている。
        # 表の作成（指標計算 × 全銘柄）が処理時間のほとんどを占めるので、
        # 複数の戦略を比べるときは 1 回作って戦略ごとに絞るほうが速い。
        table = table[table["strategy"].isin(strategy_names)]

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
    # 候補を見送った理由の内訳。集計しないと「選別が効いている」のか
    # 「そもそも選ぶ余地が無い」のか区別がつかない。
    skips: dict[str, int] = {
        "枠が埋まっていた": 0,
        "1日の上限に達した": 0,
        "すでに保有中": 0,
        "その日の株価が無い": 0,
        "損切り幅が取れない": 0,
        "単元株に届かない": 0,
        "資金が足りない": 0,
        "採用": 0,
    }

    lot = cfg.risk.lot_size
    slip = cfg.execution.slippage_pct
    fee = cfg.execution.commission_pct

    for i, today in enumerate(all_dates):
        # ---- 0. 前営業日に決めた決済を、当日の始値で執行 ------------------
        #
        # ⚠️ 決済価格は「損切り価格ちょうど」ではなく **翌営業日の寄り値**。
        # 本番の `hinotane mark` は引け後 16:10 に走るので、判定した時点で
        # 場は終わっている。逆指値注文を出す仕組みは今のところ無い
        # （OrderRequest に逆指値の欄が無く、立花証券の実装も未完）。
        # したがって実際に出せる最速の注文は「翌朝の寄り成行」であり、
        # 損切り価格ちょうどで約定すると仮定するのは、出せない注文を
        # 前提に成績を計算することになる。ここは必ず本番に合わせる。
        still_open: list[dict] = []
        for pos in open_positions:
            reason = pos.get("pending_exit")
            if not reason:
                still_open.append(pos)
                continue
            fill_price = _bar_price(prices[pos["code"]], today, "open")
            if fill_price is None:
                # 値が付かない日は執行できない。翌営業日に持ち越す。
                still_open.append(pos)
                continue
            trade = _close_position(
                pos,
                exit_price=fill_price,
                exit_date=today,
                exit_reason=reason,
                slip=slip,
                fee=fee,
                names=names,
            )
            equity += trade.pnl_jpy
            trades.append(trade)
        open_positions = still_open

        # ---- 1. 前営業日のシグナルを、当日の始値でエントリー ---------------
        #
        # 決済判定より先に行う。寄り付きの時点では、その日のうちに
        # どの建玉が決済されるかを知りようがないため、決済で空いた枠を
        # 同じ日のエントリーに使えてしまうのは未来を覗いていることになる。
        # （0. の決済は前日に決まっていた注文なので、寄りの時点で
        #   枠が空くことは事前に分かっている。こちらは先読みではない。）
        if i > 0:
            prev = all_dates[i - 1]
            candidates = signals_by_date.get(prev)
            if candidates is not None:
                held_codes = {p["code"] for p in open_positions}
                taken = 0
                for _, sig in candidates.iterrows():
                    if len(open_positions) >= cfg.risk.max_open_positions:
                        skips["枠が埋まっていた"] += len(candidates) - taken
                        break
                    if taken >= cfg.risk.max_signals_per_day:
                        skips["1日の上限に達した"] += 1
                        break
                    code = sig["code"]
                    if code in held_codes:
                        skips["すでに保有中"] += 1
                        continue
                    if today not in prices[code].index:
                        skips["その日の株価が無い"] += 1
                        continue

                    raw_open = _bar_price(prices[code], today, "open")
                    if raw_open is None:
                        skips["その日の株価が無い"] += 1
                        continue
                    entry_price = raw_open * (1 + slip)
                    stop = float(sig["stop_price"])

                    # ⚠️ 数量は **シグナル日の終値** を基準に決める。
                    # 本番（risk.py）は前日終値でシグナルを受け取ってから数量を
                    # 計算し、翌朝の寄りで成行発注する。バックテストだけ寄り値で
                    # 数量を決めると、寄り付きのギャップを見てから枚数を調整して
                    # いることになり、本番では作れない建玉ができる。
                    intended_risk_per_share = float(sig["ref_price"]) - stop
                    if intended_risk_per_share <= 0:
                        skips["損切り幅が取れない"] += 1
                        continue

                    # 資金は固定。本番の RiskManager は cfg.risk.equity_jpy を
                    # 見ており、増えた利益を再投資しない。バックテストだけ複利で
                    # 回すとリターンが実態より大きく、ドローダウンが小さく出る。
                    base = cfg.risk.equity_jpy
                    qty = int(
                        math.floor(base * cfg.risk.risk_per_trade / intended_risk_per_share / lot)
                        * lot
                    )
                    max_cost = base * cfg.risk.max_position_pct
                    if qty * entry_price > max_cost:
                        qty = int(math.floor(max_cost / entry_price / lot) * lot)
                    if qty < lot:
                        # ⚠️ ここが効きすぎると、銘柄を「選んで」いない。
                        # 日本株は 100 株単位なので、株価×100 が 1 銘柄あたりの
                        # 投資上限を超えると、どんなに評価の高い候補でも買えない。
                        # 結果として「買える銘柄」だけが残り、順位づけは素通りする。
                        skips["単元株に届かない"] += 1
                        continue

                    # 建玉の合計が運用資金を超えないこと（本番 risk.py と同じ制約）。
                    # これが無いと max_position_pct × max_open_positions > 1 の設定で
                    # 信用取引相当の建て方になり、リターンが水増しされる。
                    committed = sum(p["entry_price"] * p["quantity"] for p in open_positions)
                    if committed + qty * entry_price > base:
                        skips["資金が足りない"] += 1
                        continue

                    trail = sig.get("trailing_atr_mult")
                    open_positions.append(
                        {
                            "code": code,
                            "strategy": sig["strategy"],
                            "entry_date": today,
                            "entry_price": entry_price,
                            "quantity": qty,
                            "stop": stop,
                            # R 倍率の分母は「最初に決めた損切り幅」で固定する。
                            # トレーリングで stop が動くと、あとから R の意味が
                            # 変わってしまい成績の比較ができなくなる。
                            "initial_stop": stop,
                            # 建てた時点で「失う」と決めていた金額。ギャップダウンで
                            # 損切り価格より下に入った場合は実際の損切り幅が負になり
                            # R 倍率が計算できないので、そのときの分母に使う。
                            "intended_risk_jpy": intended_risk_per_share * qty,
                            "target": float(sig["target_price"]),
                            "max_holding_days": int(sig["max_holding_days"]),
                            "trailing_atr_mult": None if pd.isna(trail) else trail,
                            "use_stop_exit": bool(sig.get("use_stop_exit", True)),
                            "highest": entry_price,
                            # 「翌営業日の寄りで売る」と決まった決済理由。
                            # 本番は引け後に判定して翌朝に成行を出すので、
                            # 判定した瞬間には決済できない。
                            "pending_exit": None,
                        }
                    )
                    held_codes.add(code)
                    taken += 1
                    skips["採用"] += 1

        # ---- 2. 当日の値動きで決済を判定する（執行は翌営業日の寄り） --------
        #
        # **当日エントリーした建玉も対象に含める。** 買った初日に損切り価格を
        # 割ることは普通に起きる。ここを翌日以降からにすると、本来なら
        # その日に損切りされた取引が生き延びて、成績が実態より良く出る。
        for pos in open_positions:
            if pos.get("pending_exit"):
                # すでに翌営業日の寄りで売ると決まっている。
                # 決まったあとに損切りを切り上げても意味がない。
                continue
            if today not in prices[pos["code"]].index:
                continue
            bar = prices[pos["code"]].loc[today]

            held = i - date_index[pos["entry_date"]]
            if pos["use_stop_exit"] and bar["low"] <= pos["stop"]:
                pos["pending_exit"] = "stop"
            elif bar["high"] >= pos["target"]:
                pos["pending_exit"] = "target"
            elif held >= pos["max_holding_days"]:
                pos["pending_exit"] = "timeout"
            else:
                # 決済しない場合だけ、損切りラインを切り上げる。
                # 判定より先に更新すると、その日の高値を使って
                # その日の安値を判定することになり未来を覗いてしまう。
                mult = pos.get("trailing_atr_mult")
                if mult:
                    pos["highest"] = max(pos["highest"], float(bar["high"]))
                    atr_now = float(bar["atr14"]) if pd.notna(bar["atr14"]) else 0.0
                    if atr_now > 0:
                        # 損切りは切り上げるだけ。下げると損失が青天井になる。
                        pos["stop"] = max(pos["stop"], pos["highest"] - mult * atr_now)

        # ---- 3. 時価評価 ------------------------------------------------
        unrealized = 0.0
        for pos in open_positions:
            df = prices[pos["code"]]
            if today in df.index:
                unrealized += (float(df.loc[today, "close"]) - pos["entry_price"]) * pos["quantity"]
        curve.append((today, equity + unrealized))

    # ---- 4. 期末に残っている建玉を、最終日の終値で決済する ----------------
    #
    # ⚠️ これをやらないと成績が体系的に歪む。
    # 集計（確定損益・プロフィットファクター・期待値）は決済済みの取引しか
    # 数えないため、期末に持ち越した建玉は丸ごと集計から消える。
    # ところが「持ち越しているもの」は伸びている勝ち馬に偏り、
    # 「決済済みのもの」は損切りされた負けに偏る。
    # つまり勝ちだけが集計から抜け落ちる。
    # 保有期間の長い順張り戦略ほどこの歪みは大きく、
    # 本当は機能している戦略を「負け」と誤判定しかねない。
    if open_positions and all_dates:
        last_day = all_dates[-1]
        for pos in open_positions:
            df = prices[pos["code"]]
            available = df.loc[df.index <= last_day]
            if available.empty:
                continue
            trade = _close_position(
                pos,
                exit_price=float(available["close"].iloc[-1]),
                exit_date=available.index[-1],
                # 決済理由は必ず「期末」にする。ここは戦略が決めた決済ではなく
                # 検証を打ち切るための便宜的な手仕舞いなので、損切りや利確と
                # 同じ扱いで集計に混ぜると成績の読み方を誤らせる。
                exit_reason="期末",
                slip=slip,
                fee=fee,
                names=names,
            )
            equity += trade.pnl_jpy
            trades.append(trade)
        open_positions = []
        # 建玉を落としたので、最終日の資産は評価損益込みではなく確定額になる。
        if curve:
            curve[-1] = (curve[-1][0], equity)

    trades.sort(key=lambda t: (t.exit_date, t.entry_date))
    equity_curve = pd.Series(dict(curve)).sort_index()
    return BacktestResult(
        label=label,
        start=all_dates[0],
        end=all_dates[-1],
        initial_equity=initial,
        final_equity=float(equity_curve.iloc[-1]) if not equity_curve.empty else initial,
        trades=trades,
        equity_curve=equity_curve,
        benchmark_curve=_equal_weight_curve(prices, all_dates),
        skips=skips,
    )




@dataclass
class WalkForwardReport:
    """イン／アウトオブサンプル検証の結果と、判定に必要な構造情報。

    判定（judge）が「標本が足りているか」を出力ではなく **設計** から
    決められるように、ウォームアップ本数・最大保有日数・売買可能だった
    営業日数を一緒に持ち回る。取引数だけを条件にすると、
    「たまたま取引が少なかった戦略」が過剰最適化の検査を素通りできてしまう。
    """

    full: BacktestResult
    in_sample: BacktestResult
    out_sample: BacktestResult
    boundary: date
    in_window_days: int      # 前半のうち、シグナルを出せた営業日数
    out_window_days: int
    warmup_bars: int
    max_holding_days: int


def _slice_result(full: BacktestResult, *, start: date, end: date, label: str) -> BacktestResult:
    """連続運用の結果を、日付で切り出す。

    ⚠️ 期間を分けて 2 回バックテストを回すのではなく、**1 回だけ通しで回して
    から切る**。分けて回すと、境界で建玉が強制決済されて「まだ伸びている
    勝ち馬」がその時点の値で確定損益に化け、前半の成績が水増しされる。
    さらに後半が「建玉ゼロ・枠が全部空いている」状態から始まるため、
    実運用では取れなかった建玉まで取れてしまう。
    """
    trades = [t for t in full.trades if start <= t.exit_date <= end]
    curve = full.equity_curve[
        (full.equity_curve.index >= start) & (full.equity_curve.index <= end)
    ]
    bench = full.benchmark_curve[
        (full.benchmark_curve.index >= start) & (full.benchmark_curve.index <= end)
    ]
    initial = float(curve.iloc[0]) if not curve.empty else full.initial_equity
    final = float(curve.iloc[-1]) if not curve.empty else initial
    return BacktestResult(
        label=label,
        start=start,
        end=end,
        initial_equity=initial,
        final_equity=final,
        trades=trades,
        equity_curve=curve,
        benchmark_curve=bench,
    )


def walk_forward(
    cfg: AppConfig,
    db: Database,
    *,
    strategy_names: list[str] | None = None,
    split: float = 0.6,
    max_symbols: int = 600,
) -> WalkForwardReport:
    """イン／アウトオブサンプル分割で検証する。

    過剰最適化を見抜くための最低限の作法。前半（イン）でだけ成績が良く、
    後半（アウト）で崩れる戦略は、過去に当てはめただけで先には効かない。

    分割は **ウォームアップを除いた売買可能期間** で行う。日足の全期間を
    単純に 6:4 で割ると、200 日線を使う戦略では前半のほとんどが指標の
    計算待ちで消え、前半の取引が数件しか出ない。それは戦略の性質ではなく
    分割器の欠陥で、その数件を基準に後半を測っても何も分からない。
    """
    strategy_names = strategy_names or cfg.screener.strategies
    prebuilt = _build_signal_table(cfg, db, strategy_names, max_symbols)
    table, prices, _names = prebuilt
    if table.empty or not prices:
        raise ValueError(
            "シグナルが 1 件も生成されませんでした。"
            " 先に `hinotane backfill` でデータを取り込んでください。"
        )

    all_dates = sorted({d for df in prices.values() for d in df.index})
    if len(all_dates) < 200:
        raise ValueError("検証に十分な日足がありません。先に `hinotane backfill` を実行してください。")

    strategies = [get_strategy(n) for n in strategy_names]
    warmup = max(s.warmup_bars for s in strategies)
    max_hold = max(s.max_holding_days for s in strategies)

    tradable = all_dates[warmup:]
    if len(tradable) < 40:
        raise ValueError(
            f"指標の計算に {warmup} 本必要ですが、日足が {len(all_dates)} 本しかありません。"
            " 売買できる期間がほとんど残らないため検証できません。"
        )

    cut = min(max(int(len(tradable) * split), 1), len(tradable) - 1)
    boundary = tradable[cut]

    full = run_backtest(
        cfg,
        db,
        strategy_names=strategy_names,
        label="全期間（通しで連続運用）",
        max_symbols=max_symbols,
        prebuilt=prebuilt,
    )

    in_sample = _slice_result(
        full, start=all_dates[0], end=boundary, label="イン・サンプル（前半）"
    )
    after = tradable[cut + 1]
    out_sample = _slice_result(
        full, start=after, end=all_dates[-1], label="アウト・オブ・サンプル（後半）"
    )

    return WalkForwardReport(
        full=full,
        in_sample=in_sample,
        out_sample=out_sample,
        boundary=boundary,
        in_window_days=cut + 1,
        out_window_days=len(tradable) - cut - 1,
        warmup_bars=warmup,
        max_holding_days=max_hold,
    )


PASS = "PASS"
FAIL = "FAIL"
UNDETERMINED = "UNDETERMINED"




def judge(report: WalkForwardReport) -> tuple[str, list[str]]:
    """検証結果から合否を出す。返り値は PASS / FAIL / UNDETERMINED。

    設計の要は 3 つ。

    **1. 「判定できなかった」を合格側に倒さない。**
    bool を返していた頃は、標本不足で検査できなかった項目があっても ✅ が出た。
    ✅ は「実運用に載せてよい」という合図として読まれるので、
    未検査を ✅ に混ぜるのは注意書きを添えたところで免罪符にしかならない。

    **2. お金が増えていないものは、何があっても合格にしない。**
    期待値（R）はリスク額で割った比率なので、取引ごとにリスク額がばらつくと
    円の損益と符号が食い違いうる。最終的な判断はお金で行う。

    **3. 買い持ちに勝てないものは合格にしない。**
    この判定は「実弾を入れるか」を決めるためのもの。同じ銘柄を買って放って
    おくほうが成績が良いなら、わざわざ毎日売買する理由がない。
    以前は警告に留めていたため、「買い持ちに負けています」と表示した 2 行あとに
    ✅ を出すという自己矛盾が起きた（実測で発生）。

    Returns:
        (PASS | FAIL | UNDETERMINED, 表示する行のリスト)
    """
    in_s, out_s = report.in_sample, report.out_sample
    lines: list[str] = []

    if not out_s.trades:
        return UNDETERMINED, ["⚠️  アウトオブサンプルで取引が発生せず、判断できません。"]

    # 前半の標本が判定に足りているか。**取引数ではなく期間の構造** で決める。
    # 取引数は回してみないと分からない出力なので、それを条件にすると
    # 「たまたま取引が少なかった」あらゆる戦略が検査を回避できてしまう。
    needed = report.max_holding_days * 2
    blockers: list[str] = []
    if report.in_window_days < needed:
        blockers.append(
            f"前半の売買可能期間が {report.in_window_days} 営業日しかなく、"
            f" 最大保有 {report.max_holding_days} 日の売買が 1 巡もしません"
            f"（{needed} 営業日以上必要）"
        )
    if len(in_s.trades) < 30:
        blockers.append(f"前半の取引が {len(in_s.trades)} 件しかなく、期待値を推定できません")

    failures: list[str] = []

    # ------------------------------------------------------------ お金の検査
    # 判定に使うのは period_pnl_jpy（口座が実際に増減した額）。
    # net_pnl_jpy（決済した取引の合計）を使うと、前半で積み上がった含み益を
    # 後半に決済しただけで「後半も稼いだ」ことになってしまう。
    if out_s.period_pnl_jpy <= 0:
        failures.append(
            f"アウトオブサンプルで資産が減っています"
            f"（{out_s.period_pnl_jpy:+,.0f} 円 / {out_s.total_return:+.1%}）"
        )
    elif out_s.carried_in_trades and out_s.net_pnl_jpy > out_s.period_pnl_jpy * 2:
        lines.append(
            f"⚠️  後半の決済損益 {out_s.net_pnl_jpy:+,.0f} 円のうち、口座が実際に増えたのは"
            f" {out_s.period_pnl_jpy:+,.0f} 円だけです。"
            f"\n    差は前半のうちに積み上がっていた含み益を、後半に決済しただけのぶんです。"
            f"\n    後半に建てた取引がどれだけ稼いだかは、この数字からは分かりません"
            f"（{out_s.carried_in_trades} 件が前半からの持ち越し）。"
        )

    if out_s.profit_factor <= 1.0:
        failures.append(
            f"アウトオブサンプルのプロフィットファクターが 1 以下です"
            f"（{out_s.profit_factor:.2f}）＝ 損失が利益を上回っています"
        )

    # -------------------------------------------- 優位性が偶然と区別できるか
    out_lo, out_hi = out_s.expectancy_ci()
    if out_lo <= 0:
        failures.append(
            f"アウトオブサンプルの期待値 {out_s.expectancy_r:+.2f} R は、"
            f"95% 信頼区間が [{out_lo:+.2f}, {out_hi:+.2f}] R でゼロをまたぎます"
            "（優位性が偶然と区別できません）"
        )

    # ------------------------------------------------------ 買い持ちとの比較
    bench_r, bench_dd = out_s.benchmark_return, out_s.benchmark_max_drawdown
    if len(out_s.benchmark_curve) >= 2 and out_s.total_return < bench_r:
        # ドローダウン 1% あたり何 % 取れたか。投資額も損切りの有無も違う
        # 両者を、同じ土俵で並べるためのいちばん素朴な物差し。
        ratio = out_s.total_return / out_s.max_drawdown if out_s.max_drawdown > 0 else 0.0
        bench_ratio = bench_r / bench_dd if bench_dd > 0 else 0.0
        detail = (
            f"同じ期間の買い持ち {bench_r:+.1%}（最大DD {bench_dd:.1%}）に対し、"
            f"戦略は {out_s.total_return:+.1%}（最大DD {out_s.max_drawdown:.1%}）。"
            f" リスク調整後（リターン÷最大DD）は 戦略 {ratio:.2f} / 買い持ち {bench_ratio:.2f}"
        )
        if ratio < bench_ratio:
            failures.append(
                detail + " ＝ リスクを抑えたぶんを差し引いても買い持ちに負けています。"
                " 毎日売買する意味がありません"
            )
        else:
            lines.append(
                f"⚠️  リターンでは買い持ちに負けています。{detail}"
                "\n    → リスクあたりでは上回っています。取れる金額は小さいが、"
                "揺れも小さいという性質です。"
            )

    # ------------------------------------------ 数銘柄の当たりで出来ていないか
    top3 = out_s.top3_profit_share
    if top3 > 0.7:
        failures.append(
            f"アウトオブサンプルの総利益の {top3:.0%} が上位 3 取引に集中しています。"
            " 仕組みではなく数銘柄がたまたま当たった記録です"
        )
    elif top3 > 0.5:
        lines.append(
            f"⚠️  総利益の {top3:.0%} が上位 3 取引に集中しています。"
            " その数銘柄を抜くと成績はほぼ消えます。"
        )

    if failures:
        lines.append("❌ この戦略は実運用に載せないでください。")
        lines.extend(f"   ・{reason}" for reason in failures)
        if in_s.period_pnl_jpy > 0:
            lines.append("   ・前半では増えていたので、過剰最適化の可能性があります。")
        return FAIL, lines

    if blockers:
        lines.append("⚠️  判定保留。この戦略は「合格した」のではなく「検証できなかった」状態です。")
        lines.extend(f"   ・{b}" for b in blockers)
        lines.append("   ・後半の絶対成績は基準を満たしていますが、")
        lines.append("     過剰最適化かどうかは **未検査** です。実運用に載せる根拠にはなりません。")
        lines.append(f"   ・原因はデータの短さです（日足 {report.warmup_bars} 本が指標の計算で消えます）。")
        lines.append("     履歴を伸ばすか、少額のフォワードテストで取引数を稼いでください。")
        return UNDETERMINED, lines

    # ------------------------------------------------ 前半に優位性があったか
    #
    # ⚠️ ここが無いと過剰最適化の検査が空回りする。
    # 「後半が前半の半分未満か」は、前半に優位性があって初めて意味を持つ。
    # 前半の期待値が実質ゼロだと、後半がどんな値でも「劣化していない」と
    # 判定されて素通りする（実測で発生: 前半 +0.00R・PF 1.01 なのに合格）。
    # 前半にも後半にも優位性が要る。片方だけなら、それは相場か偶然の産物。
    in_lo, in_hi = in_s.expectancy_ci()
    if in_lo <= 0:
        lines.append("❌ この戦略は実運用に載せないでください。")
        lines.append(
            f"   ・前半に優位性がありません"
            f"（期待値 {in_s.expectancy_r:+.2f} R / 95%信頼区間 [{in_lo:+.2f}, {in_hi:+.2f}] R）"
        )
        lines.append(
            f"   ・前半 {len(in_s.trades)} 件・{report.in_window_days} 営業日を使って"
            " ゼロと区別できないということは、"
        )
        lines.append("     後半だけ良かったのは相場のおかげか偶然と考えるのが自然です。")
        return FAIL, lines

    if out_s.expectancy_r < in_s.expectancy_r * 0.5:
        lines.append("❌ 後半で期待値が半分以下に落ちています。過剰最適化の疑いが濃厚です。")
        lines.append(f"   ・前半 {in_s.expectancy_r:+.2f} R → 後半 {out_s.expectancy_r:+.2f} R")
        return FAIL, lines

    if len(out_s.trades) < 30:
        lines.append(
            f"⚠️  アウトオブサンプルの取引が {len(out_s.trades)} 件しかなく、"
            " 偶然の影響が大きい水準です。数字を強く信じないでください。"
        )
    if out_s.forced_exits / len(out_s.trades) > 0.25:
        lines.append(
            f"⚠️  後半の {out_s.forced_exits} 件は期間の打ち切りによる決済で、"
            " 戦略が決めた決済ではありません。成績は途中経過に近いものです。"
        )

    lines.append("✅ 前半・後半とも優位性が確認でき、買い持ちにも負けていません。")
    lines.append(f"   前半の期待値 95%区間 [{in_lo:+.2f}, {in_hi:+.2f}] R /"
                 f" 後半 [{out_lo:+.2f}, {out_hi:+.2f}] R")
    lines.append("   ただしこれは必要条件であって十分条件ではありません。")
    lines.append("   生存者バイアス（上場廃止銘柄を含まない）のぶん、実際はこれより悪くなります。")
    return PASS, lines
