#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建链路共享工具：解析 rules.yaml / dict.yaml / skill.md frontmatter。

契约 IF-5.4。本模块不打包进任何 zip——它只在构建期工作。
环境约束：一律用 `python`（非 python3），控制台中文需 PYTHONIOENCODING=utf-8。
"""
import re
import os
import hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_YAML = os.path.join(ROOT, "rules", "rules.yaml")
DICT_YAML = os.path.join(ROOT, "rules", "dict.yaml")
SKILLS_DIR = os.path.join(ROOT, "skills")
MAIN_DIR = os.path.join(ROOT, "main")
STAGE_DIR = os.path.join(ROOT, "build", "stage")
DIST_DIR = os.path.join(ROOT, "dist")


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def load_scalar(path, key, default=""):
    """取顶层标量键值，如 version: 1.0.0"""
    pat = re.compile(r"^%s:\s*(.*)$" % re.escape(key))
    for line in read_text(path).splitlines():
        m = pat.match(line)
        if m:
            return m.group(1).strip()
    return default


def load_block(path, key):
    """取顶层 YAML 块标量（key: | 形式），返回去掉公共缩进后的行列表。

    本项目刻意不用 yaml 库依赖——块标量正好是 diff 友好的纯文本。
    """
    lines = read_text(path).splitlines()
    out, in_block, base = [], False, None
    start = re.compile(r"^%s:\s*\|" % re.escape(key))
    for line in lines:
        if not in_block:
            if start.match(line):
                in_block = True
            continue
        if line.strip() == "":
            out.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if base is None:
            base = indent
        if indent < base:
            break
        out.append(line[base:])
    while out and out[-1] == "":
        out.pop()
    return out


# ── 规则 ──────────────────────────────────────────────────────────

RULE_COLS = ["id", "dim", "cond", "level", "title", "advice", "basis", "owner"]


def load_rules():
    """解析 rules.yaml 的 rules 块 → [dict]。跳过注释与空行。"""
    rules = []
    for raw in load_block(RULES_YAML, "rules"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != len(RULE_COLS):
            raise ValueError("规则列数不符（期望 %d，实际 %d）：%s"
                             % (len(RULE_COLS), len(parts), line))
        rules.append(dict(zip(RULE_COLS, parts)))
    return rules


def rules_version():
    return load_scalar(RULES_YAML, "version", "0.0.0")


def shared_inputs():
    """跨维度输入白名单：cond 中允许出现、但不由任何 skill 计算的上游变量。"""
    names = []
    for raw in load_block(RULES_YAML, "shared_inputs"):
        line = raw.split("#")[0].strip()
        if line:
            names.append(line)
    return names


def inline_topn():
    raw = " ".join(load_block(RULES_YAML, "inline_topn"))
    return [x.strip() for x in raw.split(",") if x.strip()]


# ── 字典 ──────────────────────────────────────────────────────────

def load_dict():
    """解析 dict.yaml 的 vars 块 → {code: {...}}"""
    out = {}
    for raw in load_block(DICT_YAML, "vars"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        out[parts[0]] = {
            "code": parts[0], "dim": parts[1], "name": parts[2],
            "type": parts[3],
            "range": parts[4] if len(parts) > 4 else "",
            "derivation": parts[5] if len(parts) > 5 else "",
        }
    return out


def load_thresholds():
    out = {}
    for raw in load_block(DICT_YAML, "thresholds"):
        line = raw.split("#")[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4:
            out[parts[0]] = {"贷前": parts[1], "贷中": parts[2], "贷后": parts[3]}
    return out


# ── 表达式 ────────────────────────────────────────────────────────

KEYWORDS = {"true", "false"}


def cond_vars(cond):
    """抽取 cond 表达式里的变量名（排除数字与关键字）。"""
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9]*", cond)
    return sorted({t for t in tokens if t.lower() not in KEYWORDS})


# ── skill frontmatter ─────────────────────────────────────────────

def load_frontmatter(skill_md_path):
    """解析 skill.md 的 YAML frontmatter。只支持本项目用到的子集：
    标量、行内数组 [a, b]、缩进子映射（rules_required）、对象列表（refs_index）。

    刻意不依赖 yaml 库——构建环境未必装得上，而 frontmatter 的形状是我们自己定的。
    """
    text = read_text(skill_md_path)
    m = re.match(r"^---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        raise ValueError("缺少 frontmatter：%s" % skill_md_path)
    lines = m.group(1).splitlines()

    root = {}
    stack = [(-1, root)]          # (indent, container)

    for i, raw in enumerate(lines):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if line.startswith("- "):
            if not isinstance(parent, list):
                continue
            item = line[2:].strip()
            if ":" in item:
                k, _, v = item.partition(":")
                d = {k.strip(): _coerce(v)}
                parent.append(d)
                stack.append((indent, d))     # 后续更深的键归入本条
            else:
                parent.append(_coerce(item))
            continue

        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()

        if val == "":
            # 前瞻决定是子映射还是对象列表
            nxt_indent, nxt_is_list = -1, False
            for j in range(i + 1, len(lines)):
                s = lines[j].strip()
                if s and not s.startswith("#"):
                    nxt_indent = len(lines[j]) - len(lines[j].lstrip())
                    nxt_is_list = s.startswith("- ")
                    break
            container = [] if (nxt_is_list and nxt_indent > indent) else {}
            parent[key] = container
            stack.append((indent, container))
        else:
            parent[key] = _coerce(val)

    return root


def _coerce(v):
    v = v.strip().strip('"').strip("'")
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce(x) for x in inner.split(",")]
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    return v


def list_skills():
    """返回 [(name, dir)]，跳过 _template。"""
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for name in sorted(os.listdir(SKILLS_DIR)):
        d = os.path.join(SKILLS_DIR, name)
        if name.startswith("_") or not os.path.isdir(d):
            continue
        if os.path.isfile(os.path.join(d, "skill.md")):
            out.append((name, d))
    return out


# ── 闭包切片 ──────────────────────────────────────────────────────

def skill_closure(name, fm, rules):
    """计算某 skill 的规则触发闭包。

    纳入条件（并集）：
      1. owner == 本 skill（自己的规则）
      2. id ∈ rules_required.ids（显式点名）
      3. dim ∈ rules_required.dims（按维度申请）
    """
    req = fm.get("rules_required") or {}
    if not isinstance(req, dict):
        req = {}
    want_ids = set(req.get("ids") or [])
    want_dims = set(req.get("dims") or [])
    out = []
    for r in rules:
        if r["owner"] == name or r["id"] in want_ids or r["dim"] in want_dims:
            out.append(r)
    return out


def render_closure(name, closure, dict_all, thresholds, version):
    """渲染 rules.closure.md——构建期切片，运行时懒加载。"""
    used_vars = set()
    for r in closure:
        used_vars.update(cond_vars(r["cond"]))
    lines = [
        "# 规则闭包切片 · %s" % name,
        "",
        "> 本文件由 `build/distribute_rules.py` 自动生成，**请勿手工编辑**。",
        "> 规则库版本：%s ｜ 本包规则数：%d" % (version, len(closure)),
        "> 修改规则请改 `rules/rules.yaml` 后重新构建。",
        "",
        "## 本包规则",
        "",
        "```",
        "|".join(RULE_COLS),
    ]
    for r in closure:
        lines.append("|".join(r[c] for c in RULE_COLS))
    lines += ["```", "", "## 变量字典（仅本包用到的）", "",
              "```", "code|type|name|derivation"]
    for code in sorted(used_vars):
        v = dict_all.get(code)
        if v:
            lines.append("|".join([v["code"], v["type"], v["name"], v["derivation"]]))
    unknown = sorted(c for c in used_vars if c not in dict_all)
    if unknown:
        lines += ["```", "", "## ⚠ 未在字典中定义的变量", "", "```"]
        lines += unknown

    if thresholds:
        lines += ["```", "", "## 环节阈值", "", "```", "变量|贷前|贷中|贷后"]
        for k, v in thresholds.items():
            if k in used_vars:
                lines.append("|".join([k, v["贷前"], v["贷中"], v["贷后"]]))

    lines += ["```", ""]
    return "\n".join(lines)


def render_lock(name, closure, version):
    ids = [r["id"] for r in closure]
    h = hashlib.sha256(("|".join(sorted(ids)) + version).encode("utf-8")).hexdigest()[:16]
    return "\n".join([
        "# ruleset.lock —— 闭包自检用（契约 IF-5.3）",
        "# 运行时若本文件与 references/rules.closure.md 不一致，标注 coverage: partial",
        "skill: %s" % name,
        "rules_version: %s" % version,
        "rule_count: %d" % len(ids),
        "digest: %s" % h,
        "rule_ids: %s" % ",".join(sorted(ids)),
        "",
    ])
