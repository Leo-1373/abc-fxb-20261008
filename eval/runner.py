#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""评测跑分工具（钱浩军专用，规范见 docs/评测方案.md）。

三种用法：

    --list              看题库概况：多少题、档位分布、held-out 比例
    --check             只校验题目本身写没写错（不调系统，纯静态检查）
    --score <dir>       用跑好的系统输出打分，出报告

**--check 里那道键名交叉校验是干什么的？**
    题目 `data` 里的键名，必须能在「各 skill.md 声明的输入槽 ∪ rules/dict.yaml 官方
    短码」里找到。对不上的键 = skill 取不到值 = 规则静默失效：该报的没报、扣分，
    但**全程不报错**。题目数据和 skill 输入契约是两个人分头写的，没有这道校验就
    只能等打分时看见"漏报"，还查不出原因。

**为什么不直接调系统？**
    平台怎么调用子 skill 还没定（李昊霖平台侦查未完成）。所以这里隔了一层文件：
    你先把系统对每道题的回答存成文件（文件名 = 题号），工具读文件来打分。
    平台一定，只要写个小脚本把回答落成文件，本工具一个字都不用改。

打分依据三件事（漏报与误报同等扣分）：
    should_find      该报的有没有报出来   —— 没报 = 漏报
    should_not_find  不该报的有没有报出来 —— 报了 = 误报
    risk_level       总体等级对不对

措辞不一样怎么办？靠 eval/synonyms.json 同义词表认亲。
认不出来的会单独列成"待人工确认"清单，最后由钱浩军拍板。

用法：
    PYTHONIOENCODING=utf-8 python3 eval/runner.py --list
    PYTHONIOENCODING=utf-8 python3 eval/runner.py --check
    PYTHONIOENCODING=utf-8 python3 eval/runner.py --score eval/out --report eval/out/report.md
