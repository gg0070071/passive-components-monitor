"""把 data/raw/ 的 CSV 加载进 SQLite 数据库。

流程：
  1. 建库并执行 sql/schema.sql（维度表 + 两张事实表）
  2. dim_stock   ← src/tickers.py 的标的池
  3. fact_quote  ← 各标的的 *_quotes.csv
  4. fact_financial ← 各标的的 *_financials.csv（宽表转长表）

用法：
    cd passive-components-monitor
    python src/load_to_db.py            # 增量追加
    python src/load_to_db.py --rebuild  # 删库重建
"""

import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tickers import TICKERS  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
# 默认库放在项目内的 data/db；可用环境变量 PC_DB_PATH 覆盖
# （例如把库放到本地磁盘，避免在网络/虚拟挂载盘上触发 SQLite 的文件锁限制）
DB_PATH = Path(os.environ.get("PC_DB_PATH", PROJECT_ROOT / "data" / "db" / "passive_components.db"))
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"


def to_iso(datestr: str) -> str:
    """'20260630' → '2026-06-30'。非 8 位数字原样返回。"""
    s = str(datestr)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def exchange_of(code: str) -> str:
    return "SH" if code.startswith("6") else "SZ"


def rebuild_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def load_dim_stock(conn: sqlite3.Connection) -> int:
    rows = [(code, name, seg, exchange_of(code)) for code, name, seg in TICKERS]
    conn.executemany(
        "INSERT OR REPLACE INTO dim_stock (code, name, segment, exchange) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_fact_quote(conn: sqlite3.Connection) -> int:
    total = 0
    for code, name, _seg in TICKERS:
        path = RAW_DIR / f"{code}_{name}_quotes.csv"
        if not path.exists():
            print(f"  跳过 {code} {name}：缺少 {path.name}")
            continue
        df = pd.read_csv(path)
        df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df["code"] = code
        cols = ["code", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "turnover"]
        records = df[cols].itertuples(index=False, name=None)
        conn.executemany(
            "INSERT OR REPLACE INTO fact_quote "
            "(code, trade_date, open, high, low, close, volume, amount, turnover) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            records,
        )
        total += len(df)
    conn.commit()
    return total


def load_fact_financial(conn: sqlite3.Connection) -> int:
    """宽表转长表：每个'指标 × 报告期'一行。"""
    total = 0
    for code, name, _seg in TICKERS:
        path = RAW_DIR / f"{code}_{name}_financials.csv"
        if not path.exists():
            print(f"  跳过 {code} {name}：缺少 {path.name}")
            continue
        wide = pd.read_csv(path)

        # 报告期列 = 形如 20260630 的列名
        period_cols = [c for c in wide.columns if len(str(c)) == 8 and str(c).isdigit()]
        long = wide.melt(
            id_vars=["选项", "指标"],
            value_vars=period_cols,
            var_name="report_date",
            value_name="value",
        )
        long = long.rename(columns={"选项": "category", "指标": "metric"})
        long["report_date"] = long["report_date"].map(to_iso)
        long["value"] = pd.to_numeric(long["value"], errors="coerce")  # 非数字转 NULL
        long["code"] = code

        records = long[["code", "report_date", "category", "metric", "value"]] \
            .itertuples(index=False, name=None)
        conn.executemany(
            "INSERT OR REPLACE INTO fact_financial "
            "(code, report_date, category, metric, value) VALUES (?, ?, ?, ?, ?)",
            records,
        )
        total += len(long)
    conn.commit()
    return total


def main() -> None:
    rebuild = "--rebuild" in sys.argv
    if rebuild and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"已删除旧库：{DB_PATH.name}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        rebuild_db(conn)
        n_dim = load_dim_stock(conn)
        n_quote = load_fact_quote(conn)
        n_fin = load_fact_financial(conn)

        print(f"\n入库完成 → {DB_PATH}")
        print(f"  dim_stock       {n_dim} 行")
        print(f"  fact_quote      {n_quote} 行")
        print(f"  fact_financial  {n_fin} 行")

        # 对照校验：确认入库行数 = 源文件行数
        cur = conn.execute("SELECT COUNT(*) FROM fact_quote")
        print(f"\n校验 fact_quote 实际行数：{cur.fetchone()[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
