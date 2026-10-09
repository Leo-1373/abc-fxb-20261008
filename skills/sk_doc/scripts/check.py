#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sk_doc 材料逐项比对与规则求值（契约 IF-1.3 / IF-5.5）。

三个子命令：
  check      材料清单 + 申请信息 → doc 维度短码（缺件 / 过期 / 矛盾 / 红线）
  evaluate   短码 + 规则闭包 → L1 摘要 / L2 证据明细
  selfcheck  闭包自检

为什么比对放在脚本里而不是让模型逐项看（分工.md P3 任务 3）：
  1. 材料几十项，模型逐项比对又慢又容易漏，而且漏了不报错
  2. 比对是确定性的活，天然幂等——同输入同输出
  3. 只回传 6 个短码，不把整张材料表送进主智能体上下文

**必备材料清单.md 是本脚本的输入**：清单改了，脚本行为跟着改，不需要动代码。
这正是 skill.md 里「先跑脚本聚合，模型只做判断」的落地方式。

环境约束：一律用 `python`（非 python3），中文输出加 PYTHONIOENCODING=utf-8。

用法：
    python check.py check    --input doc.json --checklist references/必备材料清单.md --stage 贷前
    python check.py evaluate --facts facts.json --closure references/rules.closure.md
    python check.py selfcheck --closure references/rules.closure.md --lock references/ruleset.lock
"""
import os
import re
import sys
import json
import datetime
import argparse

# 必备材料清单.md 里材料表的列序（改清单必须同步改这里）
MATERIAL_COLS = ["type", "名称", "环节", "类别", "必需", "红线", "核验要点", "依据"]

# 明细比对：哪些材料承载同一事实、该跟谁比。
# ⚠ 姓名不能"逢件就比"——配偶身份证、保证人身份证上的姓名**本来就不等于申请人**，
#   逢件比对会把每一个已婚申请人都判成材料矛盾（误报）。故显式限定比对范围。
APPLICANT_NAMED = {"ID_CARD", "LOAN_APPLY", "LOAN_CONTRACT", "ACCOUNT_PROOF"}
COMPARE_RULES = [
    ("name",   APPLICANT_NAMED,                                       "name"),
    ("id_no",  APPLICANT_NAMED,                                       "id_no"),
    ("area",   {"LAND_CERT", "PROJECT_PLAN"},                         "declared_area"),
    ("amount", {"PURCHASE_CONTRACT", "APPRAISAL_REPORT",
                "FARM_MACHINE_CONTRACT"},                             "declared_amount"),
]

# 应签章处（必备材料清单 §5.3）。缺签章 ≠ 缺件，两者分开计。
SIGN_ITEMS = {"LOAN_APPLY", "LOAN_CONTRACT", "GUARANTEE_CONTRACT",
              "GUARANTOR_CONSENT", "JOINT_GUARANTEE_PACT", "COOWNER_CONSENT"}

# 情形推断关键词。故意写得保守——宁可留给模型判（进 unresolved），也不要瞎猜。
# 条件项没命中 → **不计入缺件**（必备材料清单 §0 纪律 2）。
SITUATION_HINTS = {
    "已婚":            ["已婚", "结婚", "配偶"],
    "种植":            ["种植", "粮食", "水稻", "小麦", "玉米", "大豆", "大棚",
                        "苗木", "果园", "茶园", "蔬菜", "烟叶", "棉花"],
    "养殖":            ["养殖", "生猪", "母猪", "肉牛", "奶牛", "肉羊", "蛋鸡",
                        "肉鸡", "羊", "禽", "兔", "蜂"],
    # 农业保险单在种植险、养殖险下都可能是必备件，故单独列一个合并情形
    "种养殖":          ["种植", "养殖"],
    "水产":            ["水产", "鱼", "虾", "蟹", "池塘", "投饵"],
    "农机":            ["农机", "拖拉机", "收割机", "播种机", "插秧机", "购置补贴"],
    "农村个体工商户":  ["合作社", "个体工商户", "经营部", "加工厂", "运输", "商店", "门市"],
    "联保":            ["联保", "互保"],
    "整村授信":        ["整村", "信用村"],
    "抵质押担保":      ["抵押", "质押"],
    "保证担保":        ["保证", "担保人", "连带责任"],
    "共有财产":        ["共有", "夫妻共同财产"],
    "受托支付":        ["受托支付"],
    "自主支付":        ["自主支付"],
    "无房照":          ["无房照", "无房产证"],
    "有纳税主体":      ["纳税", "完税", "营业执照"],
}

# 面积一致性容差（必备材料清单 §5.3：偏差 ±5% 内视为一致）
AREA_TOLERANCE = 0.05

LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}


# ── 清单解析 ───────────────────────────────────────────────────────

def parse_checklist(path):
    """从 必备材料清单.md 的代码块里取材料表。

    只认表头精确等于 MATERIAL_COLS 的块——文件里还有阈值说明、
    红线清单等别的代码块，不能误取。
    """
    if not os.path.isfile(path):
        return None, "清单文件不存在：%s" % path
    try:
        text = open(path, "r", encoding="utf-8").read()
    except OSError as e:
        return None, "清单读取失败：%s" % e

    header = "|".join(MATERIAL_COLS)
    items, in_block, block = [], False, []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_block:
                if block and block[0].strip() == header:
                    for raw in block[1:]:
                        parts = [p.strip() for p in raw.split("|")]
                        if len(parts) == len(MATERIAL_COLS):
                            items.append(dict(zip(MATERIAL_COLS, parts)))
                block, in_block = [], False
            else:
                in_block, block = True, []
            continue
        if in_block:
            block.append(line)
    if not items:
        return None, "清单里没有解析到材料表（表头应为 %s）" % header
    return items, None


def condition_of(item):
    """取「必需」列里的条件情形。『必』『选』无条件，『条件:X,Y』返回 [X, Y]。"""
    need = item.get("必需", "")
    if not need.startswith("条件"):
        return []
    _, _, rest = need.partition(":")
    return [t.strip() for t in re.split(r"[,，]", rest) if t.strip()]


def infer_situations(applicant, declared):
    """从申报信息推断情形。declared 是输入里显式给的情形，优先于推断。"""
    hay = " ".join(str(applicant.get(k, "")) for k in
                   ("purpose", "business", "guarantee_type", "marital", "source", "note"))
    out = set(declared or [])
    for name, words in SITUATION_HINTS.items():
        if any(w in hay for w in words):
            out.add(name)
    return out


def required_items(items, stage, situations):
    """按环节取清单：必 + 命中条件的项。

    返回 (必需项, 待定项)。待定项 = 条件没判出来的那些——**不计入缺件**，
    但要报给模型定夺，否则漏判情形就会静默漏报。
    """
    req, pending = [], []
    for it in items:
        if it.get("环节") != stage:
            continue
        conds = condition_of(it)
        if not conds:
            if it.get("必需") == "必":
                req.append(it)
            continue
        if [c for c in conds if c in situations]:
            req.append(it)
        else:
            pending.append({"type": it["type"], "名称": it["名称"], "conds": conds})
    return req, pending


# ── 比对 ───────────────────────────────────────────────────────────

def _norm(v):
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace("　", "")
    return s.lower() or None


def _num(v):
    """转数字。转不出来返回 None——**绝不返回 0**（契约 IF-1.4）。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _by_type(doc):
    out = {}
    for d in doc or []:
        t = d.get("type")
        if t:
            out.setdefault(t, []).append(d)
    return out


