#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""组装自包含 zip 包（契约 IF-5.4）。

流水线： 清理 stage → 复制源文件 → 注入 top-N 内联规则 → 分发规则闭包
        → 闭包校验（不通过则拒绝打包） → 打 zip 到 dist/

设计要点：
  * package 自包含 —— 每个 zip 只含自己那份 rules.closure.md，无跨包依赖。
  * top-N 内联 —— skill.md 正文里的 <!--INLINE_RULES--> 占位符在构建期被替换成
    高频规则。规则内容仍只有一份 SSOT（rules.yaml），构建期才落到各包。

用法：
    python build/pack.py              # 全量打包
    python build/pack.py --skill sk_cash
    python build/pack.py --no-zip     # 只组装 stage，不出 zip
"""
import os
import sys
import shutil
import zipfile
import argparse
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ruleslib as RL

HERE = os.path.dirname(os.path.abspath(__file__))
PLACEHOLDER = "<!--INLINE_RULES-->"
IGNORE_EXT = {".pyc", ".pyo"}
IGNORE_DIR = {"__pycache__", ".pytest_cache", ".idea", ".vscode"}
# 操作系统/编辑器生成的元数据文件：不是源文件，绝不能进交付包。
# macOS 只要用 Finder 浏览过目录就会生成 .DS_Store——之前它被原样打进了
# sk_cash.zip（+6KB）。按小写文件名匹配（大小写不敏感）。
IGNORE_FILES = {".ds_store", "thumbs.db", "desktop.ini"}


def _skip_file(fn):
    return fn.lower() in IGNORE_FILES or os.path.splitext(fn)[1] in IGNORE_EXT


def copy_tree(src, dst):
    os.makedirs(dst, exist_ok=True)
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for fn in files:
            if _skip_file(fn):
                continue
            shutil.copy2(os.path.join(root, fn), os.path.join(target, fn))


def inject_inline_rules(skill_md, name, rules, version):
    """把 <!--INLINE_RULES--> 替换为本 skill 的 top-N 高频规则。"""
    text = RL.read_text(skill_md)
    if PLACEHOLDER not in text:
        return False
    topn = set(RL.inline_topn())
    mine = [r for r in rules if r["owner"] == name and r["id"] in topn]
    if not mine:
        mine = [r for r in rules if r["owner"] == name][:6]   # 兜底：取前 6 条
    block = "\n".join([
        "<!-- 以下规则由 build/pack.py 从 rules/rules.yaml 构建期注入，勿手工编辑 -->",
        "```",
        "|".join(RL.RULE_COLS),
    ] + ["|".join(r[c] for c in RL.RULE_COLS) for r in mine] + [
        "```",
        "",
        "> 以上为高频规则（规则库 v%s）。完整闭包见 `references/rules.closure.md`。" % version,
    ])
    RL.write_text(skill_md, text.replace(PLACEHOLDER, block))
    return True


def zip_dir(src, out_zip):
    os.makedirs(os.path.dirname(out_zip), exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIR]
            for fn in sorted(files):
                if _skip_file(fn):
                    continue
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, src).replace(os.sep, "/")
                z.write(full, arc)
    return out_zip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", default=None, help="只打包指定 skill")
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    rules = RL.load_rules()
    version = RL.rules_version()

    # 1) 清理 stage
    if os.path.isdir(RL.STAGE_DIR) and not args.skill:
        shutil.rmtree(RL.STAGE_DIR)
    os.makedirs(RL.STAGE_DIR, exist_ok=True)

    targets = []
    if os.path.isfile(os.path.join(RL.MAIN_DIR, "skill.md")):
        targets.append(("main_agent", RL.MAIN_DIR))
    for name, sdir in RL.list_skills():
        if args.skill and name != args.skill:
            continue
        targets.append((name, sdir))

    if not targets:
        print("[pack] 没有可打包的目标。")
        return 1

    # 2) 复制源 + 注入内联规则
    for name, sdir in targets:
        dst = os.path.join(RL.STAGE_DIR, name)
        copy_tree(sdir, dst)
        if inject_inline_rules(os.path.join(dst, "skill.md"), name, rules, version):
            print("  · %-10s 已注入 top-N 内联规则" % name)

    # 3) 分发规则闭包
    print("[pack] 分发规则闭包…")
    r = subprocess.run([sys.executable, os.path.join(HERE, "distribute_rules.py")],
                       capture_output=True, text=True, encoding="utf-8")
    sys.stdout.write(r.stdout or "")
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        print("[pack] 分发失败，终止。")
        return 1

    # 4) 闭包校验 —— 不通过就拒绝打包（故障左移）
    print("[pack] 闭包校验…")
    r = subprocess.run([sys.executable, os.path.join(HERE, "check_closure.py")],
                       capture_output=True, text=True, encoding="utf-8")
    sys.stdout.write(r.stdout or "")
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
        print("[pack] 闭包校验未通过，拒绝打包。")
        return 1

    if args.no_zip:
        print("[pack] 已组装到 %s（未出 zip）" % RL.STAGE_DIR)
        return 0

    # 5) 打 zip
    os.makedirs(RL.DIST_DIR, exist_ok=True)
    for name, _ in targets:
        out = zip_dir(os.path.join(RL.STAGE_DIR, name),
                      os.path.join(RL.DIST_DIR, "%s.zip" % name))
        size = os.path.getsize(out)
        print("  ✓ %-22s %6.1f KB" % (os.path.basename(out), size / 1024.0))

    print("[pack] 完成 → %s" % RL.DIST_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
