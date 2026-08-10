"""Webhook の安全性テスト。

ここが破られると、第三者が勝手に「承認」を送って発注させられる。
署名検証とユーザーIDホワイトリストは絶対に緩めないこと。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from hinotane.config import (
    AppConfig,
    ExecutionConfig,
    JQuantsConfig,
    LineConfig,
    RiskConfig,
    ScreenerConfig,
)
from hinotane.db import Database

SECRET = "test-channel-secret"
OWNER = "Uowner00000000000000000000000000"
STRANGER = "Ustranger000000000000000000000000"


def _make_client(tmp_path, monkeypatch, *, allowed: list[str]):
    db_path = tmp_path / "wh.duckdb"
    database = Database(db_path)
    database.init_schema()
    database.execute(
        """
        INSERT INTO signals (id, signal_date, code, name, strategy, side,
                             ref_price, entry_price, stop_price, target_price,
                             quantity, risk_jpy, score, reasons, status, expires_at)
        VALUES ('sig-abc', DATE '2025-06-02', '72030', 'テスト自動車', 'breakout', 'long',
                3000, 3000, 2850, 3375, 300, 45000, 1.0, 'テスト', 'pending', ?)
        """,
        [datetime.now() + timedelta(hours=12)],
    )

    fake_cfg = AppConfig(
        db_path=db_path,
        jquants=JQuantsConfig(api_key="dummy"),
        line=LineConfig(
            channel_access_token="token",
            channel_secret=SECRET,
            allowed_user_ids=allowed,
        ),
        risk=RiskConfig(),
        screener=ScreenerConfig(),
        execution=ExecutionConfig(),
    )

    import hinotane.webhook as wh

    monkeypatch.setattr(wh, "load_config", lambda: fake_cfg)
    # 実際に LINE へ通信せず、返信内容を記録するだけにする
    replies: list[str] = []

    def fake_reply(self, token, text):
        replies.append(text)
        return True

    monkeypatch.setattr(wh.LineNotifier, "reply_text", fake_reply)
    return TestClient(wh.create_app()), database, replies


@pytest.fixture
def client(tmp_path, monkeypatch):
    c, database, _ = _make_client(tmp_path, monkeypatch, allowed=[OWNER])
    yield c, database


@pytest.fixture
def setup_client(tmp_path, monkeypatch):
    """宛先 userId が未登録＝セットアップモードのクライアント。"""
    yield _make_client(tmp_path, monkeypatch, allowed=[])


def _post(client, body: dict, *, secret: str | None = SECRET):
    raw = json.dumps(body).encode()
    sig = ""
    if secret is not None:
        sig = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    return client.post(
        "/line/webhook",
        content=raw,
        headers={"X-Line-Signature": sig, "Content-Type": "application/json"},
    )


def _postback(user_id: str, action: str, sid: str = "sig-abc") -> dict:
    return {
        "events": [
            {
                "type": "postback",
                "replyToken": "rt",
                "source": {"userId": user_id, "type": "user"},
                "postback": {"data": f"a={action}&sid={sid}"},
            }
        ]
    }


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").json() == {"status": "ok"}


def test_rejects_missing_signature(client):
    c, _ = client
    assert _post(c, _postback(OWNER, "approve"), secret=None).status_code == 403


def test_rejects_forged_signature(client):
    c, db = client
    resp = _post(c, _postback(OWNER, "approve"), secret="wrong-secret")
    assert resp.status_code == 403
    # 状態が変わっていないこと
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "pending"


def test_ignores_non_whitelisted_user(client):
    c, db = client
    resp = _post(c, _postback(STRANGER, "approve"))
    assert resp.status_code == 200          # LINE には 200 を返す
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "pending"


def test_owner_approval_updates_status_and_records_audit(client):
    c, db = client
    assert _post(c, _postback(OWNER, "approve")).status_code == 200
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "approved"
    approvals = db.query("SELECT decision, decided_by FROM approvals")
    assert len(approvals) == 1
    assert approvals.iloc[0]["decision"] == "approve"
    assert approvals.iloc[0]["decided_by"] == OWNER


def test_owner_rejection(client):
    c, db = client
    _post(c, _postback(OWNER, "reject"))
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "rejected"


def test_double_approval_is_idempotent(client):
    c, db = client
    _post(c, _postback(OWNER, "approve"))
    _post(c, _postback(OWNER, "reject"))   # 2 回目は無視される
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "approved"


def test_expired_signal_cannot_be_approved(client):
    c, db = client
    db.execute(
        "UPDATE signals SET expires_at = ? WHERE id = 'sig-abc'",
        [datetime.now() - timedelta(hours=1)],
    )
    _post(c, _postback(OWNER, "approve"))
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "expired"


def test_setup_mode_replies_with_user_id(setup_client):
    """宛先未登録のうちは、話しかけると userId を教え返す。"""
    c, _, replies = setup_client
    body = {
        "events": [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"userId": STRANGER, "type": "user"},
                "message": {"type": "text", "text": "こんにちは"},
            }
        ]
    }
    assert _post(c, body).status_code == 200
    assert len(replies) == 1
    assert STRANGER in replies[0]
    assert "LINE_ALLOWED_USER_IDS" in replies[0]


def test_setup_mode_never_executes_approvals(setup_client):
    """セットアップモード中は、誰が承認ボタンを押しても状態が変わらないこと。

    userId を教え返す都合で誰でも話しかけられる状態なので、
    ここで承認が通ってしまうと第三者に発注させられる。
    """
    c, db, replies = setup_client
    assert _post(c, _postback(STRANGER, "approve")).status_code == 200
    assert _post(c, _postback(OWNER, "approve")).status_code == 200

    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "pending"
    assert db.query("SELECT count(*) AS n FROM approvals").iloc[0]["n"] == 0
    # 返ってきたのは userId の案内だけ
    assert all("セットアップモード" in r for r in replies)


def test_setup_mode_still_verifies_signature(setup_client):
    c, _, replies = setup_client
    body = {
        "events": [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"userId": STRANGER, "type": "user"},
                "message": {"type": "text", "text": "こんにちは"},
            }
        ]
    }
    assert _post(c, body, secret="wrong-secret").status_code == 403
    assert replies == []


def test_text_stop_command_cancels_pending_signals(client):
    c, db = client
    body = {
        "events": [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"userId": OWNER, "type": "user"},
                "message": {"type": "text", "text": "停止"},
            }
        ]
    }
    assert _post(c, body).status_code == 200
    assert db.query("SELECT status FROM signals").iloc[0]["status"] == "rejected"
