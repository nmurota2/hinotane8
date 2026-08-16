"""戦略の共通インターフェース。

設計上いちばん大事な点: **戦略は「全日付ぶんの判定」を返す**。
スクリーニングは最終行だけを見て、バックテストは全行を舐める。
同じ ``evaluate()`` を両方が呼ぶので、
「バックテストのコードと本番のコードが微妙に違って結果が食い違う」
という自動売買で最も多い事故が構造的に起きない。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class StrategySignal:
    """1 銘柄・1 戦略のエントリー候補。数量はまだ決まっていない（risk.py が決める）。"""

    code: str
    name: str
    signal_date: date
    strategy: str
    ref_price: float          # 判定に使った終値
    entry_price: float        # 想定エントリー価格（翌営業日の寄り想定）
    stop_price: float         # 損切り価格
    target_price: float       # 利確目標
    score: float              # 候補の優先順位づけ（大きいほど優先）
    reasons: list[str] = field(default_factory=list)
    side: str = "long"

    @property
    def risk_per_share(self) -> float:
        return max(self.entry_price - self.stop_price, 0.0)

    @property
    def reward_risk(self) -> float:
        if self.risk_per_share <= 0:
            return 0.0
        return (self.target_price - self.entry_price) / self.risk_per_share


class Strategy(ABC):
    """全戦略の基底クラス。

    ``name``           : 設定ファイルや通知に出る識別子
    ``label``          : 人間向けの短い説明
    ``max_holding_days``: 時間切れ決済までの営業日数（塩漬け防止）
    """

    name: str = "base"
    label: str = ""
    max_holding_days: int = 20

    #: トレーリングストップの ATR 倍率。None なら固定ストップ。
    #: エントリー後の最高値から この倍率×ATR 下に損切りを切り上げていく。
    #: 上げ相場で勝ち馬を早々に手放さないための仕組み。
    trailing_atr_mult: float | None = None

    #: 固定の利確目標を持つか。
    #: トレーリングストップで降りる戦略は利確目標を使わないが、R 倍率の計算と
    #: 表示のために遠い値を入れてある。これを「利確目標 +30%」と通知に出すと、
    #: 到達する気のない数字を本物として見せることになるので、False にして
    #: 「トレーリングで撤退」と表示させる。
    has_profit_target: bool = True

    #: 判定に必要な最低バー数
    warmup_bars: int = 80

    @abstractmethod
    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        """``indicators.enrich()`` 済みの日足を受け取り、判定列を足して返す。

        追加する列:
          entry        bool   その日の引けでエントリー条件が成立したか
          stop_price   float  成立時の損切り価格
          target_price float  成立時の利確目標
          score        float  候補の強さ
          reason       str    なぜ候補なのか（日本語・通知にそのまま載る）
        """

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _blank(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = False
        out["stop_price"] = float("nan")
        out["target_price"] = float("nan")
        out["score"] = 0.0
        out["reason"] = ""
        return out

    def latest_signal(
        self, df: pd.DataFrame, code: str, name: str
    ) -> StrategySignal | None:
        """最終行を見て、シグナルが立っていれば StrategySignal を作る。"""
        if len(df) < self.warmup_bars:
            return None
        out = self.evaluate(df)
        row = out.iloc[-1]
        if not bool(row["entry"]):
            return None
        if not all(
            pd.notna(row[c]) and row[c] > 0 for c in ("close", "stop_price", "target_price")
        ):
            return None
        entry_price = float(row["close"])  # 翌営業日の寄りは当日終値で近似
        if float(row["stop_price"]) >= entry_price:
            return None
        return StrategySignal(
            code=code,
            name=name,
            signal_date=pd.Timestamp(row["date"]).date(),
            strategy=self.name,
            ref_price=float(row["close"]),
            entry_price=entry_price,
            stop_price=float(row["stop_price"]),
            target_price=float(row["target_price"]),
            score=float(row["score"]),
            reasons=[r for r in str(row["reason"]).split("|") if r],
        )


_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"未知の戦略 '{name}'。利用可能: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def available() -> list[str]:
    return sorted(_REGISTRY)
