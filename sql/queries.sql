-- ============================================================================
-- 被动元件产业链投研监控系统 · 分析查询集
-- ============================================================================
-- 说明：
--   - 全部使用标准 SQL（窗口函数、CTE、JOIN），可在 SQLite / MySQL / Hive 通用
--   - 行情为前复权价；财务为报告期累计值
--   - 「同比」= 与去年同期比（LAG 4 个季度），适合处理累计口径
--
-- 用法：
--   sqlite3 data/db/passive_components.db < sql/queries.sql
--   或单独执行某条查询
-- ============================================================================


-- ---------------------------------------------------------------------------
-- Q1. 标的池概览：每只标的的交易日覆盖情况
-- ---------------------------------------------------------------------------
SELECT s.code,
       s.name,
       s.segment,
       s.exchange,
       COUNT(q.trade_date) AS trade_days,
       MIN(q.trade_date)   AS first_day,
       MAX(q.trade_date)   AS last_day
FROM dim_stock s
LEFT JOIN fact_quote q ON s.code = q.code
GROUP BY s.code, s.name, s.segment, s.exchange
ORDER BY s.segment, s.code;


-- ---------------------------------------------------------------------------
-- Q2. 停牌识别：相对全市场交易日历，找出个股缺失的交易日
--     （缺失交易日 = 停牌，不能当作缺失值插补）
-- ---------------------------------------------------------------------------
WITH calendar AS (
    SELECT DISTINCT trade_date FROM fact_quote
),
grid AS (
    SELECT s.code, s.name, c.trade_date
    FROM dim_stock s CROSS JOIN calendar c
),
missing AS (
    SELECT g.code, g.name, g.trade_date
    FROM grid g
    LEFT JOIN fact_quote q
           ON g.code = q.code AND g.trade_date = q.trade_date
    WHERE q.trade_date IS NULL
)
SELECT name,
       COUNT(*)            AS suspend_days,
       MIN(trade_date)     AS first_suspend,
       MAX(trade_date)     AS last_suspend
FROM missing
GROUP BY code, name
ORDER BY suspend_days DESC;


-- ---------------------------------------------------------------------------
-- Q3. 区间涨跌幅：以首末交易日前复权收盘价计算
-- ---------------------------------------------------------------------------
WITH ranked AS (
    SELECT code,
           trade_date,
           close,
           ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date)      AS rn_first,
           ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) AS rn_last
    FROM fact_quote
)
SELECT s.name,
       s.segment,
       MAX(CASE WHEN rn_first = 1 THEN trade_date END) AS start_day,
       MAX(CASE WHEN rn_last  = 1 THEN trade_date END) AS end_day,
       ROUND(MAX(CASE WHEN rn_first = 1 THEN close END), 2) AS close_start,
       ROUND(MAX(CASE WHEN rn_last  = 1 THEN close END), 2) AS close_end,
       ROUND(100.0 * (
             MAX(CASE WHEN rn_last = 1 THEN close END)
           / MAX(CASE WHEN rn_first = 1 THEN close END) - 1), 2) AS ret_pct
FROM ranked r
JOIN dim_stock s ON s.code = r.code
GROUP BY s.code, s.name, s.segment
ORDER BY ret_pct DESC;


-- ---------------------------------------------------------------------------
-- Q4. 最新交易日：收盘价与 20 日均线的偏离度
--     （偏离过大 = 短期超买/超卖信号）
-- ---------------------------------------------------------------------------
WITH ma AS (
    SELECT code,
           trade_date,
           close,
           AVG(close) OVER (
               PARTITION BY code ORDER BY trade_date
               ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
           ) AS ma20
    FROM fact_quote
)
SELECT s.name,
       m.trade_date,
       ROUND(m.close, 2)                          AS close,
       ROUND(m.ma20, 2)                           AS ma20,
       ROUND(100.0 * (m.close / m.ma20 - 1), 2)   AS dev_from_ma20_pct
FROM ma m
JOIN dim_stock s ON s.code = m.code
WHERE m.trade_date = (SELECT MAX(trade_date) FROM fact_quote)
  AND m.ma20 IS NOT NULL
ORDER BY dev_from_ma20_pct DESC;


-- ---------------------------------------------------------------------------
-- Q5. 异动检测：单日涨跌幅绝对值 ≥ 5% 的交易日
-- ---------------------------------------------------------------------------
WITH ret AS (
    SELECT code,
           trade_date,
           close,
           LAG(close) OVER (PARTITION BY code ORDER BY trade_date) AS prev_close,
           close / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY trade_date), 0) - 1 AS r
    FROM fact_quote
)
SELECT s.name,
       r.trade_date,
       ROUND(100 * r.r, 2) AS chg_pct
