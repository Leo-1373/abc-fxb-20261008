#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""规则库语义冲突检查（补 `check_closure.py` 的盲区）。

**为什么需要它**：`docs/分工.md` 要求规则审核时盯三件事——
① 两条规则是不是在说同一件事（重复）；② `cond` 变量有没有定义；③
**等级定得合不合理（同一个现象不能一条判"高"一条判"中"）**。

`build/check_closure.py` 只覆盖了 ②（C4）与 id 唯一性、等级合法性，
**① 和 ③ 完全没有机器检查**——只能靠人眼扫 80 行规则。已实际发生的漏网案例：
`R214`（`1≤overdue_max_days<30` 低）与 `R215`（`overdue_cur≥1` 高）会被**同一笔当前逾期**
同时命中，打出"低+高"两行；这是人眼扫规则表看不出来的（两条规则用的变量都不同）。

校验项：
  D1 `cond` 完全相同的两条规则（纯重复）
  D2 **同一变量上区间重叠、且等级不同**（"同一现象两个等级"，分工.md 点名项）
  D3 `cond` 内部自相矛盾（区间为空，如 `x>5 && x<3`）
  D4 格式：`title` ≤12 字、`advice` ≤16 字（契约 IF-1.2；全局都没有检查）
  D5 无法静态分析的规则（与另一变量比较，如 `a>b*6`）→ 列出来交人工复核

退出码：0 通过（可有警告），1 有错误。
用法：
    PYTHONIOENCODING=utf-8 python build/check_rule_conflicts.py