def _value_of(doc_item, field):
    """取某材料上的某字段值。顶层字段优先，其次 fields{}。"""
    if field in doc_item and doc_item.get(field) not in (None, ""):
        return doc_item.get(field)
    return (doc_item.get("fields") or {}).get(field)


def check(doc, applicant, stage, items, situations, as_of):
    """核心比对。返回 (metrics, facts, notes)。"""
    applicant = applicant or {}
    notes = []

    # 退化输入（缺失或空表）→ 无法计算，一律 None。
    # 关键：**空材料表不等于「43 项全缺」**。按后者处理会凭空报出
    # 「材料大量缺失（高）」，正是契约 IF-1.4 与 t_degrade 拦的那类回归。
    if not doc:
        return None, None, ["doc 缺失或为空 → 无法比对，coverage: partial"]

    present = _by_type(doc)
    required, pending = required_items(items, stage, situations)
    # 只提醒**确实没交**的待定项——已交的材料不管情形怎么判都不缺件，报出来是噪音
    unresolved = sorted({c for p in pending if p["type"] not in present
                         for c in p["conds"]})

    # ① 缺件
    miss = [it for it in required if it["type"] not in present]
    miss_names = [it["名称"] for it in miss]
    redline_hit = 1 if any(it.get("红线") == "1" for it in miss) else 0

    # ② 过期。expire_date 为空 = 没有有效期，**不是过期**。
    expired = []
    for d in doc:
        exp = _value_of(d, "expire_date")
        if not exp or not as_of:
            continue
        if str(exp) < str(as_of):
            expired.append(d.get("type"))

    # ③ 跨材料矛盾：同一事实对不上，逐处计 1。
    #    任一侧缺值一律跳过——缺值是"没数据"，不是"对不上"。
    inconsis = []
    for field, types, app_key in COMPARE_RULES:
        declared = _norm(applicant.get(app_key))
        for t in sorted(types):
            for d in present.get(t, []):
                got = _norm(_value_of(d, field))
                if got is not None and declared is not None and got != declared:
                    inconsis.append("%s.%s=%s≠申报%s" % (t, field, got, declared))
        # 材料之间互比（≥2 份材料都写了这个字段时）
        seen = [(t, _norm(_value_of(d, field)))
                for t in sorted(types) for d in present.get(t, [])]
        seen = [(t, v) for t, v in seen if v is not None]
        for i in range(len(seen)):
            for j in range(i + 1, len(seen)):
                if seen[i][1] != seen[j][1]:
                    inconsis.append("%s.%s≠%s.%s" % (seen[i][0], field, seen[j][0], field))

    # ④ 身份证核验：在有效期内 且 与申请人一致。缺件 → None（不可判为 0）
    id_valid = None
    for d in present.get("ID_CARD", []):
        exp = _value_of(d, "expire_date")
        name_ok = (_norm(_value_of(d, "name")) == _norm(applicant.get("name"))
                   or _norm(d.get("holder")) == _norm(applicant.get("name")))
        date_ok = (not exp) or (not as_of) or (str(exp) >= str(as_of))
        id_valid = 1 if (name_ok and date_ok) else 0

    # ⑤ 土地权属：面积偏差是否在 ±5% 内。任一侧缺值 → None
    land_right_match = None
    declared_area = _num(applicant.get("declared_area"))
    for d in present.get("LAND_CERT", []):
        got = _num(_value_of(d, "area"))
        if got is None or declared_area in (None, 0):
            continue
        land_right_match = 1 if abs(got - declared_area) / declared_area <= AREA_TOLERANCE else 0

    # ⑥ 签章齐全性。只看**已提交**的应签章材料——没交属于缺件，另行计数。
    sign_complete = None
    for t in sorted(SIGN_ITEMS):
        for d in present.get(t, []):
            f = d.get("fields") or {}
            if f.get("sign_complete") is False:
                sign_complete = 0
            elif f.get("sign_complete") is True:
                sign_complete = sign_complete if sign_complete == 0 else 1
            else:
                need = _num(f.get("seal_required")) or 1.0
                got = _num(f.get("seal_count"))
                if got is not None and got < need:
                    sign_complete = 0
                elif sign_complete is None:
                    sign_complete = 1

    metrics = {
        "doc_miss_cnt": len(miss),
        "doc_expired_cnt": len(expired),
        "doc_inconsist_cnt": len(inconsis),
        "id_valid": id_valid,
        "land_right_match": land_right_match,
        "sign_complete": sign_complete,
    }
    facts = {
        "miss_types": ",".join(miss_names),
        "miss_cnt": len(miss),
        "redline_hit": redline_hit,
        "expired_types": ",".join(sorted(set(t for t in expired if t))),
        "inconsist": ";".join(inconsis),
        "stage": stage,
    }
    if unresolved:
        shown = ",".join(unresolved[:10]) + ("…" if len(unresolved) > 10 else "")
        notes.append("%d 个条件情形未能判定，相关材料未计入缺件：%s"
                     "（如需计入，请在输入里显式给 situations）" % (len(unresolved), shown))
    return metrics, facts, notes


