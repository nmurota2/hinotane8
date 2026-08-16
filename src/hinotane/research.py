"""戦略の敗因を実験で特定する。

戦略を作り直す前にここを通す。思いつきを次々試すのが最悪の進め方で、
同じデータで何十回も検定すれば **必ず偶然当たるものが出る**。
そしてそれを「見つけた」と report してしまう。

このモジュールは、その罠を 3 つの仕掛けで避ける。

**1. 後半のデータに触れない。**
実験はすべて前半（イン・サンプル）だけで走る。後半は最後に 1 回だけ、
勝ち残ったひとつを検証するために取っておく。何度も覗いたデータは
もうアウト・オブ・サンプルではない。

**2. 対照群と比べる。**
「この設定で +0.2R 出た」には意味がない。**1 か所だけ変えた対照群との差**
だけが、その 1 か所の寄与を示す。特に「順位づけを乱数にした対照群」は、
銘柄の選び方が機能しているかを直接測る唯一の方法。

**3. 検定回数を数えて、そのぶん基準を上げる。**
K 個の実験を 95% 信頼区間で見れば、優位性が無くても K×5% は当たって見える。
ボンフェローニ補正で区間を広げ、何回検定したかを必ず表示する。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pandas as pd

from .backtest import BacktestResult, _build_signal_table, run_backtest
from .config import AppConfig
from .db import Database
from .strategies.base import get_strategy


@dataclass
class Experiment:
    """1 か所だけ変えた設定。本家との差が、その 1 か所の寄与になる。"""

    name: str
    hypothesis: str          # 何を確かめる実験か
    strategy: str
    risk: dict = field(default_factory=dict)   # RiskConfig の上書き


#: 実験の一覧。**思いついた順に足さないこと。**
#: ここに 1 行足すたびに検定回数が増え、全体の基準が上がる。
#: 足すなら「この結果が出たら何が結論できるか」を hypothesis に書けるものだけ。
BATTERY: list[Experiment] = [
    Experiment(
        name="① 本家 trend",
        hypothesis="比較の基準。ここからの差で各要素の寄与を測る",
        strategy="trend",
    ),
    Experiment(
        name="② 選ぶ順を乱数に",
        hypothesis="本家と差が無ければ、12-1モメンタムの順位づけは何もしていない",
        strategy="研究:順位乱数",
    ),
    Experiment(
        name="③ 損切りを外す",
        hypothesis="本家より良ければ、上げ相場で損切りが足を引っ張っている",
        strategy="研究:損切りなし",
    ),
    Experiment(
        name="④ トレーリングを外す",
        hypothesis="本家より悪ければ、損切りの切り上げは効いている",
        strategy="研究:トレーリングなし",
    ),
    Experiment(
        name="⑤ 地合いフィルタ",
        hypothesis="本家より良ければ、相場が弱い局面の無駄打ちが損失源",
        strategy="研究:地合いフィルタ",
    ),
    Experiment(
        name="⑥ 高値更新を待たない",
        hypothesis="本家より良ければ、高値更新を待つことが高値掴みになっている",
        strategy="研究:高値更新なし",
    ),
    Experiment(
        name="⑦ 同時保有を5→15銘柄",
        hypothesis="良くなれば、5銘柄への集中が分散不足として効いている",
        strategy="trend",
        # ⚠️ max_position_pct も一緒に下げること。下げないと建玉の合計が
        # 運用資金を超えられず、枠を広げても実際には 5 銘柄しか建たない
        # （最初にそう書いて、⑦⑧ が①と 1 円も違わない結果になった）。
        risk={"max_open_positions": 15, "max_signals_per_day": 8, "max_position_pct": 1 / 15},
    ),
    Experiment(
        name="⑧ 同時保有を5→30銘柄",
        hypothesis="⑦がさらに良くなるなら、分散が多いほど良い＝選別が効いていない",
        strategy="trend",
        risk={"max_open_positions": 30, "max_signals_per_day": 15, "max_position_pct": 1 / 30},
    ),
]


@dataclass
class ExperimentResult:
    experiment: Experiment
    result: BacktestResult
    ci: tuple[float, float]


def _ranking_binds(table: pd.DataFrame, cfg: AppConfig, strategy: str, end) -> tuple[int, int]:
    """順位づけが実際に効いた日数を数える。

    ⚠️ これを確かめずに「順位を乱数にしても成績が変わらない ＝ 順位づけは無意味」
    と結論してはいけない。1 日のシグナルが枠（既定 3 件）以下なら、
    どんな順位でも同じ銘柄を全部取るので、**比較そのものが成立していない**。
    合成データで実際にこの誤りが出た（8 実験中 5 個が完全に同じ数字になった）。

    Returns:
        (順位が効いた日数, シグナルが出た日数)
    """
    if table.empty:
        return (0, 0)
    sub = table[table["strategy"] == strategy]
    if end is not None:
        sub = sub[sub["date"] <= end]
    if sub.empty:
        return (0, 0)
    per_day = sub.groupby("date").size()
    return int((per_day > cfg.risk.max_signals_per_day).sum()), len(per_day)


def _lot_cost_report(
    prices: dict, cfg: AppConfig, end, positions: int
) -> tuple[float, list[str]]:
    """1 単元（100株）いくらかを調べ、何銘柄まで分散できるかを出す。

    日本株は 100 株単位でしか買えない。株価 2,000 円なら 1 単元 20 万円。
    運用資金 100 万円で 1 銘柄あたり 20% までなら、上限ちょうど 20 万円なので
    「株価 2,000 円まで」しか買えない。それ以上の銘柄は、どれほど評価が
    高くても **買えない**。

    この制約は戦略の良し悪しと無関係に効くうえ、効いていることが
    成績の表からは見えない。分散を増やす実験が「0 取引」で返ってきて
    初めて気づくことになるので、先に数字で出しておく。

    Returns:
        (投資上限内に収まる銘柄の割合, 表示する行)
    """
    import numpy as np

    lot = cfg.risk.lot_size
    costs = []
    for df in prices.values():
        closes = df.loc[df.index <= end, "close"].dropna() if end else df["close"].dropna()
        if len(closes):
            costs.append(float(closes.median()) * lot)
    lines: list[str] = []
    if not costs:
        return (0.0, lines)
    arr = np.array(costs)
    cap = cfg.risk.equity_jpy * cfg.risk.max_position_pct
    reachable = float((arr <= cap).mean())
    lines.append(f"  1単元（{lot}株）の金額: 中央値 {np.median(arr):,.0f} 円 /"
                 f" 下位25% {np.percentile(arr, 25):,.0f} 円 /"
                 f" 上位25% {np.percentile(arr, 75):,.0f} 円")
    lines.append(f"  1銘柄あたりの投資上限: {cap:,.0f} 円"
                 f"（運用資金 {cfg.risk.equity_jpy:,.0f} 円 × {cfg.risk.max_position_pct:.0%}）")
    lines.append(f"  → 上限内で買える銘柄は全体の {reachable:.0%}")
    if positions > 1:
        need = float(np.median(arr)) * positions
        lines.append(f"  → {positions} 銘柄を等しく分散して持つには、"
                     f"中央値ベースで約 {need:,.0f} 円の資金が要ります")
    return (reachable, lines)


def _row(label: str, r: BacktestResult, ci: tuple[float, float]) -> str:
    return (
        f"  {label:<22} {len(r.trades):>5} {r.total_return:>+8.1%} {r.max_drawdown:>7.1%}"
        f" {r.profit_factor:>6.2f} {r.expectancy_r:>+7.2f} [{ci[0]:+.2f}, {ci[1]:+.2f}]"
    )


def run_research(
    cfg: AppConfig,
    db: Database,
    *,
    max_symbols: int = 600,
    split: float = 0.6,
) -> str:
    """実験を一括で走らせ、判断できる形の報告を返す。

    シグナル表（全銘柄 × 全戦略の指標計算）が処理時間のほとんどを占めるので、
    **1 回だけ作って全実験で使い回す**。実験を 8 個に増やしても
    かかる時間はほとんど変わらない。
    """
    out: list[str] = []
    strategy_names = sorted({e.strategy for e in BATTERY})

    # --- 実験に使う期間を決める（後半は封印する） ------------------------
    prebuilt = _build_signal_table(cfg, db, strategy_names, max_symbols)
    table, prices, _names = prebuilt
    if table.empty or not prices:
        return "シグナルが 1 件も生成されませんでした。先に `hinotane backfill` を実行してください。"

    all_dates = sorted({d for df in prices.values() for d in df.index})
    warmup = max(get_strategy(n).warmup_bars for n in strategy_names)
    max_hold = max(get_strategy(n).max_holding_days for n in strategy_names)
    tradable = all_dates[warmup:]
    if len(tradable) < max_hold:
        return (
            f"指標の計算に {warmup} 本必要ですが、日足が {len(all_dates)} 本しかありません。"
            " 実験できる期間が残りません。"
        )
    boundary = tradable[min(max(int(len(tradable) * split), 1), len(tradable) - 1)]

    k = len(BATTERY)
    # ボンフェローニ補正。K 回検定するなら、1 回あたりの有意水準を K で割る。
    # そうしないと「優位性が無くても K×5% は当たって見える」を防げない。
    confidence = 1.0 - 0.05 / k

    out.append("=" * 78)
    out.append("実験の設計")
    out.append("=" * 78)
    out.append(f"  対象期間 : {all_dates[0]} 〜 {boundary}（前半のみ）")
    out.append(f"  封印中   : {boundary} より後（{len(tradable) - tradable.index(boundary) - 1} 営業日）")
    out.append("")
    out.append("  ⚠️ 後半のデータには一切触れていません。")
    out.append("     ここで見つけたものを、最後に 1 回だけ後半で検証します。")
    out.append("     何度も覗いたデータは、もうアウト・オブ・サンプルではありません。")
    out.append("")
    out.append(f"  実験数   : {k} 個")
    out.append(f"  信頼区間 : {confidence:.1%}（ボンフェローニ補正済み。通常の95%ではありません）")
    out.append(f"     → {k} 個も試せば、優位性が無くても {k * 0.05:.1f} 個は")
    out.append("       95%区間で「当たり」に見えます。そのぶん基準を上げてあります。")

    # --- 実験を走らせる --------------------------------------------------
    results: list[ExperimentResult] = []
    for exp in BATTERY:
        exp_cfg = cfg
        if exp.risk:
            exp_cfg = replace(cfg, risk=replace(cfg.risk, **exp.risk))
        r = run_backtest(
            exp_cfg,
            db,
            strategy_names=[exp.strategy],
            end=boundary,
            label=exp.name,
            max_symbols=max_symbols,
            prebuilt=prebuilt,
        )
        results.append(ExperimentResult(exp, r, r.expectancy_ci(confidence=confidence)))

    base = results[0]
    bench_r = base.result.benchmark_return
    bench_dd = base.result.benchmark_max_drawdown

    out.append("")
    out.append("=" * 78)
    out.append("結果")
    out.append("=" * 78)
    out.append(
        f"  {'実験':<22} {'取引':>5} {'リターン':>8} {'最大DD':>7}"
        f" {'PF':>6} {'期待値[' + f'{confidence:.0%}' + '区間]':>7}"
    )
    out.append("  " + "-" * 74)
    for er in results:
        out.append(_row(er.experiment.name, er.result, er.ci))
    out.append("  " + "-" * 74)
    out.append(f"  {'（参考）等ウェイト買い持ち':<20} {'':>5} {bench_r:>+8.1%} {bench_dd:>7.1%}")

    # --- 読み方 ----------------------------------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("読み方")
    out.append("=" * 78)

    by_name = {er.experiment.name: er for er in results}

    def gap(name: str) -> float | None:
        er = by_name.get(name)
        if er is None:
            return None
        return er.result.expectancy_r - base.result.expectancy_r

    out.append("")
    out.append("【0】そもそも「選ぶ」余地があったか")
    _reachable, lot_lines = _lot_cost_report(
        prices, cfg, boundary, cfg.risk.max_open_positions
    )
    out.extend(lot_lines)
    out.append("")
    sk = base.result.skips
    total_seen = sum(v for k, v in sk.items() if k != "採用")
    if total_seen:
        out.append(f"  候補を見送った回数の内訳（採用 {sk.get('採用', 0)} 件に対して）:")
        for reason, n in sorted(sk.items(), key=lambda kv: -kv[1]):
            if reason == "採用" or n == 0:
                continue
            out.append(f"    {reason:<16} {n:>7,} 回 ({n / total_seen:>5.1%})")
    lot_share = sk.get("単元株に届かない", 0) / total_seen if total_seen else 0.0
    if total_seen:
        if lot_share > 0.3:
            out.append("")
            out.append(f"  → ⚠️ 見送りの {lot_share:.0%} が「単元株に届かない」です。")
            out.append("     日本株は 100 株単位なので、株価×100 が 1 銘柄あたりの投資上限")
            out.append(f"     （運用資金の {cfg.risk.max_position_pct:.0%} = "
                       f"{cfg.risk.equity_jpy * cfg.risk.max_position_pct:,.0f} 円）を超えると、")
            out.append("     **どんなに評価が高い候補でも買えません。**")
            out.append("     この状態では、銘柄を選んでいるのは戦略ではなく株価と資金量です。")
            out.append("     順位づけの良し悪しを論じる前に、ここを解く必要があります。")

    out.append("")
    out.append("【1】銘柄の選び方は機能しているか（この検証の核心）")
    bound_days, signal_days = _ranking_binds(table, cfg, "trend", boundary)
    out.append(
        f"  シグナルが出た {signal_days} 日のうち、候補が枠"
        f"（1日 {cfg.risk.max_signals_per_day} 件）を超えたのは {bound_days} 日"
    )
    rnd = by_name.get("② 選ぶ順を乱数に")
    if lot_share > 0.3:
        # 【0】で「買える候補が枠より少ない」と分かっている以上、
        # 順位づけの良し悪しはこの比較では測れない。
        # ここを素通りさせると、成立していない比較から
        # 「順位づけは無意味」という強い主張を出してしまう（実際に出た）。
        out.append("  → ⚠️ この比較は成立していません。【0】のとおり、候補の大半が")
        out.append("     単元株の制約で買えないため、順位づけを使う場面がありません。")
        out.append("     まず資金・単元の制約を解いてから、あらためて測ってください。")
    elif signal_days and bound_days / signal_days < 0.2:
        # ここを確かめずに結論すると、成立していない比較から
        # 「順位づけは無意味」という強い主張を出してしまう。
        out.append("  → ⚠️ この比較は成立していません。候補が枠を超える日がほとんど無いため、")
        out.append("     どんな順位づけでも同じ銘柄を取ります。差が無いのは当然です。")
        out.append("     順位づけの効果を測るには、まず候補が枠を超える状況が要ります。")
    elif rnd:
        d = base.result.expectancy_r - rnd.result.expectancy_r
        out.append(f"  本家 {base.result.expectancy_r:+.2f}R / 乱数 {rnd.result.expectancy_r:+.2f}R"
                   f" → 差 {d:+.2f}R")
        if abs(d) < 0.05:
            out.append("  → ❌ 差がほぼありません。**12-1モメンタムの順位づけは何もしていません。**")
            out.append("     エントリー条件を磨いても無駄です。選び方から作り直す必要があります。")
        elif d > 0:
            out.append("  → ✅ 順位づけには効果があります。ここを強化する価値があります。")
        else:
            out.append("  → ⚠️ 乱数のほうが良い。順位づけが **逆に働いています**。")
        out.append("     ※ 乱数の対照群は 1 通りしか試していません。乱数の引き次第で")
        out.append("       この差はぶれます。小さな差を意味づけしないでください。")

    out.append("")
    out.append("【2】損切りは得か損か")
    nostop, fixed = gap("③ 損切りを外す"), gap("④ トレーリングを外す")
    if nostop is not None:
        out.append(f"  損切りを外すと {nostop:+.2f}R の変化")
        out.append(
            "  → 上げ相場で損切りが足を引っ張っています。幅か置き方を変える価値があります。"
            if nostop > 0.05
            else "  → 損切りは損失を抑える方向に働いています。外すべきではありません。"
        )
    if fixed is not None:
        out.append(f"  トレーリングを外すと {fixed:+.2f}R の変化")
        out.append(
            "  → トレーリングは効いています（外すと悪化）。"
            if fixed < -0.02
            else "  → トレーリングは効いていません。固定損切りと変わりません。"
        )

    out.append("")
    out.append("【3】エントリーの場面は適切か")
    for name in ("⑤ 地合いフィルタ", "⑥ 高値更新を待たない"):
        d = gap(name)
        if d is not None:
            out.append(f"  {name}: {d:+.2f}R")

    out.append("")
    out.append("【4】集中しすぎていないか")
    for name in ("⑦ 同時保有を5→15銘柄", "⑧ 同時保有を5→30銘柄"):
        er, d = by_name.get(name), gap(name)
        if er is None or d is None:
            continue
        if not er.result.trades:
            # 0 取引は「成績が悪かった」ではない。「この資金額では組めない」。
            # 表の数字だけ見ると成績ゼロに見えるので、必ず言葉で区別する。
            pct = er.experiment.risk.get("max_position_pct")
            out.append(
                f"  {name}: **この資金額では実行できません**（取引 0 件）。"
                + (f" 1銘柄あたり {cfg.risk.equity_jpy * pct:,.0f} 円までとなり、"
                   " 単元株の金額がそれを超える銘柄しかありません。" if pct else "")
            )
            continue
        pct = er.experiment.risk.get("max_position_pct")
        note = ""
        if pct:
            cap = cfg.risk.equity_jpy * pct
            note = (
                f"\n     ⚠️ ただし1銘柄あたり {cap:,.0f} 円までなので、"
                f"1単元がそれ以下の **安い銘柄だけ** の運用になっています。"
                "\n        成績の差が「分散」によるものか「安い銘柄」によるものか、"
                "この実験では区別できません。"
            )
        degenerate = ""
        if len(er.result.trades) < 30:
            degenerate = (
                f"\n     ⚠️ 取引 {len(er.result.trades)} 件では期待値を推定できません。"
                "この行の数字は読まないでください。"
            )
        out.append(
            f"  {name}: {d:+.2f}R / 取引 {len(er.result.trades)} 件"
            f" / リターン {er.result.total_return:+.1%}{note}{degenerate}"
        )
    spread = [by_name.get(n) for n in ("⑦ 同時保有を5→15銘柄", "⑧ 同時保有を5→30銘柄")]
    if any(er is not None and er.result.trades for er in spread):
        out.append("  → 銘柄数を増やすほど良くなるなら、それは「選別が効いていない」証拠です。")
        out.append("     選ばずに広く持つほうが良い＝順位づけに情報が無い、ということ。")
    else:
        out.append("  → 分散を増やす実験がどちらも成立しませんでした。")
        out.append("     この資金額では、5銘柄前後の集中運用しか物理的に組めません。")
        out.append("     「分散が足りないから負けた」という説明は、少なくともここでは検証できません。")

    # 設定を変えたのに結果が 1 円も動かないなら、その設定は最初から効いていない。
    # 「差が無い＝その要素は無意味」と読み違えないよう、明示的に警告する。
    identical = [
        er.experiment.name
        for er in results[1:]
        if len(er.result.trades) == len(base.result.trades)
        and abs(er.result.period_pnl_jpy - base.result.period_pnl_jpy) < 1.0
    ]
    if identical:
        out.append("")
        out.append("  ⚠️ 次の実験は本家と 1 円も違いませんでした:")
        for name in identical:
            out.append(f"     ・{name}")
        out.append("     設定を変えても結果が動かないのは、その設定が最初から")
        out.append("     効いていないということです。「差が無い＝その要素は無意味」ではありません。")

    # --- 総括 ------------------------------------------------------------
    out.append("")
    out.append("=" * 78)
    out.append("総括")
    out.append("=" * 78)
    positive = [
        er for er in results if er.ci[0] > 0 and er.result.total_return > 0
    ]
    if not positive:
        out.append(f"  {k} 個すべてで、期待値の {confidence:.0%} 信頼区間がゼロをまたぎました。")
        out.append("  補正前の 95% で見ても足りないなら、この方向に優位性はありません。")
        out.append("  対照群との差を見て、**どの要素を作り直すか** を決めてください。")
    else:
        out.append(f"  区間がゼロを上回ったのは {len(positive)} 個:")
        for er in positive:
            out.append(f"    ・{er.experiment.name}（{er.result.expectancy_r:+.2f}R）")
        out.append("")
        out.append("  ⚠️ ここで選んだものを、そのまま実運用に載せないでください。")
        out.append(f"     {k} 個から選んだ時点で「一番良かったものを選ぶ」というバイアスが乗ります。")
        out.append("     封印してある後半で 1 回だけ検証し、そこでも通ったものだけが候補です。")
    out.append("")
    out.append("  買い持ち（同期間 " + f"{bench_r:+.1%}" + "）を上回れないなら、")
    out.append("  どの実験が最良でも、実弾を入れる理由にはなりません。")

    return "\n".join(out)
