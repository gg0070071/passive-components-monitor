"""构建看板数据出口。

从 SQLite 查询看板所需的全部数据，输出一份 JSON，供 dashboard/template.html 内嵌。
数据内嵌而非前端请求，网页即可离线打开、也能直接部署到 GitHub Pages。

用法：
    python dashboard/build_dashboard.py
    → 写出 docs/index.html
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DB_PATH = Path(os.environ.get("PC_DB_PATH", PROJECT_ROOT / "data" / "db" / "passive_components.db"))
TEMPLATE_PATH = PROJECT_ROOT / "dashboard" / "template.html"
OUT_PATH = PROJECT_ROOT / "docs" / "index.html"


def _round(v, n=2):
    return None if v is None or pd.isna(v) else round(float(v), n)


def build_payload(conn: sqlite3.Connection) -> dict:
    dim = pd.read_sql("SELECT code, name, segment FROM dim_stock", conn)
    name_of = dict(zip(dim["code"], dim["name"]))
    seg_of = dict(zip(dim["code"], dim["segment"]))

    meta_row = pd.read_sql(
        "SELECT MIN(trade_date) AS s, MAX(trade_date) AS e FROM fact_quote", conn
    ).iloc[0]
    fin_date = pd.read_sql(
        "SELECT MAX(report_date) AS d FROM fact_financial", conn
    ).iloc[0]["d"]

    # ---- 个股区间收益 ----
    ranked = pd.read_sql(
        """
        SELECT code, trade_date, close,
               ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date)      AS rn_f,
               ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) AS rn_l
        FROM fact_quote
        """,
        conn,
    )
    stocks = []
    for code, g in ranked.groupby("code"):
        cs = float(g.loc[g["rn_f"] == 1, "close"].iloc[0])
        ce = float(g.loc[g["rn_l"] == 1, "close"].iloc[0])
        stocks.append({
            "code": code, "name": name_of.get(code, code),
            "segment": seg_of.get(code, ""),
            "close_start": _round(cs), "close_end": _round(ce),
            "ret_pct": _round((ce / cs - 1) * 100),
        })
    stocks.sort(key=lambda x: x["ret_pct"], reverse=True)

    segments = []
    for seg_name, g in pd.DataFrame(stocks).groupby("segment"):
        segments.append({
            "segment": seg_name,
            "n_stocks": int(len(g)),
            "avg_ret_pct": _round(g["ret_pct"].mean()),
        })
    segments.sort(key=lambda x: x["avg_ret_pct"], reverse=True)

    # ---- 归一化价格曲线（起点=100，便于跨股比较）----
    px = pd.read_sql(
        "SELECT code, trade_date, close FROM fact_quote ORDER BY trade_date", conn
    )
    dates = sorted(px["trade_date"].unique())
    date_idx = {d: i for i, d in enumerate(dates)}
    series = []
    for code, g in px.groupby("code"):
        g = g.sort_values("trade_date")
        base = float(g["close"].iloc[0])
        vals = [None] * len(dates)
        for d, c in zip(g["trade_date"], g["close"]):
            vals[date_idx[d]] = round(float(c) / base * 100, 1)
        series.append({
            "code": code,
            "name": name_of.get(code, code),
            "segment": seg_of.get(code, ""),
            "values": vals,
        })
    # 按环节（分层带顺序）+ 区间收益降序排列，让图例与图表层次一致
    seg_rank = {s["segment"]: i for i, s in enumerate(segments)}
    stock_rank = {s["code"]: i for i, s in enumerate(stocks)}
    series.sort(key=lambda x: (seg_rank.get(x["segment"], 99), stock_rank.get(x["code"], 99)))
    for s in series:
        s.pop("code")

    # ---- 财务同比 ----
    fin = pd.read_sql(
        """
        WITH f AS (
            SELECT code, report_date, metric, value,
                   LAG(value, 4) OVER (PARTITION BY code, metric ORDER BY report_date) AS prev
            FROM fact_financial
            WHERE category = '常用指标' AND metric IN ('营业总收入', '归母净利润')
        )
        SELECT * FROM f WHERE report_date = (SELECT MAX(report_date) FROM fact_financial)
        """,
        conn,
    )
    fin_map = {}
    for _, r in fin.iterrows():
        key = r["code"]
        entry = fin_map.setdefault(key, {"name": name_of.get(key, key),
                                         "segment": seg_of.get(key, "")})
        base_positive = pd.notna(r["prev"]) and r["prev"] > 0
        field = "revenue" if r["metric"] == "营业总收入" else "profit"
        entry[f"{field}_yi"] = _round(r["value"] / 1e8)
        entry[f"{field}_yoy"] = _round((r["value"] / r["prev"] - 1) * 100) if base_positive else None
        if not base_positive and pd.notna(r["prev"]):
            entry[f"{field}_note"] = "去年同期基数为负，同比不适用"
    financial = sorted(
        fin_map.values(),
        key=lambda x: (seg_rank.get(x["segment"], 99), -(x.get("revenue_yi") or 0)),
    )

    # ---- 盈利能力 ----
    prof = pd.read_sql(
        """
        SELECT s.name, s.segment, f.metric, f.value
        FROM fact_financial f JOIN dim_stock s ON s.code = f.code
        WHERE f.report_date = (SELECT MAX(report_date) FROM fact_financial)
          AND f.metric IN ('毛利率', '销售净利率', '净资产收益率(ROE)')
        """,
        conn,
    )
    prof_map = {}
    for _, r in prof.iterrows():
        e = prof_map.setdefault(r["name"], {"name": r["name"], "segment": r["segment"]})
        key = {"毛利率": "gross", "销售净利率": "net", "净资产收益率(ROE)": "roe"}[r["metric"]]
        e[key] = _round(r["value"])
    profitability = sorted(prof_map.values(), key=lambda x: x.get("gross") or 0, reverse=True)

    # ---- 异动 ----
    ano = pd.read_sql(
        "SELECT code, trade_date, ret, zscore, attribution FROM fact_anomaly", conn
    )
    by_attr = ({k: int(v) for k, v in ano["attribution"].value_counts().items()}
               if len(ano) else {})
    stock_only = ano[ano["attribution"] == "stock"].copy()
    if len(stock_only):
        stock_only = stock_only.reindex(
            stock_only["zscore"].abs().sort_values(ascending=False).index)
    anomalies = {
        "total": int(len(ano)),
        "by_attribution": by_attr,
        "stock_specific": [
            {"name": name_of.get(r["code"], r["code"]), "trade_date": r["trade_date"],
             "ret_pct": _round(r["ret"] * 100), "zscore": _round(r["zscore"])}
            for _, r in stock_only.iterrows()
        ],
    }

    # ---- 数据质量 ----
    suspend = pd.read_sql(
        """
        WITH cal AS (SELECT DISTINCT trade_date FROM fact_quote),
        grid AS (SELECT s.code, s.name, c.trade_date FROM dim_stock s CROSS JOIN cal c)
        SELECT g.name, COUNT(*) AS days, MIN(g.trade_date) AS d0, MAX(g.trade_date) AS d1
        FROM grid g LEFT JOIN fact_quote q
          ON g.code = q.code AND g.trade_date = q.trade_date
        WHERE q.trade_date IS NULL GROUP BY g.code, g.name
        """,
        conn,
    )
    quality = [{"name": r["name"], "days": int(r["days"]), "period": f"{r['d0']} ~ {r['d1']}"}
               for _, r in suspend.iterrows()]

    return {
        "meta": {
            "data_start": str(meta_row["s"]), "data_end": str(meta_row["e"]),
            "financial_report_date": str(fin_date),
            "n_stocks": int(len(dim)),
            "n_segments": int(dim["segment"].nunique()),
        },
        "stocks": stocks,
        "segments": segments,
        "price": {"dates": dates, "series": series},
        "financial": financial,
        "profitability": profitability,
        "anomalies": anomalies,
        "quality": quality,
    }


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        payload = build_payload(conn)
    finally:
        conn.close()

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = template.replace("/*__DATA__*/", data_json)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")

    print(f"看板已生成 → {OUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"  标的 {payload['meta']['n_stocks']} 只 / 环节 {payload['meta']['n_segments']} 个")
    print(f"  价格序列 {len(payload['price']['series'])} 条 × {len(payload['price']['dates'])} 个交易日")
    print(f"  异动 {payload['anomalies']['total']} 条（个股性 {len(payload['anomalies']['stock_specific'])} 条）")
    print(f"  文件大小 {OUT_PATH.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
