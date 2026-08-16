"""J-Quants API（V2）クライアント。

V1 は 2026年6月1日に廃止済みで、叩くと HTTP 410 が返るだけなので V2 のみ対応する。

V2 の要点（公式クライアント jquants-api-client 2.4.0 のソースで確認）:
  * ベース URL  : https://api.jquants.com/v2
  * 認証        : ``x-api-key`` ヘッダに API キーを載せるだけ
  * 上場銘柄一覧: GET /equities/master          （パラメータ: code, date）
  * 株価四本値  : GET /equities/bars/daily      （パラメータ: code, date, from, to）
  * レスポンス  : {"data": [...], "pagination_key": "..."} 形式
  * カラム名が V1 から短縮された（Close → C、AdjustmentClose → AdjC など）

カラム名は正規化テーブルを通してから返す。未知の名前だった場合は、実際に
返ってきたカラム一覧をエラーに含めるので、それを見れば対応表を 1 行足すだけで直せる。
"""

from __future__ import annotations

import logging
import time
from datetime import date

import pandas as pd
import requests

from ..config import JQuantsConfig

log = logging.getLogger(__name__)

EQ_MASTER_PATH = "/equities/master"
EQ_BARS_DAILY_PATH = "/equities/bars/daily"

# 正規化後の名前 -> API が返しうる名前の候補（優先度順）。
#
# 調整後株価（Adj*）を優先する。株式分割をまたいでも連続した系列になり、
# バックテストが分割で壊れないため。括弧内は V1 時代の名前で、
# 古いデータや別経路のデータを読ませたときのための保険。
_QUOTE_COLUMNS: dict[str, tuple[str, ...]] = {
    "code": ("Code", "code"),
    "date": ("Date", "date"),
    "open": ("AdjO", "O", "AdjustmentOpen", "Open"),
    "high": ("AdjH", "H", "AdjustmentHigh", "High"),
    "low": ("AdjL", "L", "AdjustmentLow", "Low"),
    "close": ("AdjC", "C", "AdjustmentClose", "Close"),
    "volume": ("AdjVo", "Vo", "AdjustmentVolume", "Volume"),
    "turnover_value": ("Va", "TurnoverValue"),
}

_LISTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "code": ("Code", "code"),
    "name": ("CoName", "CompanyName"),
    "market_code": ("Mkt", "MarketCode"),
    "sector17_code": ("S17", "Sector17Code"),
    "sector33_code": ("S33", "Sector33Code"),
    "scale_category": ("ScaleCat", "ScaleCategory"),
}


class JQuantsError(RuntimeError):
    """J-Quants API の呼び出しに失敗した。"""


class JQuantsAuthError(JQuantsError):
    """認証に失敗した。キーの誤り、またはプラン未選択が原因。

    「アカウントは作ったのに動かない」の大半は、ダッシュボードでの
    プラン選択（Free でも必要）が終わっていないケース。
    """


class JQuantsNetworkError(JQuantsError):
    """API に到達できなかった。ネットワーク・DNS・プロキシ・障害など。

    設定の問題ではないので、ユーザーに設定を疑わせないよう区別する。
    """


class JQuantsGoneError(JQuantsError):
    """廃止されたエンドポイントを叩いた（HTTP 410）。

    ほぼ確実に V1 の URL が残っている。
    """


