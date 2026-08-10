"""DuckDB によるローカルデータストア。

4,000 銘柄 × 20 年の日足を 1 ファイルで扱えて、サーバープロセスも不要なので
VPS 1 台構成に向く。スキーマ変更は SCHEMA_SQL に追記すれば冪等に適用される。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

log = logging.getLogger(__name__)

SCHEMA_SQL = """
-- 上場銘柄マスタ
CREATE TABLE IF NOT EXISTS listed (
    code            VARCHAR PRIMARY KEY,
    name            VARCHAR,
    market_code     VARCHAR,
    sector17_code   VARCHAR,
    sector33_code   VARCHAR,
    scale_category  VARCHAR,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

-- 日足（調整後価格）
CREATE TABLE IF NOT EXISTS daily_quotes (
    code            VARCHAR NOT NULL,
    date            DATE    NOT NULL,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume          DOUBLE,
    turnover_value  DOUBLE,
    PRIMARY KEY (code, date)
);

-- 生成されたシグナル
CREATE TABLE IF NOT EXISTS signals (
    id              VARCHAR PRIMARY KEY,
    signal_date     DATE    NOT NULL,
    code            VARCHAR NOT NULL,
    name            VARCHAR,
    strategy        VARCHAR NOT NULL,
    side            VARCHAR NOT NULL,          -- 'long'
    ref_price       DOUBLE,                    -- シグナル判定時の終値
    entry_price     DOUBLE,                    -- 想定エントリー価格
    stop_price      DOUBLE,                    -- 損切り価格
    target_price    DOUBLE,                    -- 利確目標
    quantity        INTEGER,                   -- 提案数量（単元株）
    risk_jpy        DOUBLE,                    -- この取引で失う可能性のある金額
    score           DOUBLE,
    reasons         VARCHAR,                   -- '|' 区切り
    status          VARCHAR NOT NULL,          -- pending/approved/rejected/expired/executed/failed
    expires_at      TIMESTAMP,
    created_at      TIMESTAMP DEFAULT current_timestamp
);

-- 承認・却下の記録（誰がいつ押したか）
CREATE TABLE IF NOT EXISTS approvals (
    signal_id       VARCHAR NOT NULL,
    decision        VARCHAR NOT NULL,          -- approve/reject
    decided_by      VARCHAR,                   -- LINE userId
    decided_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (signal_id, decided_at)
);

-- ポジション（PaperBroker / 実口座 共通の建玉台帳）
CREATE TABLE IF NOT EXISTS positions (
    id              VARCHAR PRIMARY KEY,
    signal_id       VARCHAR,
    code            VARCHAR NOT NULL,
    name            VARCHAR,
    strategy        VARCHAR,
    side            VARCHAR NOT NULL,
    quantity        INTEGER NOT NULL,
    entry_date      DATE,
    entry_price     DOUBLE,
    stop_price      DOUBLE,
    target_price    DOUBLE,
    exit_date       DATE,
    exit_price      DOUBLE,
    exit_reason     VARCHAR,                   -- stop/target/timeout/manual
    pnl_jpy         DOUBLE,
    is_paper        BOOLEAN NOT NULL DEFAULT TRUE,
    status          VARCHAR NOT NULL,          -- open/closed
    created_at      TIMESTAMP DEFAULT current_timestamp
);

-- バッチ実行ログ
CREATE TABLE IF NOT EXISTS runs (
    id              VARCHAR PRIMARY KEY,
    kind            VARCHAR NOT NULL,          -- fetch/screen/notify/execute/mark
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    ok              BOOLEAN,
    detail          VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_quotes_date ON daily_quotes(date);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        conn = duckdb.connect(str(self.path))
        try:
            yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(SCHEMA_SQL)
        log.info("スキーマを初期化しました: %s", self.path)

    # ------------------------------------------------------------------ 書き込み

    def upsert_listed(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        with self.connect() as conn:
            conn.register("incoming", df)
            conn.execute("DELETE FROM listed WHERE code IN (SELECT code FROM incoming)")
            conn.execute(
                """
                INSERT INTO listed (code, name, market_code, sector17_code,
                                    sector33_code, scale_category)
                SELECT code, name, market_code, sector17_code,
                       sector33_code, scale_category
                FROM incoming
                """
            )
            conn.unregister("incoming")
        return len(df)

    def upsert_quotes(self, df: pd.DataFrame) -> int:
        """日足を冪等に投入する。同じ (code, date) は上書き。"""
        if df.empty:
            return 0
        with self.connect() as conn:
            conn.register("incoming", df)
            conn.execute(
                """
                DELETE FROM daily_quotes
                WHERE (code, date) IN (SELECT code, date FROM incoming)
                """
            )
            conn.execute(
                """
                INSERT INTO daily_quotes
                    (code, date, open, high, low, close, volume, turnover_value)
                SELECT code, date, open, high, low, close, volume, turnover_value
                FROM incoming
                """
            )
            conn.unregister("incoming")
        return len(df)

    def execute(self, sql: str, params: list | tuple | None = None) -> None:
        with self.connect() as conn:
            conn.execute(sql, params or [])

    # ------------------------------------------------------------------ 読み出し

    def query(self, sql: str, params: list | tuple | None = None) -> pd.DataFrame:
        with self.connect() as conn:
            return conn.execute(sql, params or []).fetchdf()

    def latest_quote_date(self) -> pd.Timestamp | None:
        df = self.query("SELECT max(date) AS d FROM daily_quotes")
        if df.empty or pd.isna(df.iloc[0]["d"]):
            return None
        return pd.Timestamp(df.iloc[0]["d"])

    def universe(
        self,
        market_codes: list[str],
        min_history_days: int,
    ) -> pd.DataFrame:
        """スクリーニング対象の銘柄一覧（十分な履歴があるものだけ）。"""
        placeholders = ",".join("?" for _ in market_codes) or "''"
        sql = f"""
            SELECT l.code, l.name, l.market_code, l.sector33_code, count(q.date) AS bars
            FROM listed l
            JOIN daily_quotes q ON q.code = l.code
            WHERE l.market_code IN ({placeholders})
            GROUP BY 1, 2, 3, 4
            HAVING count(q.date) >= ?
            ORDER BY l.code
        """
        return self.query(sql, [*market_codes, min_history_days])

    def bars(self, code: str, limit: int = 400) -> pd.DataFrame:
        """1 銘柄の日足を古い順で返す。"""
        df = self.query(
            """
            SELECT date, open, high, low, close, volume, turnover_value
            FROM daily_quotes
            WHERE code = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            [code, limit],
        )
        return df.iloc[::-1].reset_index(drop=True)

    def bars_bulk(self, codes: list[str], limit_days: int = 400) -> dict[str, pd.DataFrame]:
        """複数銘柄の日足をまとめて取得する。

        1 銘柄ずつクエリすると 4,000 回の往復が発生して遅いので、
        直近 N 営業日ぶんを 1 回で取り出して Python 側で分割する。
        """
        if not codes:
            return {}
        cutoff = self.query(
            "SELECT date FROM (SELECT DISTINCT date FROM daily_quotes ORDER BY date DESC LIMIT ?) "
            "ORDER BY date LIMIT 1",
            [limit_days],
        )
        if cutoff.empty:
            return {}
        start = cutoff.iloc[0]["date"]
        placeholders = ",".join("?" for _ in codes)
        df = self.query(
            f"""
            SELECT code, date, open, high, low, close, volume, turnover_value
            FROM daily_quotes
            WHERE date >= ? AND code IN ({placeholders})
            ORDER BY code, date
            """,
            [start, *codes],
        )
        return {code: g.reset_index(drop=True) for code, g in df.groupby("code", sort=False)}

    def open_position_codes(self) -> set[str]:
        df = self.query("SELECT DISTINCT code FROM positions WHERE status = 'open'")
        return set(df["code"].tolist()) if not df.empty else set()

    def count_open_positions(self) -> int:
        df = self.query("SELECT count(*) AS n FROM positions WHERE status = 'open'")
        return int(df.iloc[0]["n"])
