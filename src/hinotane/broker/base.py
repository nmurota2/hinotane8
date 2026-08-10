"""発注アダプタの共通インターフェース。

執行を抽象化しておくことで、
  - 開発中は PaperBroker（擬似発注）
  - 本番は TachibanaBroker（立花証券 e支店 API）
  - 将来 米国株をやるなら MoomooBroker
を、戦略側のコードを一切変えずに差し替えられる。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass
class OrderRequest:
    code: str
    name: str
    side: str            # 'buy' | 'sell'
    quantity: int
    limit_price: float | None = None   # None なら成行
    signal_id: str | None = None


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None
    filled_price: float | None
    filled_quantity: int
    message: str
    executed_at: date | None = None


class BrokerAdapter(ABC):
    """発注アダプタ。"""

    name: str = "base"
    is_paper: bool = True

    @abstractmethod
    def buy(self, req: OrderRequest) -> OrderResult:
        ...

    @abstractmethod
    def sell(self, req: OrderRequest) -> OrderResult:
        ...

    def healthcheck(self) -> tuple[bool, str]:
        """発注前の疎通確認。失敗したらその日の執行を止める。"""
        return True, "ok"