"""
import os
import re
import sys
import json
import glob
import difflib
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES_DIR = os.path.join(ROOT, "eval", "cases")
OUT_DIR = os.path.join(ROOT, "eval", "out")
SYN_PATH = os.path.join(ROOT, "eval", "synonyms.json")
DICT_PATH = os.path.join(ROOT, "rules", "dict.yaml")
SKILLS_DIR = os.path.join(ROOT, "skills")

TIERS = ("L1", "L2", "L3")
DIMS = {"doc", "cash", "cred", "x", "ew"}
LEVELS = {"高", "中", "低"}
STAGES = {"贷前", "贷中", "贷后", "跨环节"}
# 题号里不该出现内部规则编号——出现就说明是照着规则库出的题，不干净
RULE_ID_RE = re.compile(r"^[RXE]\d+$")
# "追问/标注数据不全" 的语感词：命中任一即视为合格
CLARIFY_WORDS = ("请补充", "请提供", "请确认", "需要补充", "资料不全", "数据不全",
                 "无法判断", "缺失", "缺少", "暂缺")


# ────────────────────────── 读文件 ──────────────────────────

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_synonyms():
    if not os.path.isfile(SYN_PATH):
        return {}
    try:
        return load_json(SYN_PATH)
    except Exception:
        return {}


def iter_cases(include_heldout=False):
    """遍历题库，产出 (题目 dict, 文件路径, 是否 held-out 目录)。"""
    buckets = [("public", False)]
    if include_heldout:
        buckets.append(("heldout", True))
    for bucket, is_held in buckets:
        for tier in TIERS:
            for path in sorted(glob.glob(os.path.join(CASES_DIR, bucket, tier, "*.json"))):
                try:
                    case = load_json(path)
                except Exception as e:
                    yield {"case_id": os.path.splitext(os.path.basename(path))[0],
                           "_load_error": str(e)}, path, is_held
                    continue
                yield case, path, is_held


# ────────────────────────── 同义词匹配 ──────────────────────────

def syn_group(term, table):
    """把大白话术语展开成一组等价说法（含自身）。"""
    for key, aliases in table.items():
        if term == key or term in aliases:
            return [key] + [str(a) for a in aliases]
    return [term]


def match_terms(term, titles, table):
    """term 是否被 titles 中某条命中。命中则返回那条 title，否则返回 None。

    双向子串匹配，但要求词组至少 2 个字，避免"收入"这类过短词乱认。
    """
    group = [g for g in syn_group(term, table) if g and len(g) >= 2]
    for t in titles:
        if not t:
            continue
        for g in group:
            if g in t or t in g:
                return t
    return None


def has_clarify_signal(out):
    """系统是否表现出"我数据不够，需要追问"的姿态。"""
    risk = out.get("risk") or {}
    if not risk.get("level"):
        return True
    blob = " ".join([
        str(out.get("note") or ""),
        " ".join(str(a) for a in (out.get("actions") or [])),
    ])
    return any(w in blob for w in CLARIFY_WORDS)


def load_system_output(path):
    """读系统回答。支持两种形式：裸输出，或 {output:{...}, usage:{...}} 包一层。"""
    obj = load_json(path)
    if isinstance(obj.get("output"), dict):
        return obj["output"], (obj.get("usage") or {})
    return obj, (obj.get("usage") or {})


# ────────────────── 键名交叉校验：题目 data vs skill 声明的槽位 ──────────────────

# 需要钻进对象内部校验子字段的槽。这些槽的子字段是**官方短码级**的事实，
# 名字写错会让规则静默失效（`bad_debt` vs `bad_debt_flag` 就是这么漏掉的，
# 两条"一票否决"红线规则整整一周跑不到，而分数照扣、且不报错）。
# `applicant{}` 是自由申报信息，子字段由各 skill 的 prose 约定、不是短码，
# 钻进去只会产生误报，因此不列入。
DEEP_SLOTS = ("credit", "features", "loan")

_SLOT_CACHE = None


def _codes_in_dict():
    """rules/dict.yaml `vars:` 段里的官方短码（每行首列）。"""
    out = {}
    if not os.path.isfile(DICT_PATH):
        return out
    in_vars = False
    for raw in open(DICT_PATH, encoding="utf-8"):
        line = raw.rstrip("\n")
        if re.match(r"^vars:\s*\|", line):
            in_vars = True
            continue
        if in_vars and re.match(r"^\S", line):   # 顶格的下一段，vars 段到此结束
            break
        if not in_vars:
            continue
        p = [x.strip() for x in line.strip().split("|")]
        if p and re.match(r"^[a-z_][a-z0-9_]*$", p[0]):
            out[p[0]] = "dict.yaml"
    return out


def _slots_in_skills():
    """各 skill.md 声明的输入槽：frontmatter 的 `input_slots` + 「## 输入」表里的字段名。"""
    out = {}
    if not os.path.isdir(SKILLS_DIR):
        return out
    for sk in sorted(os.listdir(SKILLS_DIR)):
        path = os.path.join(SKILLS_DIR, sk, "skill.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        names = set()
        m = re.search(r"^input_slots:\s*(.+)$", text, re.M)
        if m:
            # 用标识符提取而非按逗号切：`[doc[], applicant{}, stage]` 里的 `[]`
            # 会让"切到第一个 ]"的写法把 doc 截成 doc[
            names |= set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", m.group(1)))
        sec = re.search(r"^## 输入\s*$(.*?)^## ", text, re.M | re.S)
        if sec:
            names |= set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", sec.group(1)))
        for n in names:
            out.setdefault(n, set()).add(sk)
    return out


def load_slots():
    """合法字段名 → 谁声明的。扫全库一次，进程内缓存。"""
    global _SLOT_CACHE
    if _SLOT_CACHE is None:
        declared = {}
        for k, src in _codes_in_dict().items():
            declared.setdefault(k, set()).add(src)
        for k, sks in _slots_in_skills().items():
            declared.setdefault(k, set()).update(sks)
        _SLOT_CACHE = declared
    return _SLOT_CACHE


def _probe_key(key, where, declared, errs):
    if key in declared:
        return
    near = difflib.get_close_matches(key, declared, n=1, cutoff=0.5)
    if near:
        hint = "；是不是想写 `%s`（%s 声明）？" % (near[0], "、".join(sorted(declared[near[0]])))
    else:
        hint = "；skills/*/skill.md 与 rules/dict.yaml 里都没有这个字段"
    errs.append("%s 里的 `%s` 没被任何 skill 声明%s" % (where, key, hint))


def check_slots(case, declared):
    """题目 `data` 的键名是否都落在 skill 声明过的槽位里。

    为什么必须查：题目数据（钱浩军）和 skill 输入契约（各 skill 作者）是两个人
    在两个时间写的，谁也不知道对方用了什么名字。名字对不上时 skill 取不到值 →
    规则不命中 → 该报的没报（漏报扣分），但**全程不报错**。这类静默失效
    只能靠键名交叉校验抓出来。
    """
    errs = []
    data = case.get("data") or {}
    for key, val in data.items():
        _probe_key(key, "data", declared, errs)
        if key in DEEP_SLOTS and isinstance(val, dict):
            for sub in val:
                _probe_key(sub, "data.%s{}" % key, declared, errs)
    return errs


# ────────────────────────── 静态校验 ──────────────────────────

def check_case(case, path, is_held):
    """校验单道题。返回问题清单（空 = 没问题）。"""
    errs = []
    name = os.path.basename(path)
    tier_dir = os.path.basename(os.path.dirname(path))

    if "_load_error" in case:
        return ["JSON 读不了：%s" % case["_load_error"]]

    cid = case.get("case_id")
    if not cid:
        errs.append("缺 case_id")
    elif cid != os.path.splitext(name)[0]:
        errs.append("case_id(%s) 和文件名(%s) 对不上" % (cid, name))

    if case.get("tier") not in TIERS:
        errs.append("tier 非法：%r" % case.get("tier"))
    elif case.get("tier") != tier_dir:
        errs.append("tier(%s) 和所在目录(%s) 对不上" % (case.get("tier"), tier_dir))

    if bool(case.get("held_out")) != is_held:
        errs.append("held_out(%s) 和所在目录(%s) 对不上"
                    % (case.get("held_out"), "heldout" if is_held else "public"))

    if not case.get("question"):
        errs.append("缺 question（用户原话）")

    if not case.get("data"):
        errs.append("缺 data（客户资料）")
    else:
        declared = load_slots()
        if declared:
            errs.extend(check_slots(case, declared))

    for d in (case.get("dim") or []):
        if d not in DIMS:
            errs.append("dim 非法：%s" % d)
    if not case.get("dim"):
        errs.append("dim 是空的")

    if case.get("stage") not in STAGES:
        errs.append("stage 非法：%r" % case.get("stage"))

    exp = case.get("expected")
    if not isinstance(exp, dict):
        errs.append("缺 expected")
    else:
        lv = exp.get("risk_level")
        if lv is not None and lv not in LEVELS:
            errs.append("expected.risk_level 非法：%r" % lv)
        for key in ("should_find", "should_not_find"):
            v = exp.get(key, [])
            if not isinstance(v, list):
                errs.append("expected.%s 应为数组" % key)
                continue
            for term in v:
                if RULE_ID_RE.match(str(term)):
                    errs.append("expected.%s 里出现内部规则编号「%s」"
                                "——题目必须用业务大白话，不能照规则库出题" % (key, term))
        overlap = set(exp.get("should_find") or []) & set(exp.get("should_not_find") or [])
        if overlap:
            errs.append("同一个词既该报又不该报：%s" % "、".join(sorted(overlap)))
        if not (exp.get("should_find") or exp.get("should_not_find")
                or exp.get("risk_level") or exp.get("coverage_partial")
                or exp.get("expect_clarify")):
            errs.append("expected 全空——这题考什么？")

    if not case.get("note"):
        errs.append("缺 note（出题人备注，人工复核全靠它）")

    return errs


# ────────────────────────── 打分 ──────────────────────────

def score_case(case, records, table):
    """给一道题打分。返回结果 dict。"""
    cid = case.get("case_id")
    exp = case.get("expected") or {}
    rec = records.get(cid)

    if rec is None:
        return {"case_id": cid, "status": "未跑", "miss": [], "false": [],
                "level_ok": None, "partial_ok": None, "clarify_ok": None,
                "expect_partial": None, "actual_partial": None, "partial_fatal": False,
                "unmatched": [], "detail": "找不到系统输出文件"}

    out, usage = rec
    titles = [p.get("title") for p in (out.get("points") or []) if isinstance(p, dict)]
    titles = [t for t in titles if t]

    # 该报的 —— 没报就是漏报
    miss = []
    for term in (exp.get("should_find") or []):
        if not match_terms(term, titles, table):
            miss.append(term)

    # 不该报的 —— 报了就是误报
    false_alarm = []
    for term in (exp.get("should_not_find") or []):
        hit = match_terms(term, titles, table)
        if hit:
            false_alarm.append("%s（系统报了：%s）" % (term, hit))

    # 总体等级
    level_ok = None
    actual_level = (out.get("risk") or {}).get("level") or ""
    if exp.get("risk_level"):
        level_ok = actual_level == exp["risk_level"]

    # 数据不全是否标注 —— **双向**校验。
    # 只查"该标没标"会漏掉另一半：题目数据齐全时系统仍喊"数据不全"，
    # 是拿"不敢下结论"当挡箭牌，同样该扣分。而此前 `coverage_partial: false`
    # 的题根本不验——那些题在系统什么都不报时天然通过，绿得毫无意义。
    partial_ok = None
    expect_partial = exp.get("coverage_partial")
    actual_partial = None
    if expect_partial is not None:
        cov = out.get("coverage")
        # coverage 有两种写法：子 skill 回传的字符串（`"partial"`）和主智能体
        # 汇成对象（`{"partial": true}`）。两种都得认——双向校验现在每题都跑，
        # 认不出形状会当场崩，而不是安静地漏一项。
        if isinstance(cov, dict):
            actual_partial = bool(cov.get("partial"))
        else:
            actual_partial = "partial" in str(cov or "").lower()
        partial_ok = actual_partial == bool(expect_partial)

    # 只有"该标没标"算硬失败。"不该标却标了"先只报出来、不判死——
    # 因为 `partial` 的口径两组还没统一：
    #   · docs/评测方案.md：数据缺**关键项**时填 true（题目的 false 是"没缺关键项"）
    #   · sk_cred/契约 IF-1.4：**任一**指标算不出来就标 partial
    # 一份 11 个字段的征信数据会让 cred.py 标出 18 项缺失 → 照契约行事的好系统
    # 反而被这 6 道题判死。这是**取证**性质的分歧，不是笔误，得先裁决口径再收紧。
    # 裁决前：报 ⚠ 不扣分。裁决后把 partial_ok 直接放进 passed 即可。
    partial_fatal = bool(expect_partial) and partial_ok is False

    # 该不该追问
    clarify_ok = None
    if exp.get("expect_clarify"):
        clarify_ok = has_clarify_signal(out)

    # 没被任何一条 expected 认领的报出项 —— 可能是系统多报了，也可能只是措辞不同
    claimed = set()
    for term in (exp.get("should_find") or []) + (exp.get("should_not_find") or []):
        hit = match_terms(term, titles, table)
        if hit:
            claimed.add(hit)
    unmatched = [t for t in titles if t not in claimed]

    passed = (not miss and not false_alarm
              and level_ok is not False and not partial_fatal
              and clarify_ok is not False)

    return {
        "case_id": cid, "status": "通过" if passed else "失败",
        "miss": miss, "false": false_alarm,
        "level_ok": level_ok, "partial_ok": partial_ok, "clarify_ok": clarify_ok,
        "expect_partial": expect_partial, "actual_partial": actual_partial,
        "partial_fatal": partial_fatal,
        "expect_level": exp.get("risk_level") or "—",
        "actual_level": actual_level or "（空）",
        "unmatched": unmatched,
        "titles": titles,
        "usage": usage,
        "note": case.get("note") or "",
    }


# ────────────────────────── 输出 ──────────────────────────

def collect(include_heldout=False):
    cases, problems = [], []
    for case, path, is_held in iter_cases(include_heldout):
        cases.append((case, path, is_held))
        for e in check_case(case, path, is_held):
            problems.append((os.path.basename(path), e))
    return cases, problems


def cmd_list(include_heldout):
    cases, problems = collect(include_heldout=True)   # 概况永远看全量
    if not cases:
        print("[runner] 题库还是空的。去 eval/cases/public/L1/ 下出第一道题吧。")
        return 0

    print("[runner] 题库概况")
    print("   总题数：%d" % len(cases))
    pub = [c for c, _, h in cases if not h]
    held = [c for c, _, h in cases if h]
    print("   公开题：%d    held-out：%d" % (len(pub), len(held)))
    if cases:
        ratio = len(held) * 100.0 / len(cases)
        flag = "✓" if 15 <= ratio <= 25 else "⚠ 目标 20%"
        print("   held-out 占比：%.0f%%  %s" % (ratio, flag))

    print("\n   按档位：")
    for tier in TIERS:
        row = [c for c, _, _ in cases if c.get("tier") == tier]
        h = [c for c in row if c.get("held_out")]
        print("     %-3s %3d 道（其中藏起来 %d 道）" % (tier, len(row), len(h)))

    print("\n   按维度：")
    counter = {}
    for c, _, _ in cases:
        for d in (c.get("dim") or []):
            counter[d] = counter.get(d, 0) + 1
    for d, n in sorted(counter.items(), key=lambda x: -x[1]):
        print("     %-6s %3d 道" % (d, n))

    # 环节最容易漏（赛题三个环节并列点名），单独列出来
    print("\n   按环节：")
    for s in ("贷前", "贷中", "贷后", "跨环节"):
        n = len([c for c, _, _ in cases if c.get("stage") == s])
        mark = "  ⚠ 一题都没有" if n == 0 else ""
        print("     %-5s %3d 道%s" % (s, n, mark))

    adv = [c for c, _, _ in cases if c.get("adversarial")]
    print("\n   坑题：%d 道（目标 ≥10）%s" % (len(adv), "✓" if len(adv) >= 10 else "⚠"))

    if problems:
        print("\n   ⚠ 有 %d 处题目写法问题，跑 --check 看详情" % len(problems))
    else:
        print("\n   ✓ 题目写法没问题")
    return 0


def cmd_check(include_heldout):
    cases, problems = collect(include_heldout)
    if not cases:
        print("[runner] 没找到题目（默认只看公开题，加 --include-heldout 看全部）。")
        return 0
    if not load_slots():
        print("   ⚠ 没扫到任何 skill 声明（skills/*/skill.md、rules/dict.yaml 都读不到），"
              "键名交叉校验已跳过")
    for name, e in problems:
        print("   ✗ %-24s %s" % (name, e))
    print()
    if problems:
        print("[runner] 校验不通过：%d 道题有 %d 处问题" %
              (len(set(n for n, _ in problems)), len(problems)))
        return 1
    print("[runner] 校验通过：%d 道题全部合规（公开题%s）"
          % (len(cases), " + held-out" if include_heldout else ""))
    return 0


def cmd_score(score_dir, include_heldout, report_path):
    cases, problems = collect(include_heldout)
    if not cases:
        print("[runner] 没找到题目。")
        return 1

    records = {}
    for name in os.listdir(score_dir) if os.path.isdir(score_dir) else []:
        if not name.endswith(".json"):
            continue
        try:
            records[os.path.splitext(name)[0]] = load_system_output(
                os.path.join(score_dir, name))
        except Exception as e:
            print("   ⚠ %s 读不了：%s" % (name, e))

    table = load_synonyms()
    results = []
    print("[runner] 打分中（题目 %d 道，系统输出 %d 份）…\n" % (len(cases), len(records)))
    for case, path, is_held in cases:
        r = score_case(case, records, table)
        r["tier"] = case.get("tier")
        r["held_out"] = bool(is_held)
        r["adversarial"] = bool(case.get("adversarial"))
        results.append(r)

    # 逐题
    for r in results:
        mark = {"通过": "✓", "失败": "✗", "未跑": "–"}[r["status"]]
        tag = " [坑题]" if r["adversarial"] else ""
        print("   %s %-16s %s%s" % (mark, r["case_id"], r["status"], tag))
        if r["miss"]:
            print("       漏报：%s" % "、".join(r["miss"]))
        if r["false"]:
            print("       误报：%s" % "、".join(r["false"]))
        if r["level_ok"] is False:
            print("       等级不对：期望 %s，系统报 %s"
                  % (r["expect_level"], r["actual_level"]))
        if r["partial_fatal"]:
            print("       该标数据不全却没标：期望标 partial，系统的 coverage 里没有")
        elif r.get("partial_ok") is False:
            print("       ⚠ 口径待裁决：题目说数据不缺关键项，系统标了 partial"
                  "（不影响判定，见 partial 口径分歧）")
        if r["clarify_ok"] is False:
            print("       该追问却没追问：题目要求先问不要硬答")
        if r["status"] != "未跑" and r["unmatched"]:
            print("       待确认（没认出来的报出项）：%s" % "、".join(r["unmatched"]))

    # 汇总
    done = [r for r in results if r["status"] != "未跑"]
    ok = [r for r in done if r["status"] == "通过"]
    print("\n[runner] 汇总")
    print("   跑了的题：%d 道，通过 %d 道，通过率 %s"
          % (len(done), len(ok),
             ("%.0f%%" % (len(ok) * 100.0 / len(done))) if done else "—"))
    print("   漏报合计：%d 项    误报合计：%d 项"
          % (sum(len(r["miss"]) for r in done), sum(len(r["false"]) for r in done)))

    for tier in TIERS:
        sub = [r for r in done if r["tier"] == tier]
        if sub:
            o = len([r for r in sub if r["status"] == "通过"])
            print("   %-3s ：%d/%d 通过" % (tier, o, len(sub)))

    # 开销
    tokens = sum((r.get("usage") or {}).get("tokens", 0) or 0 for r in results)
    secs = sum((r.get("usage") or {}).get("seconds", 0) or 0 for r in results)
    if tokens or secs:
        print("\n   开销：token %s，耗时 %.1f 秒" % (tokens, secs))
    else:
        print("\n   开销：系统输出里没带 token/耗时，统计不了（可选）")

    if problems:
        print("\n   ⚠ 有 %d 处题目写法问题，跑 --check 看详情" % len(problems))

    if report_path:
        write_report(report_path, results, problems)
        print("\n   报告已写出 → %s" % report_path)

    return 0 if len(ok) == len(done) else 1


def write_report(path, results, problems):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    done = [r for r in results if r["status"] != "未跑"]
    ok = [r for r in done if r["status"] == "通过"]
    lines = [
        "# 评测报告", "",
        "- 题目：%d 道（跑通 %d 道）" % (len(results), len(done)),
        "- 通过：%d 道，通过率 %s" % (len(ok),
                                  ("%.0f%%" % (len(ok) * 100.0 / len(done))) if done else "—"),
        "- 漏报：%d 项　误报：%d 项" % (sum(len(r["miss"]) for r in done),
                                    sum(len(r["false"]) for r in done)),
        "", "| 题号 | 档位 | 结果 | 漏报 | 误报 | 等级 |", "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["level_ok"] is None:
            lv = "—"
        elif r["level_ok"]:
            lv = "对"
        else:
            lv = "**错**（期望 %s，报 %s）" % (r["expect_level"], r["actual_level"])
        lines.append("| %s%s | %s | %s | %s | %s | %s |" % (
            r["case_id"], " 🕳" if r["adversarial"] else "", r["tier"], r["status"],
            "、".join(r["miss"]) or "—", "、".join(r["false"]) or "—", lv))
    unclear = [(r["case_id"], r["unmatched"]) for r in done if r["unmatched"]]
    if unclear:
        lines += ["", "## 待人工确认（工具没认出来的报出项）", ""]
        lines += ["- **%s**：%s" % (cid, "、".join(u)) for cid, u in unclear]
        lines += ["", "> 逐条判断：是系统多报了（误报），还是只是措辞不同没认上？",
                  "> 后者把词补进 `eval/synonyms.json` 即可。"]
    overclaim = [r["case_id"] for r in done
                 if r.get("partial_ok") is False and not r.get("partial_fatal")]
    if overclaim:
        lines += ["", "## ⚠ 口径待裁决：系统标了「数据不全」，题目说不缺关键项", "",
                  "- " + "、".join(overclaim), "",
                  "> 两组对 `partial` 的定义域不同，**暂不扣分**：",
                  "> `docs/评测方案.md` 说「缺**关键项**才标」，`sk_cred` 契约说「**任一**指标",
                  "> 算不出来就标」。一份 11 字段的征信数据会让 cred.py 标出 18 项缺失，",
                  "> 于是照契约行事的好系统会被判成「过度标注」。口径统一前此项只报不判。"]
    if problems:
        lines += ["", "## 题目写法问题", ""]
        lines += ["- %s：%s" % (n, e) for n, e in problems]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_index(out_path, include_heldout=True):
    """把题库导出成一份人看得懂的清单（题目文件本身是给电脑看的 JSON）。"""
    cases, problems = collect(include_heldout)
    if not cases:
        print("[runner] 题库是空的。")
        return 1

    lines = ["# 题库总表", "",
             "> 由 `python3 eval/runner.py --index` 自动生成，**不要手工编辑**。",
             "> 题目本体是 JSON（给电脑看），这份表是给人看的。", ""]

    # 概况
    pub = [c for c, _, h in cases if not h]
    held = [c for c, _, h in cases if h]
    lines += ["## 概况", "",
              "- 共 **%d** 道（公开 %d，藏起来 %d）" % (len(cases), len(pub), len(held)),
              ""]
    lines += ["| 档位 | 数量 |", "|---|---|"]
    for tier in TIERS:
        n = len([c for c, _, _ in cases if c.get("tier") == tier])
        lines.append("| %s | %d |" % (tier, n))
    lines += ["", "| 环节 | 数量 |", "|---|---|"]
    for s in ("贷前", "贷中", "贷后", "跨环节"):
        n = len([c for c, _, _ in cases if c.get("stage") == s])
        lines.append("| %s | %d%s |" % (s, n, " ⚠" if n == 0 else ""))
    lines.append("")

    # 逐题
    for tier in TIERS:
        row = [(c, p, h) for c, p, h in cases if c.get("tier") == tier]
        if not row:
            continue
        lines += ["## %s（%d 道）" % (tier, len(row)), ""]
        for case, path, is_held in row:
            exp = case.get("expected") or {}
            tags = case.get("tags") or []
            head = "### %s　%s" % (case.get("case_id"), case.get("question") or "")
            if is_held:
                head += "　🔒 **藏起来**"
            if case.get("adversarial"):
                head += "　🕳 **坑题**"
            lines += [head, ""]
            lines += ["- 环节：%s　维度：%s%s"
                      % (case.get("stage"), "、".join(case.get("dim") or []),
                         ("　标签：" + "、".join(tags)) if tags else "")]
            find = exp.get("should_find") or []
            avoid = exp.get("should_not_find") or []
            lines.append("- 该报出来：%s" % ("、".join(find) if find else "（什么都不该报）"))
            if avoid:
                lines.append("- 不该报：%s" % "、".join(avoid))
            if exp.get("expect_clarify"):
                lines.append("- 应该做的是：**先追问，别硬答**")
            if exp.get("coverage_partial"):
                lines.append("- 应该标注：数据不全")
            lines += ["- 出题人备注：%s" % (case.get("note") or "—"), ""]
    if problems:
        lines += ["## 写法问题", ""] + ["- %s：%s" % (n, e) for n, e in problems] + [""]

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[runner] 题库总表已写出 → %s（共 %d 道）" % (out_path, len(cases)))
    return 0


def main():
    ap = argparse.ArgumentParser(description="评测跑分工具（钱浩军）")
    ap.add_argument("--list", action="store_true", help="看题库概况")
    ap.add_argument("--check", action="store_true", help="只校验题目写法")
    ap.add_argument("--score", metavar="DIR", help="用系统输出目录打分")
    ap.add_argument("--index", metavar="PATH", nargs="?", const="docs/题库总表.md",
                    help="导出人看得懂的题库清单（默认 docs/题库总表.md）")
    ap.add_argument("--include-heldout", action="store_true",
                    help="把藏起来的题也算进来（默认不算）")
    ap.add_argument("--report", metavar="PATH", help="把报告写成 markdown")
    args = ap.parse_args()

    if args.list:
        return cmd_list(args.include_heldout)
    if args.check:
        return cmd_check(args.include_heldout)
    if args.score:
        return cmd_score(args.score, args.include_heldout, args.report)
    if args.index:
        path = args.index
        if not os.path.isabs(path):
            path = os.path.join(ROOT, path)
        return cmd_index(path, True)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
