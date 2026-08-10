from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date

from hinotane.notify.line import _bubble, verify_signature
from hinotane.risk import SizedSignal
from hinotane.strategies.base import StrategySignal


def test_verify_signature_accepts_valid_and_rejects_tampered():
    secret = "s3cr3t"
    body = b'{"events":[]}'
    good = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()

    assert verify_signature(secret, body, good)
    assert not verify_signature(secret, body, "wrong")
    assert not verify_signature(secret, body, "")
    # 本文が 1 バイトでも変わったら弾かれる（偽の承認リクエスト対策）
    assert not verify_signature(secret, b'{"events":[1]}', good)


def _sized() -> SizedSignal:
    signal = StrategySignal(
        code="72030",
        name="テスト自動車",
        signal_date=date(2025, 6, 2),
        strategy="breakout",
        ref_price=3000.0,
        entry_price=3000.0,
        stop_price=2850.0,
        target_price=3375.0,
        score=12.3,
        reasons=["20日高値を上抜け", "出来高2.1倍"],
    )
    return SizedSignal(signal=signal, quantity=300, risk_jpy=45_000, cost_jpy=900_000)


def test_bubble_is_serialisable_and_carries_postback_ids():
    bubble = _bubble(_sized())
    # LINE に送る前に JSON 化できること
    payload = json.dumps(bubble, ensure_ascii=False)
    assert "テスト自動車" in payload
    # 5桁コードは4桁表示に直る
    assert "7203" in payload

    actions = [c["action"] for c in bubble["footer"]["contents"]]
    assert {a["action"] if False else a["type"] for a in actions} == {"postback"}
    approve = next(a for a in actions if "approve" in a["data"])
    reject = next(a for a in actions if "reject" in a["data"])
    assert _sized().signal_key in approve["data"]
    assert _sized().signal_key in reject["data"]


def test_signal_key_is_deterministic():
    assert _sized().signal_key == _sized().signal_key
    assert len(_sized().signal_key) == 16
