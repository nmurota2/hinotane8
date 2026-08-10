"""LINE Messaging API による通知。

LINE Notify は 2025/3/31 に終了済みのため Messaging API を使う。

課金の考え方:
  - push（こちらから送る）は課金対象。無料プランは月 200 通まで。
  - reply（ユーザーの発言・操作への返信）は **課金対象外**。
  1 日 1 回の候補配信なら月 20 通程度なので、無料枠に十分収まる。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any

import requests

from ..config import LineConfig
from ..datasource.jquants import display_code
from ..risk import SizedSignal
from ..strategies.base import get_strategy

log = logging.getLogger(__name__)

PUSH_URL = "https://api.line.me/v2/bot/message/push"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# Flex のカルーセルは 12 バブルまで
MAX_BUBBLES = 12


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """Webhook の署名検証。

    これを省くと、第三者が偽の「承認」リクエストを投げて勝手に発注させられる。
    必ず検証すること。
    """
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")


def _row(label: str, value: str, *, color: str = "#333333", bold: bool = False) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#8c8c8c", "flex": 4},
            {
                "type": "text",
                "text": value,
                "size": "sm",
                "color": color,
                "flex": 6,
                "align": "end",
                "weight": "bold" if bold else "regular",
                "wrap": True,
            },
        ],
    }


def _bubble(item: SizedSignal) -> dict[str, Any]:
    s = item.signal
    try:
        label = get_strategy(s.strategy).label
    except KeyError:
        label = s.strategy

    loss_pct = (s.stop_price / s.entry_price - 1) * 100
    gain_pct = (s.target_price / s.entry_price - 1) * 100

    reason_lines = [
        {
            "type": "text",
            "text": f"・{r}",
            "size": "xs",
            "color": "#555555",
            "wrap": True,
        }
        for r in s.reasons[:5]
    ] or [{"type": "text", "text": "・条件成立", "size": "xs", "color": "#555555"}]

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1f4e79",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "text",
                    "text": s.name[:24],
                    "color": "#ffffff",
                    "weight": "bold",
                    "size": "lg",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": f"{display_code(s.code)}  /  {label}",
                    "color": "#c9dcf0",
                    "size": "xs",
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "xs",
                    "contents": reason_lines,
                },
                {"type": "separator", "margin": "md"},
                _row("想定エントリー", f"{s.entry_price:,.0f} 円", bold=True),
                _row("損切り", f"{s.stop_price:,.0f} 円 ({loss_pct:+.1f}%)", color="#c0392b"),
                _row("利確目標", f"{s.target_price:,.0f} 円 ({gain_pct:+.1f}%)", color="#1e8449"),
                _row("リワード/リスク", f"{s.reward_risk:.1f} 倍"),
                {"type": "separator", "margin": "md"},
                _row("提案数量", f"{item.quantity:,} 株"),
                _row("必要資金", f"{item.cost_jpy:,.0f} 円"),
                _row("⚠️ 最大損失", f"{item.risk_jpy:,.0f} 円", color="#c0392b", bold=True),
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "paddingAll": "10px",
            "contents": [
                {
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "見送る",
                        "data": f"a=reject&sid={item.signal_key}",
                        "displayText": f"{display_code(s.code)} を見送ります",
                    },
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#1f4e79",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "買う",
                        "data": f"a=approve&sid={item.signal_key}",
                        "displayText": f"{display_code(s.code)} を承認しました",
                    },
                },
            ],
        },
    }


class LineNotifier:
    def __init__(self, cfg: LineConfig):
        self.cfg = cfg
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return self.cfg.configured

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.channel_access_token}",
            "Content-Type": "application/json",
        }

    def _post(self, url: str, payload: dict) -> bool:
        resp = self._session.post(url, json=payload, headers=self._headers(), timeout=20)
        if resp.status_code != 200:
            log.error("LINE 送信失敗 %s: %s", resp.status_code, resp.text[:300])
            return False
        return True

    def push_text(self, text: str) -> bool:
        if not self.enabled:
            log.warning("LINE 未設定のため送信をスキップ: %s", text[:80])
            return False
        ok = True
        for user_id in self.cfg.allowed_user_ids:
            ok &= self._post(
                PUSH_URL,
                {"to": user_id, "messages": [{"type": "text", "text": text[:4900]}]},
            )
        return ok

    def push_signals(
        self,
        items: list[SizedSignal],
        *,
        header_text: str,
        warnings: list[str] | None = None,
    ) -> bool:
        """候補銘柄カードを送る。"""
        if not self.enabled:
            log.warning("LINE 未設定のため送信をスキップしました")
            return False
        if not items:
            return self.push_text(f"{header_text}\n本日の候補はありません。")

        messages: list[dict[str, Any]] = [{"type": "text", "text": header_text}]
        for w in warnings or []:
            messages.append({"type": "text", "text": f"⚠️ {w}"})

        messages.append(
            {
                "type": "flex",
                "altText": f"本日の候補 {len(items)} 銘柄",
                "contents": {
                    "type": "carousel",
                    "contents": [_bubble(i) for i in items[:MAX_BUBBLES]],
                },
            }
        )

        ok = True
        for user_id in self.cfg.allowed_user_ids:
            ok &= self._post(PUSH_URL, {"to": user_id, "messages": messages})
        return ok

    def reply_text(self, reply_token: str, text: str) -> bool:
        """ユーザー操作への返信。push と違い課金対象外。"""
        if not self.enabled:
            return False
        return self._post(
            REPLY_URL,
            {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]},
        )
