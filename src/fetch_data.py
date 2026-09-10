"""采集被动元件产业链标的池的公开数据。

数据来源：akshare（免费公开接口，无需 token）
  - 日线行情：多源自动降级（东财 → 新浪 → 腾讯）
  - 财务摘要：stock_financial_abstract

多源降级的原因：单一免费接口可能限流、变动或被网络环境拦截。
行情脚本会按顺序尝试多个数据源，任何一个成功即采用，并把实际使用的
来源记录下来——这是数据管道的基本健壮性要求。

输出（data/raw/）：
  {代码}_{简称}_quotes.csv       统一列名后的日线行情
  {代码}_{简称}_financials.csv   财务摘要（宽表，保留原始结构）
  _fetch_log.csv                本次采集的来源与行数记录

统一后的行情列：
  date, open, high, low, close, volume(股), amount(元), turnover(%)

用法：
    cd passive-components-monitor
    python src/fetch_data.py            # 已存在的文件跳过
    python src/fetch_data.py --force    # 强制重抓
"""

import sys
import time
from datetime import date
from pathlib import Path

import akshare as ak
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tickers import TICKERS, QUOTE_START_DATE  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"

MAX_RETRY = 3
RETRY_WAIT = 4  # 秒
SLEEP_BETWEEN = 1  # 每次请求之间的温和限速

# 统一后的行情列
QUOTE_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover"]


def fetch_with_retry(func, *args, **kwargs):
    """带重试地调用 akshare 接口，避免偶发网络抖动导致整批失败。"""
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < MAX_RETRY:
                time.sleep(RETRY_WAIT)
    raise RuntimeError(f"重试 {MAX_RETRY} 次仍失败") from last_err


def _prefixed(code: str) -> str:
    """给裸代码加交易所前缀：6 开头为沪市，0/3 开头为深市。"""
    return ("sh" if code.startswith("6") else "sz") + code


# --------------------------------------------------------------------------
# 各数据源：统一输出 QUOTE_COLUMNS
# --------------------------------------------------------------------------

def _quotes_eastmoney(code: str, start: str, end: str) -> pd.DataFrame:
    """东方财富。volume 单位是手，需 ×100 换成股。"""
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
    )
    df = df.rename(
        columns={
            "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
            "收盘": "close", "成交量": "volume", "成交额": "amount", "换手率": "turnover",
        }
    )
    df["volume"] = df["volume"] * 100  # 手 → 股
    return df[QUOTE_COLUMNS]


def _quotes_sina(code: str, start: str, end: str) -> pd.DataFrame:
    """新浪。turnover 返回比例，×100 换成百分比。"""
    df = ak.stock_zh_a_daily(
        symbol=_prefixed(code), start_date=start, end_date=end, adjust="qfq"
    )
    df = df.rename(columns={"date": "date", "turnover": "turnover"})
    df["turnover"] = df["turnover"] * 100  # 比例 → 百分比
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[QUOTE_COLUMNS]


def _quotes_tencent(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯。volume 已是股；turnover 为比例，×100。"""
    df = ak.stock_zh_a_hist_tx(
        symbol=_prefixed(code), start_date=start, end_date=end, adjust="qfq"
    )
    df["turnover"] = df["turnover"] * 100
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df[QUOTE_COLUMNS]


QUOTE_SOURCES = [
    ("东财", _quotes_eastmoney),
    ("新浪", _quotes_sina),
    ("腾讯", _quotes_tencent),
]


def fetch_quotes(code: str, start: str, end: str):
    """按顺序尝试各数据源，返回 (数据, 来源名)。全部失败则抛异常。"""
    errors = []
    for source_name, fn in QUOTE_SOURCES:
        try:
            df = fetch_with_retry(fn, code, start, end)
            if df is not None and len(df) > 0:
                return df, source_name
            errors.append(f"{source_name}: 返回空数据")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{source_name}: {type(e).__name__}")
    raise RuntimeError("所有行情源均失败 → " + " | ".join(errors))


def fetch_financials(code: str) -> pd.DataFrame:
    return fetch_with_retry(ak.stock_financial_abstract, symbol=code)


# --------------------------------------------------------------------------

def main() -> None:
    force = "--force" in sys.argv
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    end = date.today().strftime("%Y%m%d")
    print(f"采集窗口：{QUOTE_START_DATE} ~ {end}，共 {len(TICKERS)} 只标的\n")

    log_rows = []
    failed = []

    for code, name, segment in TICKERS:
        print(f"[{code} {name}] {segment}")

        # --- 行情 ---
        quote_path = RAW_DIR / f"{code}_{name}_quotes.csv"
        if quote_path.exists() and not force:
            print(f"  行情：已存在，跳过（{quote_path.name}）")
        else:
            try:
                df, source = fetch_quotes(code, QUOTE_START_DATE, end)
                df.to_csv(quote_path, index=False, encoding="utf-8-sig")
                print(f"  行情：{len(df)} 行，来源={source} → {quote_path.name}")
                log_rows.append(
                    {"code": code, "name": name, "kind": "quotes",
                     "source": source, "rows": len(df), "status": "ok"}
                )
            except Exception as e:  # noqa: BLE001
                print(f"  行情：失败 {e}")
                failed.append((code, name, "行情"))
                log_rows.append(
                    {"code": code, "name": name, "kind": "quotes",
                     "source": "", "rows": 0, "status": f"fail: {e}"}
                )

        # --- 财务 ---
        fin_path = RAW_DIR / f"{code}_{name}_financials.csv"
        if fin_path.exists() and not force:
            print(f"  财务：已存在，跳过（{fin_path.name}）")
        else:
            try:
                df = fetch_financials(code)
                df.to_csv(fin_path, index=False, encoding="utf-8-sig")
                print(f"  财务：{df.shape[0]} 行 × {df.shape[1]} 列 → {fin_path.name}")
                log_rows.append(
                    {"code": code, "name": name, "kind": "financials",
                     "source": "新浪", "rows": df.shape[0], "status": "ok"}
                )
            except Exception as e:  # noqa: BLE001
                print(f"  财务：失败 {type(e).__name__}")
                failed.append((code, name, "财务"))
                log_rows.append(
                    {"code": code, "name": name, "kind": "financials",
                     "source": "", "rows": 0, "status": f"fail: {type(e).__name__}"}
                )

        time.sleep(SLEEP_BETWEEN)

    pd.DataFrame(log_rows).to_csv(
        RAW_DIR / "_fetch_log.csv", index=False, encoding="utf-8-sig"
    )

    print("\n" + "=" * 50)
    print(f"完成：成功 {len(log_rows) - len(failed)} 项，失败 {len(failed)} 项")
    print("采集日志：data/raw/_fetch_log.csv")
    if failed:
        print("失败清单：")
        for code, name, kind in failed:
            print(f"  - {code} {name} {kind}")


if __name__ == "__main__":
    main()
