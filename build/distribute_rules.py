#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按触发闭包切片分发规则（契约 IF-5.4）。

读 rules/rules.yaml（SSOT）+ 各 skill 的 rules_required
→ 写各包 build/stage/<skill>/references/rules.closure.md + ruleset.lock

这一步是「单一事实来源」与「包自包含」两者的接缝：
源码期只有一份规则，构建期才复制成 N 份互不依赖的切片。

用法：
    python build/distribute_rules.py            # 分发到 build/stage/
    python build/distribute_rules.py --out DIR  # 指定输出根目录
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ruleslib as RL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=RL.STAGE_DIR, help="输出根目录")
    args = ap.parse_args()

    rules = RL.load_rules()
    dict_all = RL.load_dict()
    thresholds = RL.load_thresholds()
    version = RL.rules_version()
    skills = RL.list_skills()

    if not skills:
        print("[distribute] 未发现任何 skill（skills/sk_*/skill.md），跳过。")
        return 0

    print("[distribute] 规则库 v%s，共 %d 条规则" % (version, len(rules)))
    total_written = 0

    for name, sdir in skills:
        fm = RL.load_frontmatter(os.path.join(sdir, "skill.md"))
        closure = RL.skill_closure(name, fm, rules)
        if not closure:
            print("  ⚠ %-10s 闭包为空——检查 rules_required 是否填了 dims/ids" % name)

        out_dir = os.path.join(args.out, name, "references")
        RL.write_text(os.path.join(out_dir, "rules.closure.md"),
                      RL.render_closure(name, closure, dict_all, thresholds, version))
        RL.write_text(os.path.join(out_dir, "ruleset.lock"),
                      RL.render_lock(name, closure, version))
        total_written += len(closure)
        print("  ✓ %-10s 规则 %2d 条 → %s" % (name, len(closure), out_dir))

    print("[distribute] 完成，共分发 %d 条（去重前）" % total_written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
