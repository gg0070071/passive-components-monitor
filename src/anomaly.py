"""异动检测与归因。

方法：
  1. 对每只标的计算日收益率
  2. 用滚动窗口（默认 60 个交易日）算自身收益率的均值与标准差
  3. 计算 z 分数 z = (当日收益 - 滚动均值) / 滚动标准差
  4. |z| 超过阈值即判定为异动
  5. 归因：
       market  —— 当日全池平均涨跌幅已能解释（普涨/普跌）
       segment —— 当日同环节（不含自身）平均涨跌幅已能解释（板块联动）
       stock   —— 市场和板块都解释不了，属个股性异动，更值得深挖

为什么用滚动 z 分数而不是固定涨跌幅阈值：
  固定阈值（如 ±5%）对高波动个股过于敏感、对低波动个股过于迟钝。
  用个股自身的波动水平做标尺，才能横向比较。

用法：
    cd passive-components-monitor
    python src/anomaly.py
"""

import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PC_DB_PATH", PROJECT_ROOT / "data" / "db" / "passive_components.db"))

WINDOW = 60          # 滚动窗口（交易日）
Z_THRESH = 3.0       # z 分数阈值
MARKET_THRESH = 0.01  # 全池平均涨跌幅绝对值 ≥ 1% 视为市场性
SEGMENT_THRESH = 0.02  # 同环节平均涨跌幅绝对值 ≥ 2% 视为板块性


def compute(conn: sqlite3.Connection) -> pd.DataFrame:
    quotes = pd.read_sql(
        "SELECT code, trade_date, close FROM fact_quote ORDER BY code, trade_date", conn
    )
    dim = pd.read_sql("SELECT code, name, segment FROM dim_stock", conn)

    quotes["ret"] = quotes.groupby("code")["close"].pct_change()

    grp = quotes.groupby("code")["ret"]
    quotes["roll_mean"] = grp.transform(
        lambda s: s.rolling(WINDOW, min_periods=WINDOW).mean()
    )
    quotes["roll_std"] = grp.transform(
        lambda s: s.rolling(WINDOW, min_periods=WINDOW).std()
    )
    quotes["zscore"] = (quotes["ret"] - quotes["roll_mean"]) / quotes["roll_std"]

    # 当日全池平均涨跌幅（市场性代理）
    market = quotes.groupby("trade_date")["ret"].mean().rename("market_ret")
    quotes = quotes.merge(market, on="trade_date", how="left")

    # 当日同环节平均涨跌幅，剔除自身
    quotes = quotes.merge(dim[["code", "segment"]], on="code", how="left")
    seg_sum = quotes.groupby(["trade_date", "segment"])["ret"].transform("sum")
    seg_cnt = quotes.groupby(["trade_date", "segment"])["ret"].transform("count")
    peers = seg_cnt - 1
    quotes["segment_ret"] = np.where(peers > 0, (seg_sum - quotes["ret"]) / peers, np.nan)

    return quotes


def classify(row) -> str:
    if pd.notna(row["market_ret"]) and abs(row["market_ret"]) >= MARKET_THRESH:
        return "market"
    if pd.notna(row["segment_ret"]) and abs(row["segment_ret"]) >= SEGMENT_THRESH:
        return "segment"
    return "stock"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        df = compute(conn)

        anomalies = df[df["zscore"].abs() >= Z_THRESH].copy()
        anomalies["attribution"] = anomalies.apply(classify, axis=1)
        anomalies = anomalies.dropna(subset=["ret"])

        out = anomalies[[
            "code", "trade_date", "ret", "zscore",
            "market_ret", "segment_ret", "attribution",
        ]].copy()

        conn.execute("DELETE FROM fact_anomaly")
        conn.executemany(
            "INSERT OR REPLACE INTO fact_anomaly "
            "(code, trade_date, ret, zscore, market_ret, segment_ret, attribution) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            out.itertuples(index=False, name=None),
        )
        conn.commit()

        total = len(df.dropna(subset=["zscore"]))
        print(f"可检测样本：{total} 个交易日观测")
        print(f"检出异动：{len(out)} 条（|z| ≥ {Z_THRESH}，窗口 {WINDOW} 日）\n")

        print("归因分布：")
        for k, v in out["attribution"].value_counts().items():
            label = {"market": "市场性", "segment": "板块性", "stock": "个股性"}[k]
            print(f"  {label:4s} {v:5d} 条")

        print("\n个股性异动 Top 10（市场与板块都解释不了，最值得深挖）：")
        names = dict(
            pd.read_sql("SELECT code, name FROM dim_stock", conn)
            .itertuples(index=False, name=None)
        )
        stock_only = out[out["attribution"] == "stock"].copy()
        stock_only = stock_only.reindex(
            stock_only["zscore"].abs().sort_values(ascending=False).index
        )
        for _, r in stock_only.head(10).iterrows():
            print(f"  {names.get(r['code'], r['code']):6s} {r['trade_date']}  "
                  f"涨跌 {r['ret']*100:+6.2f}%  z={r['zscore']:+.2f}")

        print(f"\n已写入 fact_anomaly 表（{len(out)} 行）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
