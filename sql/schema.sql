-- ============================================================================
-- 被动元件产业链投研监控系统 · 表结构
-- ============================================================================
-- 数据库：SQLite
-- 设计：星型模型（维度表 + 事实表）
--   dim_stock       维度表：标的池（每只股票一行）
--   fact_quote      事实表：日频行情（每只股票每个交易日一行）
--   fact_financial  事实表：季频财务（长表，每个指标每期一行）
--
-- 约定：
--   - 只用标准 SQL，不使用 MySQL/SQLite 专有语法，便于日后迁移到 MySQL/Hive
--   - 日期统一用字符串 'YYYY-MM-DD'
--   - 金额单位：元；成交量单位：股；换手率单位：百分比
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 维度表：标的池
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dim_stock (
    code        TEXT PRIMARY KEY,        -- 股票代码，如 '300408'
    name        TEXT NOT NULL,           -- 简称，如 '三环集团'
    segment     TEXT NOT NULL,           -- 产业链环节，如 'MLCC/陶瓷'
    exchange    TEXT NOT NULL            -- 交易所：'SH' 沪市 / 'SZ' 深市
);

-- ---------------------------------------------------------------------------
-- 事实表：日频行情
-- ---------------------------------------------------------------------------
-- 只存实际有交易的交易日。缺失的交易日 = 停牌（如洁美科技 2026-03-03~03-16），
-- 分析时不可当作缺失值插补，需单独识别。
CREATE TABLE IF NOT EXISTS fact_quote (
    code        TEXT NOT NULL,           -- 股票代码
    trade_date  TEXT NOT NULL,           -- 交易日 'YYYY-MM-DD'
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,                    -- 前复权收盘价
    volume      REAL,                    -- 成交量（股）
    amount      REAL,                    -- 成交额（元）
    turnover    REAL,                    -- 换手率（%）
    PRIMARY KEY (code, trade_date),
    FOREIGN KEY (code) REFERENCES dim_stock(code)
);

-- 按日期检索全部标的（如"某天全行业涨跌"）时用得到
CREATE INDEX IF NOT EXISTS idx_quote_date ON fact_quote (trade_date);

-- ---------------------------------------------------------------------------
-- 事实表：季频财务
-- ---------------------------------------------------------------------------
-- 源数据是宽表（指标 × 报告期），这里转成"长表"——每个"指标 × 报告期"一行，
-- 便于用 SQL 做同比/环比和跨公司对比。
--
-- category（原文'选项'）用于区分同名指标所属的分组，避免主键冲突。
CREATE TABLE IF NOT EXISTS fact_financial (
    code        TEXT NOT NULL,           -- 股票代码
    report_date TEXT NOT NULL,           -- 报告期 'YYYY-MM-DD'（如 2026-06-30）
    category    TEXT NOT NULL,           -- 指标分组，源数据的'选项'列
    metric      TEXT NOT NULL,           -- 指标名，如 '归母净利润'
    value       REAL,                    -- 指标值（原始单位）
    PRIMARY KEY (code, report_date, category, metric),
    FOREIGN KEY (code) REFERENCES dim_stock(code)
);

CREATE INDEX IF NOT EXISTS idx_fin_metric ON fact_financial (metric, report_date);

-- ---------------------------------------------------------------------------
-- 事实表：异动记录（派生表，由 src/anomaly.py 生成）
-- ---------------------------------------------------------------------------
-- 记录每一条检测到的个股日收益异动，以及归因结果：
--   market  —— 当日全池普涨/普跌，属市场性
--   segment —— 当日同环节整体同向变动，属板块性
--   stock   —— 市场与板块都解释不了，属个股性（更值得深挖）
CREATE TABLE IF NOT EXISTS fact_anomaly (
    code            TEXT NOT NULL,       -- 股票代码
    trade_date      TEXT NOT NULL,       -- 异动交易日
    ret             REAL,                -- 当日涨跌幅（小数，0.05 = 5%）
    zscore          REAL,                -- 相对自身近 60 日的 z 分数
    market_ret      REAL,                -- 当日全池平均涨跌幅
    segment_ret     REAL,                -- 当日同环节平均涨跌幅（不含自身）
    attribution     TEXT,                -- market / segment / stock
    PRIMARY KEY (code, trade_date),
    FOREIGN KEY (code) REFERENCES dim_stock(code)
);