# ── 规则闭包解析与求值（与 analyze.py 同构；本包自包含，不能跨包复用）──

SAFE_CHARS = re.compile(r"^[A-Za-z_0-9\s\.\+\-\*/<>=!&|()]*$")
KEYWORDS = {"true", "false"}


def cond_vars(cond):
    return sorted({t for t in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", cond)
                   if t.lower() not in KEYWORDS})


def parse_closure(path):
    if not os.path.isfile(path):
        return None, "闭包文件不存在：%s" % path
    lines = open(path, "r", encoding="utf-8").read().splitlines()
    rules, header, in_block = [], None, False
    for line in lines:
        if line.strip() == "```":
            in_block = not in_block
            continue
        if not in_block:
            continue
        if header is None and line.startswith("id|"):
            header = line.split("|")
            continue
        if header and not line.startswith("code|"):
            parts = line.split("|")
            if len(parts) == len(header):
                rules.append(dict(zip(header, parts)))
    return rules, None


def eval_cond(cond, env):
    if not SAFE_CHARS.match(cond):
        raise ValueError("cond 含非法字符：%s" % cond)
    expr = cond.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"\btrue\b", "True", expr, flags=re.I)
    expr = re.sub(r"\bfalse\b", "False", expr, flags=re.I)
    scope = {k: v for k, v in env.items() if isinstance(v, (int, float, bool))}
    try:
        return bool(eval(expr, {"__builtins__": {}}, scope))
    except NameError as e:
        raise ValueError("cond 引用了未提供的变量：%s（%s）" % (e, cond))


