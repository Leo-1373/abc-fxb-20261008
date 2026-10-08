#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""闭包完整性校验（契约 IF-5.4）。

**故障左移原则**：宁可构建失败，不可运行时翻车。
缺规则的 exit 1 优于运行时静默跳过——静默跳过会让风险点凭空消失，
而这类错误在评测里表现为"漏报"，极难归因。

校验项：
  C1 规则 id 全局唯一
  C2 每条的 owner 对应一个真实存在的 skill 目录
  C3 每条的 level ∈ {高,中,低}（IF-1.1 标尺）
  C4 cond 中的变量要么在 dict.yaml 定义，要么在 shared_inputs 白名单
  C5 rules_required.ids 点名的规则必须真实存在
  C6 已分发的 ruleset.lock 必须与当前规则库一致（防止改了规则没重跑构建）
  C7 每个 skill.md 的 frontmatter 含 IF-2 规定的 8 个必需字段
  C8 每个 skill.md 正文含 `## 边界` 节

退出码：0 通过，1 有错误。
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ruleslib as RL

LEVELS = {"高", "中", "低"}
REQUIRED_FM = ["name", "description", "version", "contract", "input_slots",
               "output_keys", "rules_required", "refs_index"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default=RL.STAGE_DIR, help="已分发产物目录")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    errors, warns = [], []

    rules = RL.load_rules()
    dict_all = RL.load_dict()
    allowed = set(dict_all) | set(RL.shared_inputs())
    skills = RL.list_skills()
    skill_names = {n for n, _ in skills}
    version = RL.rules_version()

    # C1 唯一性
    seen = {}
    for r in rules:
        if r["id"] in seen:
            errors.append("C1 规则 id 重复：%s" % r["id"])
        seen[r["id"]] = r

    # C2 owner 存在
    for r in rules:
        if r["owner"] not in skill_names:
            errors.append("C2 规则 %s 的 owner=%s 没有对应 skill 目录"
                          % (r["id"], r["owner"]))

    # C3 等级标尺
    for r in rules:
        if r["level"] not in LEVELS:
            errors.append("C3 规则 %s 等级非法：%s（只能是 高/中/低）"
                          % (r["id"], r["level"]))

    # C4 cond 变量可解释
    for r in rules:
        for v in RL.cond_vars(r["cond"]):
            if v not in allowed:
                errors.append("C4 规则 %s 的 cond 用了未定义变量：%s" % (r["id"], v))

    # C5/C7/C8 + 闭包一致性
    for name, sdir in skills:
        md = os.path.join(sdir, "skill.md")
        try:
            fm = RL.load_frontmatter(md)
        except ValueError as e:
            errors.append("C7 %s" % e)
            continue

        for k in REQUIRED_FM:
            if k not in fm:
                errors.append("C7 %s 的 frontmatter 缺少字段：%s" % (name, k))

        body = RL.read_text(md)
        if "## 边界" not in body:
            errors.append("C8 %s 缺少 `## 边界` 节（防 skill 膨胀的关键节）" % name)

        req = fm.get("rules_required") or {}
        if not isinstance(req, dict):
            req = {}
        for rid in (req.get("ids") or []):
            if rid not in seen:
                errors.append("C5 %s 点名了不存在的规则 id：%s" % (name, rid))

        if fm.get("name") != name:
            warns.append("frontmatter name=%s 与目录名 %s 不一致"
                         % (fm.get("name"), name))

        # C6 分发产物与当前规则库一致
        lock = os.path.join(args.stage, name, "references", "ruleset.lock")
        if os.path.isfile(lock):
            want = {r["id"] for r in RL.skill_closure(name, fm, rules)}
            txt = RL.read_text(lock)
            got = set()
            for line in txt.splitlines():
                if line.startswith("rule_ids:"):
                    got = {x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()}
            lv = ""
            for line in txt.splitlines():
                if line.startswith("rules_version:"):
                    lv = line.split(":", 1)[1].strip()
            if got != want:
                errors.append("C6 %s 的 ruleset.lock 已过期（缺 %s / 多 %s）——请重跑 distribute_rules.py"
                              % (name,
                                 ",".join(sorted(want - got)) or "无",
                                 ",".join(sorted(got - want)) or "无"))
            if lv != version:
                errors.append("C6 %s 的 ruleset.lock 版本 %s ≠ 规则库 %s"
                              % (name, lv, version))
        else:
            warns.append("%s 尚未分发（build/stage 下无 ruleset.lock）" % name)

    for w in warns:
        print("  ⚠ %s" % w)
    for e in errors:
        print("  ✗ %s" % e)

    if errors:
        print("\n[check_closure] 失败：%d 个错误，拒绝构建。" % len(errors))
        return 1

    if not args.quiet:
        print("[check_closure] 通过：%d 条规则 / %d 个 skill / 规则库 v%s"
              % (len(rules), len(skills), version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
