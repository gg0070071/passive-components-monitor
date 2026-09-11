"""汇总生成行业月报所需的全部事实。

这是 LLM 流水线的"事实层"：先把所有要用到的数字从数据库里查出来、结构化，
再交给 LLM 组织成文字。这样做的关键好处是——

  LLM 只负责"表达"，不负责"算数"。所有数字都来自这里，月报里的每个数
  都能回溯到一条 SQL 查询，后面 ai/quality_gate.py 会据此校验。

用法：
    python ai/report_input.py                 # 打印 JSON
    python ai/report_input.py --out data/processed/report_input.json
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PC_DB_PATH", PROJECT_ROOT / "data" / "db" / "passive_components.db"))


def _round(v, n=2):
    return None if pd.isna(v) else round(float(v), n)


def collect(conn: sqlite3.Connection) -> dict:
    """把报告需要的所有事实查出来，返回结构化字典。"""
    dim = pd.read_sql("SELECT code, name, segment FROM dim_stock", conn)
    name_of = dict(zip(dim["code"], dim["name"]))
    seg_of = dict(zip(dim["code"], dim["segment"]))

    last_day = pd.read_sql("SELECT MAX(trade_date) AS d FROM fact_quote", conn)["d"][0]
    first_day = pd.read_sql("SELECT MIN(trade_date) AS d FROM fact_quote", conn)["d"][0]

    # --- 区间涨跌幅（首末收盘价）---
    ranked = pd.read_sql(
        """
        SELECT code, trade_date, close,
               ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date)      AS rn_f,
               ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) AS rn_l
        FROM fact_quote
        """,
        conn,
    )
    agg = ranked.groupby("code").apply(
        lambda g: pd.Series({
            "close_start": g.loc[g["rn_f"] == 1, "close"].iloc[0],
            "close_end": g.loc[g["rn_l"] == 1, "close"].iloc[0],
        }),
        include_groups=False,
    )
    agg["ret_pct"] = (agg["close_end"] / agg["close_start"] - 1) * 100
    returns = [
        {
            "code": code,
            "name": name_of.get(code, code),
            "segment": seg_of.get(code, "未知"),
            "close_start": _round(row["close_start"]),
            "close_end": _round(row["close_end"]),
            "ret_pct": _round(row["ret_pct"]),
        }
        for code, row in agg.sort_values("ret_pct", ascending=False).iterrows()
    ]

    # --- 按环节聚合 ---
    seg = (
        pd.DataFrame(returns).groupby("segment")
        .agg(n_stocks=("code", "count"), avg_ret_pct=("ret_pct", "mean"))
        .reset_index()
    )
    segments = [
        {"segment": r["segment"], "n_stocks": int(r["n_stocks"]),
         "avg_ret_pct": _round(r["avg_ret_pct"])}
        for _, r in seg.sort_values("avg_ret_pct", ascending=False).iterrows()
    ]

    # --- 财务同比（最新报告期，基数为负时不给百分比）---
    fin = pd.read_sql(
        """
        WITH f AS (
            SELECT code, report_date, metric, value,
                   LAG(value, 4) OVER (PARTITION BY code, metric ORDER BY report_date) AS prev
            FROM fact_financial
            WHERE category = '常用指标'
              AND metric IN ('营业总收入', '归母净利润')
        )
        SELECT * FROM f
        WHERE report_date = (SELECT MAX(report_date) FROM fact_financial)
        """,
        conn,
    )
    fin_report_date = fin["report_date"].iloc[0] if len(fin) else None
    yoy = []
    for _, r in fin.iterrows():
        base_positive = pd.notna(r["prev"]) and r["prev"] > 0
        yoy.append({
            "code": r["code"],
            "name": name_of.get(r["code"], r["code"]),
            "metric": r["metric"],
            "value_yi": _round(r["value"] / 1e8),
            "prev_yoy_yi": _round(r["prev"] / 1e8) if pd.notna(r["prev"]) else None,
            "yoy_pct": _round((r["value"] / r["prev"] - 1) * 100) if base_positive else None,
            "yoy_change_yi": _round((r["value"] - r["prev"]) / 1e8) if pd.notna(r["prev"]) else None,
        })

    # --- 盈利能力对比 ---
    prof = pd.read_sql(
        """
        SELECT s.name, s.segment, f.metric, f.value
        FROM fact_financial f JOIN dim_stock s ON s.code = f.code
        WHERE f.report_date = (SELECT MAX(report_date) FROM fact_financial)
          AND f.metric IN ('毛利率', '销售净利率', '净资产收益率(ROE)')
        """,
        conn,
    )
    profitability = []
    for _, r in prof.iterrows():
        profitability.append({
            "name": r["name"], "segment": r["segment"],
            "metric": r["metric"], "value_pct": _round(r["value"]),
        })

    # --- 异动统计 ---
    ano = pd.read_sql(
        "SELECT code, trade_date, ret, zscore, attribution FROM fact_anomaly", conn
    )
    anomaly_summary = {
        "total": int(len(ano)),
        "by_attribution": {
            k: int(v) for k, v in ano["attribution"].value_counts().items()
        } if len(ano) else {},
        "stock_specific": [
            {
                "name": name_of.get(r["code"], r["code"]),
                "trade_date": r["trade_date"],
                "ret_pct": _round(r["ret"] * 100),
                "zscore": _round(r["zscore"]),
            }
            for _, r in ano[ano["attribution"] == "stock"]
            .reindex(ano[ano["attribution"] == "stock"]["zscore"].abs()
                     .sort_values(ascending=False).index)
            .iterrows()
        ] if len(ano) else [],
    }

    # --- 数据质量说明（停牌）---
    suspend = pd.read_sql(
        """
        WITH cal AS (SELECT DISTINCT trade_date FROM fact_quote),
        grid AS (SELECT s.code, s.name, c.trade_date FROM dim_stock s CROSS JOIN cal c)
        SELECT g.name, COUNT(*) AS days, MIN(g.trade_date) AS first_day,
               MAX(g.trade_date) AS last_day
        FROM grid g
        LEFT JOIN fact_quote q ON g.code = q.code AND g.trade_date = q.trade_date
        WHERE q.trade_date IS NULL
        GROUP BY g.code, g.name
        """,
        conn,
    )
    data_notes = [
        {"name": r["name"], "type": "停牌", "days": int(r["days"]),
         "period": f"{r['first_day']} ~ {r['last_day']}"}
        for _, r in suspend.iterrows()
    ]

    # --- 派生指标 ---------------------------------------------------------
    # 这些数由管道算出，而不是让 LLM 自己减/除。原因见 README：
    # LLM 自算的数无法被质量门禁溯源，会被判为"编造"。
    # 凡是月报里想用的比较类数字，都应该在这里先算好。
    rdf = pd.DataFrame(returns)
    segment_spread = []
    for seg_name, g in rdf.groupby("segment"):
        hi = g.loc[g["ret_pct"].idxmax()]
        lo = g.loc[g["ret_pct"].idxmin()]
        segment_spread.append({
            "segment": seg_name,
            "max_name": hi["name"], "max_ret_pct": _round(hi["ret_pct"]),
            "min_name": lo["name"], "min_ret_pct": _round(lo["ret_pct"]),
            "spread_pct": _round(hi["ret_pct"] - lo["ret_pct"]),
        })
    segment_spread.sort(key=lambda x: x["spread_pct"], reverse=True)

    total_ano = anomaly_summary["total"]
    anomaly_share = (
        {k: _round(100 * v / total_ano) for k, v in anomaly_summary["by_attribution"].items()}
        if total_ano else {}
    )

    return {
        "meta": {
            "data_start": first_day,
            "data_end": last_day,
            "financial_report_date": fin_report_date,
            "n_stocks": int(len(dim)),
        },
        "returns": returns,
        "segments": segments,
        "financial_yoy": yoy,
        "profitability": profitability,
        "anomaly_summary": anomaly_summary,
        "data_notes": data_notes,
        "derived": {
            "segment_spread": segment_spread,
            "anomaly_share_pct": anomaly_share,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="输出 JSON 的路径")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    try:
        facts = collect(conn)
    finally:
        conn.close()

    text = json.dumps(facts, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写出：{out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