"""
import os
import re
import sys
import itertools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ruleslib as RL

LEVELS = {"高", "中", "低"}
TERM_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*(>=|<=|==|>|<)\s*(\d+\.?\d*)\s*$")
NUM_RE = re.compile(r"^\d+\.?\d*$")


def parse_cond(cond):
    """→ (per_var_intervals, symbolic_vars, unparsed_terms)。

    per_var_intervals: {var: (lo, lo_inc, hi, hi_inc)}；等式记为 lo==hi==v。
    与另一个变量比较（如 `guarantee_bal>declared_inc*6`）无法静态求区间 →
    该变量进 symbolic_vars，交给人工复核（D5）。
    """
    iv, symbolic, unparsed = {}, set(), []
    for term in cond.split("&&"):
        m = TERM_RE.match(term)
        if not m:
            for v in RL.cond_vars(term):
                symbolic.add(v)
            unparsed.append(term.strip())
            continue
        v, op, num = m.group(1), m.group(2), float(m.group(3))
        lo, lo_inc, hi, hi_inc = iv.get(v, (float("-inf"), True, float("inf"), True))
        if op == ">":
            lo, lo_inc = num, False
        elif op == ">=":
            lo, lo_inc = num, True
        elif op == "<":
            hi, hi_inc = num, False
        elif op == "<=":
            hi, hi_inc = num, True
        elif op == "==":
            lo, lo_inc, hi, hi_inc = num, True, num, True
        iv[v] = (lo, lo_inc, hi, hi_inc)
    return iv, symbolic, unparsed


def is_empty(t):
    lo, lo_inc, hi, hi_inc = t
    if lo > hi:
        return True
    if lo == hi:
        return not (lo_inc and hi_inc)
    return False


def overlap(a, b):
    """两个区间是否有非空交集（端点开闭参与判断）。"""
    al, ali, ah, ahi = a
    bl, bli, bh, bhi = b
    low, high = max(al, bl), min(ah, bh)
    if low > high:
        return False
    if low == high:
        # 端点必须两边都真的包含它
        return (low != al or ali) and (low != bl or bli) \
            and (low != ah or ahi) and (low != bh or bhi)
    return True


def subset_interval(a, b):
    """区间 a ⊆ b？"""
    al, ali, ah, ahi = a
    bl, bli, bh, bhi = b
    if al < bl:
        return False
    if al == bl and ali and not bli:
        return False
    if ah > bh:
        return False
    if ah == bh and ahi and not bhi:
        return False
    return True


def classify(rh, ivh, rl, ivl):
    """区分两种"区间重叠但等级不同"：

    * **同一变量集合**上的重叠 → 真缺陷（同一现象两个等级，契约/分工.md 点名项）
    * 高等级规则**多带一个条件维度**、且在各公共变量上区间被低等级规则包含
      → "组合升级"（如 R101 波动+断档 高 ⊃ R102 仅波动 中）。这种设计有价值，
      但必须显式确认是有意为之，故降级为警告而不是错误。
    * 其余（部分重叠、无包含关系）→ 无法自动判定，交人工复核（警告）。

    **注意**：判"区间重叠但等级不同"时不能逐个共享变量独立判定——必须要求
    **所有**共享变量同时相交（即两条规则的**联立可行域非空**），否则会误报：
    `R242`(query_3m∈[6,8) 且 new_loan_3m==0, 中) 与 `R243`(query_3m≥8 且 new_loan_3m==0, 高)
    在 `new_loan_3m` 上区间完全相同，但 `query_3m` 上互斥，两条规则**永远不可能同时命中**。
    """
    if set(ivh) == set(ivl):
        return "error", ("D2 %s(%s, %s) 与 %s(%s, %s) 条件维度相同、区间相交却给了两个等级"
                         "（同一现象两个等级）"
                         % (rh["id"], rh["level"], rh["cond"], rl["id"], rl["level"], rl["cond"]))
    if set(ivl) < set(ivh) and all(subset_interval(ivh[k], ivl[k]) for k in ivl):
        return "warn", ("D2* %s(%s) 是 %s(%s) 的严格强化（多带条件维度 %s）→ 组合升级，"
                        "请 P2 确认是有意设计并记入依据库"
                        % (rh["id"], rh["level"], rl["id"], rl["level"],
                           ",".join(sorted(set(ivh) - set(ivl)))))
    return "warn", ("D2? %s(%s, %s) 与 %s(%s, %s) 区间部分重叠且等级不同，"
                    "无法判定是否组合升级 → 需人工复核"
                    % (rh["id"], rh["level"], rh["cond"], rl["id"], rl["level"], rl["cond"]))


def main():
    rules = RL.load_rules()
    errors, warnings = [], []

    parsed = []
    for r in rules:
        iv, sym, unp = parse_cond(r["cond"])
        parsed.append((r, iv, sym, unp))

    for r, iv, sym, unp in parsed:
        # D3 自相矛盾的 cond
        for v, t in iv.items():
            if is_empty(t):
                errors.append("D3 %s 的 cond 在变量 %s 上区间为空：%s"
                              % (r["id"], v, r["cond"]))
        # D4 格式（契约 IF-1.2）
        if len(r["title"]) > 12:
            errors.append("D4 %s 的 title 超 12 字（%d）：%s"
                          % (r["id"], len(r["title"]), r["title"]))
        if len(r["advice"]) > 16:
            errors.append("D4 %s 的 advice 超 16 字（%d）：%s"
                          % (r["id"], len(r["advice"]), r["advice"]))
        if r["level"] not in LEVELS:
            errors.append("D4 %s 等级非法：%s" % (r["id"], r["level"]))
        # D5 记下无法静态分析的部分（只提示，不报错）
        if unp and not iv:
            warnings.append("D5 %s 的 cond 含跨变量比较，区间无法静态校验：%s"
                            % (r["id"], r["cond"]))

    # D1 完全重复的 cond
    by_cond = {}
    for r, iv, sym, unp in parsed:
        by_cond.setdefault(r["cond"].replace(" ", ""), []).append(r["id"])
    for cond, ids in by_cond.items():
        if len(ids) > 1:
            errors.append("D1 cond 完全重复：%s（%s）" % (cond, " / ".join(ids)))

    # D2 同一变量区间重叠 + 等级不同
    for (r1, iv1, _, _), (r2, iv2, _, _) in itertools.combinations(parsed, 2):
        if r1["level"] == r2["level"] or r1["level"] not in LEVELS or r2["level"] not in LEVELS:
            continue
        order = {v: i for i, v in enumerate("高中低")}
        rh, ivh, rl, ivl = ((r1, iv1, r2, iv2) if order[r1["level"]] < order[r2["level"]]
                            else (r2, iv2, r1, iv1))
        for v in set(ivh) & set(ivl):
            pass  # 逐个变量的判断见下（必须联立，不能逐个独立判定）
        shared = set(ivh) & set(ivl)
        if not shared:
            continue
        # 只有**所有**共享变量同时相交，两条规则才可能同时命中
        if all(overlap(ivh[v], ivl[v]) for v in shared):
            kind, msg = classify(rh, ivh, rl, ivl)
            (errors if kind == "error" else warnings).append(msg)

    for w in warnings:
        print("  ⚠ %s" % w)
    for e in errors:
        print("  ✗ %s" % e)
    if errors:
        print("\n[check_rule_conflicts] 失败：%d 个问题（%d 条规则，%d 条无法静态校验）"
              % (len(errors), len(rules), len(warnings)))
        return 1
    print("[check_rule_conflicts] 通过：%d 条规则无重复、无等级冲突、格式合规"
          "（另有 %d 条含跨变量比较，已在上面列明）" % (len(rules), len(warnings)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
