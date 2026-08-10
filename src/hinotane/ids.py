"""識別子の生成。

シグナル ID は「日付 + 銘柄 + 戦略」から決まる決定的な値にする。
バッチを二重起動しても同じ ID になるので、通知や発注が重複しない。
"""

from __future__ import annotations

import hashlib
from datetime import date


def signal_id(signal_date: date | str, code: str, strategy: str) -> str:
    raw = f"{signal_date}|{code}|{strategy}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]
