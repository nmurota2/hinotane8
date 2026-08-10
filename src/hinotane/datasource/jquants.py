"""J-Quants API クライアント（V1 / V2 両対応）。

- V2（2025年12月〜）: ダッシュボードで発行した API キーを ``x-api-key`` ヘッダで送る。
  レスポンスのカラム名が短縮形（Open→O, Close→C など）になっている。
- V1: メールアドレス/パスワード → リフレッシュトークン → ID トークン の 3 段階。
  ``Authorization: Bearer <idToken>`` を付ける。

どちらのカラム命名でも動くよう、正規化テーブルを通してから返す。
未知のカラム名だった場合は、実際に返ってきたカラム一覧をエラーに含めるので、
そのまま報告してもらえれば対応表を 1 行足すだけで直せる。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from ..config import JQuantsConfig

log = logging.getLogger(__name__)

# 正規化後の名前 -> API が返しうる名前の候補（優先度順）。
# 調整後株価（Adjustment*）を優先する。株式分割をまたいでも連続した系列になり、
# バックテストが分割で壊れないため。
_QUOTE_COLUMNS: dict[str, tuple[str, ...]] = {
    "code": ("Code", "code", "C0", "Ticker"),
    "date": ("Date", "date", "D"),
    "open": ("AdjustmentOpen", "AO", "Open", "O", "open"),
    "high": ("AdjustmentHigh", "AH", "High", "H", "high"),
    "low": ("AdjustmentLow", "AL", "Low", "L", "low"),
    "close": ("AdjustmentClose", "AC", "Close", "C", "close"),
    "volume": ("AdjustmentVolume", "AV", "Volume", "V", "volume"),
    "turnover_value": ("TurnoverValue", "TV", "turnover_value"),
}

_LISTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "code": ("Code", "code"),
    "name": ("CompanyName", "CN", "name"),
    "market_code": ("MarketCode", "MC", "market_code"),
    "sector17_code": ("Sector17Code", "S17", "sector17_code"),
    "sector33_code": ("Sector33Code", "S33", "sector33_code"),
    "scale_category": ("ScaleCategory", "SC", "scale_category"),
}


class JQuantsError(RuntimeError):
    pass


def _normalize(df: pd.DataFrame, mapping: dict[str, tuple[str, ...]], required: set[str]) -> pd.DataFrame:
    """API のカラム名を内部の正規名に揃える。"""
    out = pd.DataFrame(index=df.index)
    missing: list[str] = []
    for target, candidates in mapping.items():
        for cand in candidates:
            if cand in df.columns:
                out[target] = df[cand]
                break
        else:
            if target in required:
                missing.append(target)
            else:
                out[target] = None
    if missing:
        raise JQuantsError(
            f"J-Quants のレスポンスから必須カラム {missing} を特定できませんでした。"
            f" 実際に返ってきたカラム: {sorted(df.columns.tolist())}。"
            " このカラム一覧をそのまま伝えてもらえれば対応表を更新します。"
        )
    return out


def display_code(code: str) -> str:
    """J-Quants の 5 桁コードを、普段目にする 4 桁表記に直す。

    J-Quants は 4 桁銘柄コードの末尾に 0 を足した 5 桁で返す（トヨタ=72030）。
    """
    code = str(code).strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


class JQuantsClient:
    def __init__(self, cfg: JQuantsConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._id_token: str | None = None
        self._id_token_expires_at: datetime | None = None

    # ------------------------------------------------------------------ 認証

    def _headers(self) -> dict[str, str]:
        if self.cfg.auth_mode == "apikey":
            return {"x-api-key": self.cfg.api_key or ""}
        return {"Authorization": f"Bearer {self._ensure_id_token()}"}

    def _ensure_id_token(self) -> str:
        now = datetime.now()
        if self._id_token and self._id_token_expires_at and now < self._id_token_expires_at:
            return self._id_token

        refresh_token = self.cfg.refresh_token
        if not refresh_token:
            if not (self.cfg.mail_address and self.cfg.password):
                raise JQuantsError(
                    "J-Quants の認証情報がありません。"
                    " JQUANTS_API_KEY（V2）か、JQUANTS_REFRESH_TOKEN、"
                    " または JQUANTS_MAIL_ADDRESS + JQUANTS_PASSWORD を .env に設定してください。"
                )
            resp = self._session.post(
                f"{self.cfg.base_url}/token/auth_user",
                json={"mailaddress": self.cfg.mail_address, "password": self.cfg.password},
                timeout=self.cfg.timeout_sec,
            )
            if resp.status_code != 200:
                raise JQuantsError(f"リフレッシュトークンの取得に失敗: {resp.status_code} {resp.text[:300]}")
            refresh_token = resp.json()["refreshToken"]

        resp = self._session.post(
            f"{self.cfg.base_url}/token/auth_refresh",
            params={"refreshtoken": refresh_token},
            timeout=self.cfg.timeout_sec,
        )
        if resp.status_code != 200:
            raise JQuantsError(f"ID トークンの取得に失敗: {resp.status_code} {resp.text[:300]}")
        self._id_token = resp.json()["idToken"]
        # ID トークンの寿命は 24 時間。余裕を持って 20 時間で切る。
        self._id_token_expires_at = datetime.now() + timedelta(hours=20)
        return self._id_token

    # ------------------------------------------------------------------ 低レベル

    def _get(self, path: str, params: dict) -> list[dict]:
        """1 エンドポイントを pagination_key が尽きるまで取得する。"""
        url = f"{self.cfg.base_url}{path}"
        rows: list[dict] = []
        page_params = dict(params)
        data_key: str | None = None

        while True:
            payload = self._get_with_retry(url, page_params)
            if data_key is None:
                # 'daily_quotes' / 'info' など、エンドポイントごとに配列のキー名が違う
                candidates = [k for k, v in payload.items() if isinstance(v, list)]
                if not candidates:
                    return rows
                data_key = candidates[0]
            rows.extend(payload.get(data_key) or [])

            next_key = payload.get("pagination_key")
            if not next_key:
                return rows
            page_params = {**params, "pagination_key": next_key}

    def _get_with_retry(self, url: str, params: dict) -> dict:
        last_err: str = ""
        for attempt in range(self.cfg.max_retries):
            try:
                resp = self._session.get(
                    url, params=params, headers=self._headers(), timeout=self.cfg.timeout_sec
                )
            except requests.RequestException as exc:
                last_err = str(exc)
                time.sleep(2**attempt)
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 403) and self.cfg.auth_mode == "token":
                # ID トークン失効。作り直して 1 回だけやり直す。
                self._id_token = None
                last_err = f"{resp.status_code} {resp.text[:200]}"
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"{resp.status_code} {resp.text[:200]}"
                time.sleep(2**attempt)
                continue
            raise JQuantsError(f"J-Quants API エラー {resp.status_code}: {resp.text[:300]}")

        raise JQuantsError(f"J-Quants API に {self.cfg.max_retries} 回失敗しました: {last_err}")

    # ------------------------------------------------------------------ 公開 API

    def listed_info(self, target: date | None = None) -> pd.DataFrame:
        """上場銘柄一覧。"""
        params: dict[str, str] = {}
        if target:
            params["date"] = target.strftime("%Y-%m-%d")
        rows = self._get("/listed/info", params)
        if not rows:
            return pd.DataFrame(columns=list(_LISTED_COLUMNS))
        df = _normalize(pd.DataFrame(rows), _LISTED_COLUMNS, required={"code", "name"})
        df["code"] = df["code"].astype(str)
        return df.drop_duplicates(subset=["code"], keep="last")

    def daily_quotes_by_date(self, target: date) -> pd.DataFrame:
        """特定日の全銘柄日足。日次更新はこちらが効率的（1 リクエストで全銘柄）。"""
        rows = self._get("/prices/daily_quotes", {"date": target.strftime("%Y-%m-%d")})
        return self._to_quotes_df(rows)

    def daily_quotes_by_code(self, code: str, start: date, end: date) -> pd.DataFrame:
        """特定銘柄の期間日足。初回のヒストリカル取得に使う。"""
        rows = self._get(
            "/prices/daily_quotes",
            {
                "code": code,
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
            },
        )
        return self._to_quotes_df(rows)

    @staticmethod
    def _to_quotes_df(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=list(_QUOTE_COLUMNS))
        df = _normalize(
            pd.DataFrame(rows),
            _QUOTE_COLUMNS,
            required={"code", "date", "close"},
        )
        df["code"] = df["code"].astype(str)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for col in ("open", "high", "low", "close", "volume", "turnover_value"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        # 売買代金が無い契約プランでは 終値 × 出来高 で近似する
        approx = df["close"] * df["volume"]
        df["turnover_value"] = df["turnover_value"].fillna(approx)
        # 終値が無い日（売買停止など）は捨てる
        return df.dropna(subset=["close"]).reset_index(drop=True)
