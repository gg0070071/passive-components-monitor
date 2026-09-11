"""输出质量门禁：校验 LLM 生成的月报里每个数字都能回溯到事实数据。

这是本项目区别于"让 AI 随便写一段"的关键：LLM 会自信地编造数字，
所以在成稿之后必须有一道机器校验——把月报里的每个数字抽出来，
逐个比对事实数据，任何找不到来源的数字都会被标出来。

方法：
  1. 递归遍历事实数据，收集所有"允许出现的数字"
  2. 从月报正文抽取出所有数字
  3. 逐个匹配（允许四舍五入的误差）
  4. 输出通过 / 无法溯源清单

注意：这是"回溯校验"，不是"事实校验"。它只能保证月报没编数字，
   不能保证数字本身的业务含义正确——那仍然需要人来判断。

用法：
    from quality_gate import check
    check(facts, report_text)
或命令行：
    python ai/quality_gate.py reports/月报_2026-09-10.md
"""

import json
import re
import sys
from pathlib import Path

# 允许的相对误差（LLM 可能做四舍五入）与最小绝对误差
REL_TOL = 0.005
ABS_TOL = 0.05


def _collect_numbers(obj, acc: set) -> None:
    """递归收集事实数据里的所有数字。"""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        acc.add(float(obj))
    elif isinstance(obj, str):
        # 日期字符串里的年份、月、日也视为允许出现
        for m in re.findall(r"\d+", obj):
            acc.add(float(m))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, acc)


def build_allowed(facts: dict) -> set:
    acc: set = set()
    _collect_numbers(facts, acc)
    # 叠加常见的四舍五入结果，避免把"正常取整"误判为编造
    extra = set()
    for v in acc:
        extra.add(round(v))
        extra.add(round(v, 1))
        extra.add(round(v, 2))
    acc |= extra
    # 通用安全值：0 与常见序数
    acc |= {0.0, 1.0, 2.0, 3.0, 4.0, 5.0}
    return acc


def extract_numbers(text: str) -> list:
    """从文本中抽取数字（含负号与小数），返回 (数值, 原文片段)。"""
    # 去掉日期格式，避免把 2026-09-10 拆成三个数造成噪声
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    out = []
    for m in re.finditer(r"[-+]?\d+(?:\.\d+)?", cleaned):
        raw = m.group(0)
        try:
            out.append((float(raw), raw))
        except ValueError:
            continue
    return out


def is_traceable(value: float, allowed: set) -> bool:
    for a in allowed:
        tol = max(ABS_TOL, REL_TOL * abs(a))
        if abs(a - value) <= tol:
            return True
    return False


def check(facts: dict, report_text: str, verbose: bool = True) -> bool:
    allowed = build_allowed(facts)
    found = extract_numbers(report_text)

    untraceable = []
    for value, raw in found:
        if not is_traceable(value, allowed):
            untraceable.append(raw)

    if verbose:
        print(f"  抽出数字 {len(found)} 个，允许集合 {len(allowed)} 个值")
        if untraceable:
            print(f"  ✗ 无法溯源 {len(untraceable)} 个：{untraceable}")
            print("    这些数字在事实数据里找不到来源，可能是 LLM 编造的，需人工确认。")
        else:
            print("  ✓ 所有数字均可在事实数据中找到来源")

    return len(untraceable) == 0


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("用法：python ai/quality_gate.py <月报文件> [事实JSON文件]")

    report_path = Path(sys.argv[1])
    facts_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if facts_path is None:
        raise SystemExit("请一并提供事实 JSON：python ai/report_input.py --out facts.json")

    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    text = report_path.read_text(encoding="utf-8")
    print(f"校验 {report_path.name}：")
    ok = check(facts, text)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
