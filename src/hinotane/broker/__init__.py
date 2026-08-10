"""発注アダプタの選択。

安全側に倒すため、以下をすべて満たさない限り必ず PaperBroker になる:
  - LIVE_TRADING=true
  - BROKER が paper 以外
  - そのアダプタの healthcheck() が成功する
"""

from __future__ import annotations

import logging

from ..config import AppConfig
from ..db import Database
from .base import BrokerAdapter, OrderRequest, OrderResult
from .paper import PaperBroker
from .tachibana import TachibanaBroker

log = logging.getLogger(__name__)

_BROKERS = {
    "paper": PaperBroker,
    "tachibana": TachibanaBroker,
}


def get_broker(cfg: AppConfig, db: Database) -> BrokerAdapter:
    name = cfg.execution.broker.lower()

    if not cfg.execution.live_trading:
        if name != "paper":
            log.warning(
                "LIVE_TRADING=false のため、BROKER=%s の指定を無視して擬似発注で動作します", name
            )
        return PaperBroker(cfg.execution, db)

    if name not in _BROKERS:
        raise ValueError(f"未知の BROKER '{name}'。利用可能: {sorted(_BROKERS)}")

    broker = _BROKERS[name](cfg.execution, db)
    ok, message = broker.healthcheck()
    if not ok:
        log.error("発注アダプタ %s が利用できません。擬似発注に切り替えます: %s", name, message)
        return PaperBroker(cfg.execution, db)

    log.warning("⚠️ 実発注モードで動作します: broker=%s", name)
    return broker


__all__ = [
    "BrokerAdapter",
    "OrderRequest",
    "OrderResult",
    "PaperBroker",
    "TachibanaBroker",
    "get_broker",
]
