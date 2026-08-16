"""ターミナル出力。LINE を設定する前の動作確認用。"""

from __future__ import annotations

from ..datasource.jquants import display_code
from ..risk import SizedSignal
from ..strategies.base import get_strategy


def render(items: list[SizedSignal], *, header_text: str, warnings: list[str] | None = None) -> str:
    lines = [header_text, ""]
    for w in warnings or []:
        lines.append(f"⚠️  {w}")
    if warnings:
        lines.append("")

    if not items:
        lines.append("本日の候補はありません。")
        return "\n".join(lines)

    for i, item in enumerate(items, 1):
        s = item.signal
        try:
            strategy = get_strategy(s.strategy)
            label, has_target = strategy.label, strategy.has_profit_target
        except KeyError:
            label, has_target = s.strategy, True
        loss_pct = (s.stop_price / s.entry_price - 1) * 100
        gain_pct = (s.target_price / s.entry_price - 1) * 100
        target_line = (
            f"     利確 : {s.target_price:>9,.0f} 円 ({gain_pct:+.1f}%)  R/R {s.reward_risk:.1f}倍"
            if has_target
            else "     利確 : 目標なし。トレーリングストップで撤退"
        )

        lines += [
            f"[{i}] {display_code(s.code)} {s.name}  —  {label}",
            "     根拠 : " + " / ".join(s.reasons),
            f"     入り : {s.entry_price:>9,.0f} 円",
            f"     損切 : {s.stop_price:>9,.0f} 円 ({loss_pct:+.1f}%)",
            target_line,
            f"     数量 : {item.quantity:>9,} 株   必要資金 {item.cost_jpy:,.0f} 円",
            f"     最大損失 : {item.risk_jpy:,.0f} 円",
            f"     承認ID : {item.signal_key}",
            "",
        ]
    return "\n".join(lines)
