#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""依据库 ↔ 规则表 一致性校验（邱家杰任务1 的收尾检查）。

**为什么需要这个脚本**：
`rules/rules.yaml` 是规则 SSOT，`skills/sk_cred/references/征信审查要点.md` 是它的**依据库**
（每条规则 `basis` 列引用的"征信审查要点N"在那里展开）。两者是**手工同步**的，
一旦漂移就是**静默错误**：规则改了阈值，依据库还在解释老阈值，读的人会被误导。

`build/check_closure.py` 校验不了这件事——它只管规则闭包与 frontmatter，
看不见依据库的正文。所以这里单独校。

校验项：
  B1 依据库提到的规则 id 必须真实存在（防幻影 id / 笔误）
  B2 每条 cred 规则都要被依据库覆盖（防漏写）
  B3 规则的 `level` 必须出现在该规则的依据库行里（防等级漂移）
  B4 `cond` 里的每个数字字面量都要能在该规则的依据库行里找到（防阈值漂移）
  B5 附表D（要点→规则索引）必须与 `rules.yaml` 的 `basis` 列一致（防索引漂移）

**注意**：本脚本**刻意不接入 `build/pack.py`**。它属邱家杰的自检工具，
是否升级为构建门槛由李昊霖决定（接入后，依据库漏写一条就会阻断全员构建）。

用法：
    PYTHONIOENCODING=utf-8 python build/check_basis.py
退出码：0 通过，1 有不一致。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ruleslib as RL

DOC = os.path.join(RL.ROOT, "skills", "sk_cred", "references", "征信审查要点.md")


def doc_lines_for(lines, rid):
    pat = re.compile(r"\b%s\b" % re.escape(rid))
    return [l for l in lines if pat.search(l)]


def parse_appendix_d(lines):
    """解析附表D（要点→规则索引）→ {要点号: {规则 id}}。"""
    out, in_d = {}, False
    for line in lines:
        if line.startswith("## 附表"):
            in_d = line.startswith("## 附表D")
            continue
        if not in_d or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        m = re.match(r"(\d+)\b", cells[0])
        if not m or cells[0].startswith("-"):
            continue
        out[int(m.group(1))] = set(re.findall(r"\bR\d{2,4}\b", cells[1]))
    return out


def _nums(s):
    """抽出字符串里的数字并归一化（30 与 30.0 视为同一个，0.50 与 0.5 视为同一个）。"""
    out = set()
    for x in re.findall(r"\d+\.?\d*", s):
        try:
            out.add("%g" % float(x))
        except ValueError:
            continue
    return out


def cond_rows(lines, rid):
    """『分档与依据』表里『规则』列恰为本 id 的行 → [[cells]]。

    这张表的列序固定为 `条件|等级|规则|依据|级别`（个别要点为 4 列），
    所以用 cells[2] 精确定位，**不要**再用"cell[0] 是否以数字开头"之类的启发式判断——
    那会把 `1 ≤ overdue_max_days < 30` 这类合法条件行误当成索引行跳过
    （本脚本早期版本正是这么漏掉 R214 漂移的）。
    """
    out = []
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 4 and cells[2].strip("`* ") == rid:
            out.append(cells)
    return out


def main():
    if not os.path.isfile(DOC):
        print("[check_basis] 找不到依据库：%s" % DOC)
        return 1

    all_rules = RL.load_rules()
    cred_rules = [r for r in all_rules if r["dim"] == "cred"]
    doc = RL.read_text(DOC)
    lines = doc.splitlines()
    fails = []

    # B1 幻影 id
    all_ids = {r["id"] for r in all_rules}
    mentioned = set(re.findall(r"\bR\d{2,4}\b", doc))
    phantom = sorted(mentioned - all_ids)
    if phantom:
        fails.append("B1 依据库提到规则库中不存在的 id：%s" % ", ".join(phantom))

    # B2 覆盖
    uncovered = [r["id"] for r in cred_rules if not doc_lines_for(lines, r["id"])]
    if uncovered:
        fails.append("B2 cred 规则未被依据库覆盖：%s" % ", ".join(uncovered))

    # B3 等级 / B4 阈值 —— 必须有一张"见证行"：规则列 == 本 id，
    # 且该行的【等级】与【条件数字集合】都与 rules.yaml 一致。
    for r in cred_rules:
        rows = cond_rows(lines, r["id"])
        if not rows:
            fails.append("B3 %s 在依据库中找不到『分档与依据』行，无法核对阈值与等级（cond=%s）"
                         % (r["id"], r["cond"]))
            continue
        want = _nums(r["cond"])
        for cells in rows:
            if r["level"] not in cells[1]:
                fails.append("B3 %s 等级不一致：依据库写 %r，规则库是 %s"
                             % (r["id"], cells[1], r["level"]))
            got = _nums(cells[0])
            if got != want:
                fails.append("B4 %s 阈值集合不一致：依据库 %s（条件：%s） vs 规则库 %s（cond：%s）"
                             % (r["id"], sorted(got) or "无", cells[0][:48],
                                sorted(want) or "无", r["cond"]))

    # B5 附表D 与 basis 一致
    expect = {}
    for r in cred_rules:
        m = re.match(r"征信审查要点(\d+)", r["basis"])
        if m:
            expect.setdefault(int(m.group(1)), set()).add(r["id"])
    got = parse_appendix_d(lines)
    for n in sorted(set(expect) | set(got)):
        e, g = expect.get(n, set()), got.get(n, set())
        if e != g:
            fails.append("B5 附表D 要点%d 与 rules.yaml 的 basis 不一致（依据库缺 %s / 多 %s）"
                         % (n, ",".join(sorted(e - g)) or "无", ",".join(sorted(g - e)) or "无"))

    for f in fails:
        print("  ✗ %s" % f)
    if fails:
        print("\n[check_basis] 失败：%d 处不一致——依据库与规则表已漂移，请同步。" % len(fails))
        return 1
    print("[check_basis] 通过：%d 条 cred 规则的依据、等级、阈值、索引均与规则表一致"
          % len(cred_rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