FROM ret r
JOIN dim_stock s ON s.code = r.code
WHERE ABS(r.r) >= 0.05
ORDER BY ABS(r.r) DESC
LIMIT 20;


-- ---------------------------------------------------------------------------
-- Q6. 近 90 个交易日波动率（日收益率标准差，手动计算，兼容无 STDDEV 的库）
-- ---------------------------------------------------------------------------
WITH ret AS (
    SELECT code,
           trade_date,
           close / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY trade_date), 0) - 1 AS r
    FROM fact_quote
),
recent AS (
    SELECT code, r
    FROM ret
    WHERE trade_date >= date((SELECT MAX(trade_date) FROM fact_quote), '-90 day')
      AND r IS NOT NULL
)
SELECT s.name,
       COUNT(*)                                          AS samples,
       ROUND(100 * SQRT(AVG(r * r) - AVG(r) * AVG(r)), 2) AS daily_vol_pct
FROM recent rc
JOIN dim_stock s ON s.code = rc.code
GROUP BY s.code, s.name
ORDER BY daily_vol_pct DESC;


-- ---------------------------------------------------------------------------
-- Q7. 财务同比：最新报告期的营业总收入与归母净利润同比增速
--     （报告期为累计值，同比即与去年同一报告期比较）
--
--     口径注意：当去年同期为负（亏损）时，同比百分比在数学上没有意义
--     （如 -242%），此时只给绝对变动额，不给百分比。这是投研里的常见坑。
-- ---------------------------------------------------------------------------
WITH fin AS (
    SELECT code,
           report_date,
           metric,
           value,
           LAG(value, 4) OVER (PARTITION BY code, metric ORDER BY report_date) AS prev_yoy
    FROM fact_financial
    WHERE category = '常用指标'
      AND metric IN ('营业总收入', '归母净利润')
)
SELECT s.name,
       f.metric,
       f.report_date,
       ROUND(f.value / 1e8, 2)    AS value_yi,
       ROUND(f.prev_yoy / 1e8, 2) AS prev_yoy_yi,
       CASE
           WHEN f.prev_yoy > 0 THEN ROUND(100 * (f.value / f.prev_yoy - 1), 2)
           ELSE NULL                       -- 基数为负，同比不适用
       END                        AS yoy_pct,
       ROUND((f.value - f.prev_yoy) / 1e8, 2) AS yoy_change_yi
FROM fin f
JOIN dim_stock s ON s.code = f.code
WHERE f.report_date = (SELECT MAX(report_date) FROM fact_financial)
ORDER BY f.metric, yoy_pct DESC;


-- ---------------------------------------------------------------------------
-- Q8. 同业盈利能力对比：最新报告期的毛利率、销售净利率、ROE
-- ---------------------------------------------------------------------------
SELECT s.name,
       s.segment,
       ROUND(MAX(CASE WHEN f.metric = '毛利率'        THEN f.value END), 2) AS gross_margin_pct,
       ROUND(MAX(CASE WHEN f.metric = '销售净利率'    THEN f.value END), 2) AS net_margin_pct,
       ROUND(MAX(CASE WHEN f.metric = '净资产收益率(ROE)' THEN f.value END), 2) AS roe_pct
FROM fact_financial f
JOIN dim_stock s ON s.code = f.code
WHERE f.report_date = (SELECT MAX(report_date) FROM fact_financial)
  AND f.metric IN ('毛利率', '销售净利率', '净资产收益率(ROE)')
GROUP BY s.code, s.name, s.segment
ORDER BY gross_margin_pct DESC;


-- ---------------------------------------------------------------------------
-- Q9. 产业链环节聚合：按细分环节汇总区间涨跌幅均值
-- ---------------------------------------------------------------------------
WITH ret AS (
    SELECT code,
           trade_date,
           close,
           FIRST_VALUE(close) OVER (PARTITION BY code ORDER BY trade_date) AS first_close
    FROM fact_quote
)
SELECT s.segment,
       COUNT(DISTINCT s.code)                      AS n_stocks,
       ROUND(100 * AVG(r.close / r.first_close - 1), 2) AS avg_ret_pct
FROM ret r
JOIN dim_stock s ON s.code = r.code
WHERE r.trade_date = (SELECT MAX(trade_date) FROM fact_quote)
GROUP BY s.segment
ORDER BY avg_ret_pct DESC;
