"""環境変数から設定を読み込む。

設定はすべて .env ファイル（または環境変数）で与える。
コードに証券口座のパスワードや API キーを直接書かないこと。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

JST = ZoneInfo("Asia/Tokyo")

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def now() -> datetime:
    """タイムゾーンを持たない「日本時間の現在時刻」を返す。

    VPS のシステム時刻が UTC のままだと、素の ``datetime.now()`` は UTC を返し、
    承認の有効期限が 9 時間ずれる（＝夜に承認したものが朝の執行時点で
    期限切れ扱いになる、あるいはその逆）。ホストの TZ 設定に依存しないよう、
    必ず JST に変換してから naive に戻す。

    naive のまま扱うのは、DuckDB の TIMESTAMP 列が naive で返るため。
    aware と naive を混ぜると比較で TypeError になる。
    """
    return datetime.now(JST).replace(tzinfo=None)


def today():
    """日本時間での「今日」。"""
    return datetime.now(JST).date()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw in (None, ""):
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


JQUANTS_DEFAULT_BASE_URL = "https://api.jquants.com/v2"


def _normalize_jquants_base_url(raw: str | None) -> str:
    """設定された J-Quants のベース URL を V2 に正規化する。

    V1（``.../v1``）は 2026年6月1日に廃止済みで、叩くと HTTP 410 が返るだけ。
    古い .env が残っていても動くよう、ここで黙って V2 に読み替える。
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return JQUANTS_DEFAULT_BASE_URL
    if url.endswith("/v1"):
        return url[: -len("/v1")] + "/v2"
    if not url.endswith("/v2"):
        return url + "/v2"
    return url


@dataclass(frozen=True)
class JQuantsConfig:
    """J-Quants API（V2）の接続設定。

    V2 はダッシュボードで発行した API キーを ``x-api-key`` ヘッダで送るだけ。
    V1 のメールアドレス/パスワード → リフレッシュトークン → ID トークンという
    3 段階の認証は、V1 の廃止（2026年6月1日）とともに使えなくなっている。
    """

    base_url: str = _normalize_jquants_base_url(os.getenv("JQUANTS_BASE_URL"))
    api_key: str | None = (os.getenv("JQUANTS_API_KEY") or "").strip() or None
    timeout_sec: int = _int("JQUANTS_TIMEOUT_SEC", 30)
    max_retries: int = _int("JQUANTS_MAX_RETRIES", 4)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class LineConfig:
    """LINE Messaging API の設定。

    LINE Notify は 2025/3/31 に終了済みのため Messaging API を使う。
    LINE Developers コンソールで Messaging API チャネルを作成して取得する。
    """

    channel_access_token: str | None = os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or None
    channel_secret: str | None = os.getenv("LINE_CHANNEL_SECRET") or None
    # 通知の宛先。自分の userId（U から始まる 33 文字）。
    # 承認操作を受け付けるのもこの ID だけに限定する。
    allowed_user_ids: list[str] = field(default_factory=lambda: _list("LINE_ALLOWED_USER_IDS", []))

    @property
    def configured(self) -> bool:
        """LINE と通信できるか（返信だけなら宛先の登録は不要）。"""
        return bool(self.channel_access_token and self.channel_secret)

    @property
    def can_push(self) -> bool:
        """こちらから通知を送れるか。宛先 userId の登録が必須。"""
        return self.configured and bool(self.allowed_user_ids)

    @property
    def setup_mode(self) -> bool:
        """トークンはあるが宛先 userId が未登録の状態。

        この間、Bot は「あなたの userId はこれです」とだけ返し、
        承認などの操作は一切受け付けない。userId を調べるための一時的な状態。
        """
        return self.configured and not self.allowed_user_ids


@dataclass(frozen=True)
class RiskConfig:
    """リスク管理パラメータ。ここが実質的な「安全装置」。"""

    # 運用資金（円）。ポジションサイズ計算の基準。
    equity_jpy: float = _float("RISK_EQUITY_JPY", 1_000_000)
    # 1 トレードで許容する損失（運用資金に対する割合）。0.01 = 1%
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.01)
    # 1 銘柄に投じる上限（運用資金に対する割合）
    max_position_pct: float = _float("RISK_MAX_POSITION_PCT", 0.20)
    # 同時に保有できる最大銘柄数
    max_open_positions: int = _int("RISK_MAX_OPEN_POSITIONS", 5)
    # 1 日に通知・発注してよい最大シグナル数
    max_signals_per_day: int = _int("RISK_MAX_SIGNALS_PER_DAY", 3)
    # 累計ドローダウンがこの割合を超えたら全シグナルを停止（キルスイッチ）
    kill_switch_drawdown_pct: float = _float("RISK_KILL_SWITCH_DRAWDOWN_PCT", 0.15)
    # 承認の有効期限（時間）。期限切れの承認は執行しない。
    approval_ttl_hours: int = _int("RISK_APPROVAL_TTL_HOURS", 18)
    # 日本株の売買単位
    lot_size: int = _int("RISK_LOT_SIZE", 100)


@dataclass(frozen=True)
class ScreenerConfig:
    """スクリーニングの対象と足切り条件。"""

    strategies: list[str] = field(
        default_factory=lambda: _list("SCREENER_STRATEGIES", ["breakout", "pullback", "reversal"])
    )
    # 流動性フィルタ: 直近 20 日平均売買代金の下限（円）。
    # 薄い銘柄はバックテストと実約定が乖離するので必ず切る。
    min_turnover_jpy: float = _float("SCREENER_MIN_TURNOVER_JPY", 100_000_000)
    # 株価の下限・上限（円）。単元 100 株で買える価格帯に絞る。
    min_price: float = _float("SCREENER_MIN_PRICE", 300)
    max_price: float = _float("SCREENER_MAX_PRICE", 20_000)
    # 指標計算に必要な最低営業日数
    min_history_days: int = _int("SCREENER_MIN_HISTORY_DAYS", 120)
    # 対象市場（J-Quants の MarketCode）。0111=プライム, 0112=スタンダード, 0113=グロース
    market_codes: list[str] = field(
        default_factory=lambda: _list("SCREENER_MARKET_CODES", ["0111", "0112"])
    )


@dataclass(frozen=True)
class ExecutionConfig:
    """執行設定。デフォルトは必ず擬似発注（PaperBroker）。"""

    # true にしない限り実際の注文は絶対に出ない
    live_trading: bool = _bool("LIVE_TRADING", False)
    broker: str = os.getenv("BROKER", "paper")
    # 承認なしで自動発注するか。デフォルトは false（LINE で承認したものだけ執行）
    auto_execute_without_approval: bool = _bool("AUTO_EXECUTE_WITHOUT_APPROVAL", False)
    # 発注時のスリッページ想定（バックテスト・擬似約定用）
    slippage_pct: float = _float("EXEC_SLIPPAGE_PCT", 0.002)
    # 売買手数料の想定（片道・率）
    commission_pct: float = _float("EXEC_COMMISSION_PCT", 0.0005)


@dataclass(frozen=True)
class AppConfig:
    db_path: Path = Path(os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "hinotane.duckdb")))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # データがこの日数より古かったら通知に警告を出す
    stale_data_warn_days: int = _int("STALE_DATA_WARN_DAYS", 5)
    webhook_host: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    webhook_port: int = _int("WEBHOOK_PORT", 8080)

    jquants: JQuantsConfig = field(default_factory=JQuantsConfig)
    line: LineConfig = field(default_factory=LineConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    screener: ScreenerConfig = field(default_factory=ScreenerConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


def load_config() -> AppConfig:
    cfg = AppConfig()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg
