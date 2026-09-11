"""行业月报生成流水线。

流程：
  1. ai/report_input.py 汇总事实（全部来自数据库）
  2. 套用 ai/prompts/monthly_report.md 提示词模板
  3. 调用 DeepSeek 生成月报
  4. 落盘到 reports/
  5. 交给 ai/quality_gate.py 校验（数字是否都能回溯）

设计原则：LLM 只负责组织语言，不负责计算。所有数字都来自第 1 步。

API key 从环境变量 DEEPSEEK_API_KEY 读取，不写入代码、不提交仓库。

用法：
    export DEEPSEEK_API_KEY=sk-xxxx
    python ai/report_pipeline.py                # 调 API 生成
    python ai/report_pipeline.py --dry-run      # 不调 API，只渲染提示词，用于离线调试
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "ai"))

DB_PATH = Path(os.environ.get("PC_DB_PATH", PROJECT_ROOT / "data" / "db" / "passive_components.db"))
PROMPT_PATH = PROJECT_ROOT / "ai" / "prompts" / "monthly_report.md"
REPORTS_DIR = PROJECT_ROOT / "reports"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"


def render_prompt(facts: dict) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    # 只把事实层喂给模型，不含任何指令外的内容
    facts_json = json.dumps(facts, ensure_ascii=False, indent=2)
    return template.replace("{facts_json}", facts_json)


def call_deepseek(prompt: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise SystemExit("需要先安装 openai：pip install openai") from e

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit(
            "未设置环境变量 DEEPSEEK_API_KEY。\n"
            "请先执行：export DEEPSEEK_API_KEY=你的key\n"
            "（或先用 --dry-run 离线调试）"
        )

    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": "你是严谨的产业链研究员，只依据给定数据写作。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,   # 降低随机性，研究报告不需要发散
    )
    return resp.choices[0].message.content


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不调 API，只渲染提示词")
    ap.add_argument("--out", help="输出文件路径")
    args = ap.parse_args()

    # 1. 汇总事实
    from report_input import collect
    conn = sqlite3.connect(DB_PATH)
    try:
        facts = collect(conn)
    finally:
        conn.close()

    # 2. 渲染提示词
    prompt = render_prompt(facts)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = facts["meta"]["data_end"]
    out_path = Path(args.out) if args.out else REPORTS_DIR / f"月报_{stamp}.md"
    prompt_path = REPORTS_DIR / f"_提示词_{stamp}.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    if args.dry_run:
        print("【dry-run】未调用 API。")
        print(f"  提示词已渲染 → {prompt_path}")
        print(f"  提示词长度：{len(prompt)} 字符")
        print(f"  事实数据：{len(facts['returns'])} 只标的、"
              f"{len(facts['financial_yoy'])} 条财务同比、"
              f"{facts['anomaly_summary']['total']} 条异动")
        print("\n设置 DEEPSEEK_API_KEY 后去掉 --dry-run 即可生成月报。")
        return

    # 3. 调用 LLM
    print(f"正在调用 DeepSeek（{DEEPSEEK_MODEL}）...")
    body = call_deepseek(prompt)

    # 4. 落盘
    header = f"<!-- 由 AI 生成，事实数据来自 SQLite，生成日期 {date.today()} -->\n\n"
    out_path.write_text(header + body, encoding="utf-8")
    print(f"月报已生成 → {out_path}")

    # 5. 质量门禁
    print("\n正在校验数字可溯源性...")
    from quality_gate import check
    ok = check(facts, body)
    print("质量门禁：通过 ✓" if ok else "质量门禁：存在无法溯源的数字 ✗（详见上方报告）")


if __name__ == "__main__":
    main()