def _normalize(
    df: pd.DataFrame, mapping: dict[str, tuple[str, ...]], required: set[str]
) -> pd.DataFrame:
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

    # ------------------------------------------------------------------ 低レベル

    def _headers(self) -> dict[str, str]:
        if not self.cfg.api_key:
            raise JQuantsAuthError(
                "J-Quants の API キーが設定されていません。"
                " ダッシュボードの［設定］→［API キー］で発行して、"
                " .env の JQUANTS_API_KEY に設定してください。"
            )
        return {"x-api-key": self.cfg.api_key}

    def _get(self, path: str, params: dict) -> list[dict]:
        """1 エンドポイントを pagination_key が尽きるまで取得する。"""
        url = f"{self.cfg.base_url}{path}"
        rows: list[dict] = []
        page_params = dict(params)

        while True:
            payload = self._get_with_retry(url, page_params)
            batch = payload.get("data")
            if not isinstance(batch, list):
                # 想定外の形。配列を持つキーがあれば拾う（仕様変更への保険）
                batch = next(
                    (v for v in payload.values() if isinstance(v, list)), []
                )
            rows.extend(batch)

            next_key = payload.get("pagination_key")
            if not next_key:
                return rows
            page_params = {**params, "pagination_key": next_key}

    def _get_with_retry(self, url: str, params: dict) -> dict:
        last_err: str = ""
        network_failure = False
        auth_failure = False

        for attempt in range(self.cfg.max_retries):
            try:
                resp = self._session.get(
                    url, params=params, headers=self._headers(), timeout=self.cfg.timeout_sec
                )
            except requests.RequestException as exc:
                # DNS・プロキシ・タイムアウトなど。設定ではなく到達性の問題。
                network_failure = True
                last_err = str(exc)
                time.sleep(2**attempt)
                continue

            network_failure = False

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 410:
                raise JQuantsGoneError(
                    "廃止されたエンドポイントを呼び出しました（HTTP 410）。"
                    f" 呼び出し先: {url}。"
                    " .env の JQUANTS_BASE_URL が V1 のままになっている可能性があります。"
                    " 正しくは https://api.jquants.com/v2 です。"
                )

            if resp.status_code in (401, 403):
                # 再試行しても結果は変わらない
                auth_failure = True
                raise JQuantsAuthError(
                    f"認証に失敗しました（{resp.status_code}）: {resp.text[:200]}"
                )

            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"{resp.status_code} {resp.text[:200]}"
                time.sleep(2**attempt)
                continue

            raise JQuantsError(f"J-Quants API エラー {resp.status_code}: {resp.text[:300]}")

        if network_failure:
            raise JQuantsNetworkError(
                f"J-Quants API に接続できませんでした（{self.cfg.max_retries} 回試行）: {last_err}"
            )
        if auth_failure:
            raise JQuantsAuthError(f"認証に失敗しました: {last_err}")
        raise JQuantsError(f"J-Quants API に {self.cfg.max_retries} 回失敗しました: {last_err}")

    # ------------------------------------------------------------------ 公開 API

    def listed_info(self, target: date | None = None) -> pd.DataFrame:
        """上場銘柄一覧（V2: /equities/master）。"""
        params: dict[str, str] = {}
        if target:
            params["date"] = target.strftime("%Y-%m-%d")
        rows = self._get(EQ_MASTER_PATH, params)
        if not rows:
            return pd.DataFrame(columns=list(_LISTED_COLUMNS))
        df = _normalize(pd.DataFrame(rows), _LISTED_COLUMNS, required={"code", "name"})
        df["code"] = df["code"].astype(str)
        return df.drop_duplicates(subset=["code"], keep="last")

    def daily_quotes_by_date(self, target: date) -> pd.DataFrame:
        """特定日の全銘柄日足。日次更新はこちらが効率的（1 リクエストで全銘柄）。"""
        rows = self._get(EQ_BARS_DAILY_PATH, {"date": target.strftime("%Y-%m-%d")})
        return self._to_quotes_df(rows)

    def daily_quotes_by_code(self, code: str, start: date, end: date) -> pd.DataFrame:
        """特定銘柄の期間日足。1 銘柄を深く見たいときに使う。"""
        rows = self._get(
            EQ_BARS_DAILY_PATH,
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


__all__ = [
    "JQuantsAuthError",
    "JQuantsClient",
    "JQuantsError",
    "JQuantsGoneError",
    "JQuantsNetworkError",
    "display_code",
]