def evaluate(rules, metrics, facts, stage="贷前"):
    """返回 (L1 行, L2 行, 跳过数)。

    指标为 None 的规则会被跳过——**这正是 None 语义的用武之地**：
    「土地权属没比对成」不该等价于「土地权属不符（高）」。
    """
    env = {k: v for k, v in metrics.items() if v is not None}
    l1, l2, skipped = [], [], 0
    for r in rules:
        try:
            hit = eval_cond(r["cond"], env)
        except ValueError:
            skipped += 1
            continue
        if not hit:
            continue
        ev = ";".join("doc.%s=%s" % (v, env[v])
                      for v in cond_vars(r["cond"]) if v in env)
        l1.append((LEVEL_ORDER.get(r["level"], 9),
                   "F|%s|%s|%s|%s" % (r["id"], r["level"], r["title"], stage)))
        l2.append("%s|ev=%s|basis=%s|conf=%s" % (r["id"], ev, r["basis"], "0.9"))
    l1.sort(key=lambda x: x[0])
    return [x[1] for x in l1], l2, skipped


def facts_line(facts):
    order = ["miss_types", "miss_cnt", "redline_hit", "expired_types", "inconsist", "stage"]
    return "facts@doc|" + "|".join("%s=%s" % (k, facts[k]) for k in order if facts.get(k) not in (None, ""))


# ── selfcheck ─────────────────────────────────────────────────────

def selfcheck(closure, lock):
    rules, err = parse_closure(closure)
    if err:
        print(json.dumps({"coverage": "partial", "reason": err}, ensure_ascii=False))
        return 1
    ids = {r["id"] for r in rules}
    want = set()
    if os.path.isfile(lock):
        for line in open(lock, "r", encoding="utf-8").read().splitlines():
            if line.startswith("rule_ids:"):
                want = {x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()}
    missing = sorted(want - ids)
    ok = not missing
    print(json.dumps({"coverage": "full" if ok else "partial",
                      "rule_count": len(ids), "missing": missing}, ensure_ascii=False))
    return 0 if ok else 1


# ── CLI ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check")
    c.add_argument("--input", required=True)
    c.add_argument("--checklist", default="references/必备材料清单.md")
    c.add_argument("--stage", default="贷前")
    c.add_argument("--out", default=None)

    e = sub.add_parser("evaluate")
    e.add_argument("--facts", required=True)
    e.add_argument("--closure", required=True)
    e.add_argument("--stage", default="贷前")

    s = sub.add_parser("selfcheck")
    s.add_argument("--closure", required=True)
    s.add_argument("--lock", required=True)

    args = ap.parse_args()

    if args.cmd == "check":
        data = {"doc": [], "applicant": {}}
        if os.path.isfile(args.input):
            data = json.loads(open(args.input, "r", encoding="utf-8").read())
        items, err = parse_checklist(args.checklist)
        if err:
            print(json.dumps({"coverage": "partial", "reason": err}, ensure_ascii=False))
            return 0
        applicant = data.get("applicant") or {}
        situations = infer_situations(applicant, data.get("situations"))
        # as_of 缺省取当天：材料时效本来就是"截至某日"的判断。
        # 要可复现的回归测试，请在输入里显式给 as_of。
        as_of = data.get("as_of") or applicant.get("as_of") or \
            datetime.date.today().isoformat()
        metrics, facts, notes = check(data.get("doc"), applicant, args.stage,
                                      items, situations, as_of)
        if metrics is None:
            print(json.dumps({"coverage": "partial", "reason": notes[0],
                              "findings": []}, ensure_ascii=False))
            return 0
        for n in notes:
            print("# %s" % n)
        print(facts_line(facts))
        print("metrics|" + "|".join("%s=%s" % (k, v) for k, v in metrics.items()))
        if args.out:
            open(args.out, "w", encoding="utf-8", newline="\n").write(
                json.dumps({"metrics": metrics, "facts": facts, "situations": sorted(situations),
                            "stage": args.stage, "as_of": as_of},
                           ensure_ascii=False, indent=2))
            print("[sk_doc] 比对结果已写入 %s" % args.out)
        return 0

    if args.cmd == "evaluate":
        data = json.loads(open(args.facts, "r", encoding="utf-8").read())
        rules, err = parse_closure(args.closure)
        if err:
            print(json.dumps({"coverage": "partial", "reason": err,
                              "findings": []}, ensure_ascii=False))
            return 0
        l1, l2, skipped = evaluate(rules, data.get("metrics", {}),
                                   data.get("facts", {}), args.stage)
        print("L1:")
        for x in l1:
            print(x)
        print("L2:")
        for x in l2:
            print(x)
        print(facts_line(data.get("facts", {})))
        if skipped:
            print("# 跳过 %d 条规则（指标为 None，无法判定）" % skipped)
        return 0

    if args.cmd == "selfcheck":
        return selfcheck(args.closure, args.lock)


if __name__ == "__main__":
    sys.exit(main())
