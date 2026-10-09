#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sk_rules 等级合成脚本（契约 D2 / IF-1.1 / IF-1.3）。

把各领域 skill 上报的 findings + 因子，合成为整体风险等级。

为什么放在脚本里而不是让模型算：
  1. 幂等——同输入必同输出（契约测试 C-幂等，模型做不到）
  2. 确定性——定级只有这一处，绝不因措辞漂移而给出两个答案
  3. 省 token——规则表与 findings 都在脚本侧求值，只回传结论行

职责边界（与 skill.md「边界」节一致）：
  - 只消费 findings 与因子，不读原始流水/征信
  - 只求值 dim=x 的跨域规则；领域规则由各 skill 自己求值并上报
  - 只产出 risk{} / points / synthesis，不写最终报告、不判贷后时序预警

用法：
    python synthesize.py synthesize --input input.json --closure references/rules.closure.md
    python synthesize.py synthesize --input input.json --closure ... --json
    python synthesize.py selfcheck --closure references/rules.closure.md --lock references/ruleset.lock

输入 JSON 形状（input.json）：
{
  "findings": [
    "F|R201|高|负债率超限|贷前",
    "F|R102|中|收入稳定性不足|贷前"
  ],
  "features": { "dti": 0.62, "total_debt": 260000, "dim_conflict": 0 },
  "declared_inc": 18000,
  "income_est": 9000,
  "collateral_val": 70000,
  "apply_amount": 100000,
  "stage": "贷前"
}
"""
import os
import re
import sys
import json
import argparse

LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}
LEVELS = {"高", "中", "低"}
CONF_THRESHOLD = 0.6

RULE_COLS = ["id", "dim", "cond", "level", "title", "advice", "basis", "owner"]


# ── 条件求值（与 sk_cash/scripts/analyze.py 保持同一套安全约定） ─────

SAFE_CHARS = re.compile(r"^[A-Za-z_0-9\s\.\+\-\*/<>=!&|()]*$")
KEYWORDS = {"true", "false"}


def cond_vars(cond):
    """抽取 cond 中的变量名（排除数字与布尔字面量）。"""
    return sorted({t for t in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", cond)
                   if t.lower() not in KEYWORDS})


def eval_cond(cond, env):
    """安全求值规则条件，仅允许算术/比较/逻辑运算。

    与 analyze.py 一致：引用到缺失变量会抛 ValueError，由调用方跳过该规则，
    绝不把缺失值臆测为 0——0 往往恰好落在阈值内，会凭空生成风险点。
    """
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


# ── 规则闭包解析（与 analyze.py 相同，从 rules.closure.md 取规则表） ──

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
        if header and line.count("|") >= len(header) - 1 and not line.startswith("code|"):
            parts = line.split("|")
            if len(parts) == len(header):
                rules.append(dict(zip(header, parts)))
    return rules, None


# ── 输入解析与派生量 ────────────────────────────────────────────────

def infer_dim(rid):
    """从规则 id 推断维度（L1 摘要行不含 dim 字段，合成时按需补）。"""
    if rid.startswith("X"):
        return "x"
    if rid.startswith("E"):
        return "ew"
    if rid.startswith("R2"):
        return "cred"
    if rid.startswith("R1"):
        return "cash"
    return "doc"   # R0xx


def parse_findings(lines):
    """解析 L1 摘要行数组：F|id|level|title|stage。"""
    out = []
    for line in lines or []:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 5 or parts[0] != "F":
            continue
        _, rid, level, title, stage = parts
        if level not in LEVELS:
            continue
        out.append({"id": rid, "dim": infer_dim(rid), "level": level,
                    "title": title, "stage": stage})
    return out


def _coerce(v):
    """把字符串因子尽量还原成数值/布尔，供条件求值使用。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s in ("true", "True"):
            return True
        if s in ("false", "False"):
            return False
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return v
    return v


def derive_env(inp):
    """组装求值作用域。派生量任一缺则置 None（跳过相关规则），绝不用 0 填充。"""
    env = {}

    def put(k, v):
        v = _coerce(v)
        if v is not None and v != "":
            env[k] = v

    declared = inp.get("declared_inc")
    income = inp.get("income_est")
    collat = inp.get("collateral_val")
    amount = inp.get("apply_amount")

    put("declared_inc", declared)
    put("income_est", income)
    put("collateral_val", collat)
    put("apply_amount", amount)
    put("total_debt", inp.get("total_debt"))

    # 三方对账：申报收入 / 流水测算收入
    inc_gap_ratio = None
    if declared is not None and income not in (None, 0):
        inc_gap_ratio = float(declared) / float(income)
    put("inc_gap_ratio", inc_gap_ratio)

    # 抵押覆盖：抵押物评估值 / 申请额度
    collat_ratio = None
    if collat is not None and amount not in (None, 0):
        collat_ratio = float(collat) / float(amount)
    put("collat_ratio", collat_ratio)

    # features{} 里带的是各 skill 回传的因子（dti、dim_conflict 等），最后覆盖
    for k, v in (inp.get("features") or {}).items():
        put(k, v)
    return env


def derive_flags(env, findings, inp):
    """派生两个 sk_rules 内部变量（可用 features 显式覆盖）。

    doc_risk_link：是否命中准入类缺件红线。近似口径＝存在「高」等级的 doc 维度
    finding（TODO：待 P3 的《必备材料清单》定稿后收紧到具体红线项）。
    dim_conflict：维度结论冲突。来自上游显式标注（跨 skill 对同一事实判定相反），
    脚本无法仅凭 L1 行可靠自动识别，故优先取 features。
    """
    feats = inp.get("features") or {}
    if "doc_risk_link" in feats:
        doc_risk_link = _coerce(feats["doc_risk_link"])
    else:
        doc_risk_link = int(any(f["dim"] == "doc" and f["level"] == "高"
                                for f in findings))
    dim_conflict = _coerce(feats.get("dim_conflict", 0))
    env["doc_risk_link"] = doc_risk_link
    env["dim_conflict"] = dim_conflict
    return env


# ── 合成核心 ────────────────────────────────────────────────────────

def dedupe_points(items):
    """按 id 去重，同 id 多条取最高等级（高>中>低）。"""
    best = {}
    for it in items:
        rid = it["id"]
        if rid not in best:
            best[rid] = it
        elif LEVEL_ORDER[it["level"]] < LEVEL_ORDER[best[rid]["level"]]:
            best[rid] = it
    return list(best.values())


def evaluate_x(rules, env, stage):
    """求值 dim=x 的跨域规则。返回 (points, skipped)。"""
    points, skipped = [], 0
    for r in rules:
        if r.get("dim") != "x":
            continue
        try:
            hit = eval_cond(r["cond"], env)
        except ValueError:
            skipped += 1
            continue
        if not hit:
            continue
        ev = ";".join("x.%s=%s" % (v, env[v])
                      for v in cond_vars(r["cond"]) if v in env)
        points.append({
            "id": r["id"], "dim": "x", "level": r["level"],
            "title": r["title"], "stage": stage,
            "ev": ev, "basis": r["basis"], "conf": 0.9,
        })
    return points, skipped


def synthesize_level(levels, conflict):
    """契约 D2 的等级合成：任一条高→高；仅中→中；仅低/无→低；冲突→强制高。"""
    if conflict:
        return "高"
    if any(l == "高" for l in levels):
        return "高"
    if any(l == "中" for l in levels):
        return "中"
    return "低"


def rule_text(levels, conflict):
    if conflict:
        return "维度冲突强制高"
    if any(l == "高" for l in levels):
        return "任一条高则整体高"
    if any(l == "中" for l in levels):
        return "仅中无高则整体中"
    return "仅低或无发现则整体低"


def synthesize(inp, rules, stage=None):
    """主入口：汇总 findings + 求值跨域规则 → 合成整体等级。幂等纯函数。"""
    stage = stage or inp.get("stage") or "贷前"

    findings = parse_findings(inp.get("findings"))
    env = derive_env(inp)
    env = derive_flags(env, findings, inp)

    x_points, skipped = evaluate_x(rules, env, stage)

    # 合并（领域 findings + 跨域 points），按 id 去重取最高等级
    points = dedupe_points(findings + x_points)
    points.sort(key=lambda p: (LEVEL_ORDER.get(p["level"], 9), p["id"]))

    levels = [p["level"] for p in points]
    conflict = bool(env.get("dim_conflict"))
    level = synthesize_level(levels, conflict)

    high_cnt = sum(1 for l in levels if l == "高")
    mid_cnt = sum(1 for l in levels if l == "中")
    low_cnt = sum(1 for l in levels if l == "低")

    # pending：任一命中项 conf<0.6 需人工复核；上游也可显式提示
    pending = 1 if inp.get("pending_hint") else 0
    if any(p.get("conf") is not None and p["conf"] < CONF_THRESHOLD
           for p in points):
        pending = 1

    return {
        "risk": {"level": level, "pending": pending},
        "points": points,
        "x_points": x_points,
        "synthesis": {
            "high_cnt": high_cnt, "mid_cnt": mid_cnt, "low_cnt": low_cnt,
            "conflict": int(conflict), "rule": rule_text(levels, conflict),
            "skipped": skipped,
        },
    }


# ── 输出（对齐 skill.md 的双层文本格式） ─────────────────────────────

def render_text(result):
    lines = []
    r = result["risk"]
    lines.append("risk|level=%s|pending=%d" % (r["level"], r["pending"]))
    for p in result["points"]:
        lines.append("points|%s|%s|%s" % (p["id"], p["level"], p["title"]))
    lines.append("---")
    # 证据明细只回传 sk_rules 自己算出的跨域点；领域 finding 的证据留在源 skill
    for p in result["x_points"]:
        lines.append("%s|ev=%s|basis=%s|conf=%s"
                     % (p["id"], p["ev"], p["basis"], p["conf"]))
    s = result["synthesis"]
    lines.append("synthesis|high_cnt=%d|mid_cnt=%d|low_cnt=%d|conflict=%d|rule=%s"
                 % (s["high_cnt"], s["mid_cnt"], s["low_cnt"],
                    s["conflict"], s["rule"]))
    return "\n".join(lines)


# ── selfcheck（与 analyze.py 一致的闭包自检） ────────────────────────

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
    print(json.dumps({
        "coverage": "full" if ok else "partial",
        "rule_count": len(ids),
        "missing": missing,
    }, ensure_ascii=False))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synthesize")
    s.add_argument("--input", required=True)
    s.add_argument("--closure", required=True)
    s.add_argument("--json", action="store_true")

    c = sub.add_parser("selfcheck")
    c.add_argument("--closure", required=True)
    c.add_argument("--lock", required=True)

    args = ap.parse_args()

    if args.cmd == "synthesize":
        data = json.loads(open(args.input, "r", encoding="utf-8").read())
        rules, err = parse_closure(args.closure)
        if err:
            # 降级协议：不崩溃，标注 partial（契约 IF-5.5）
            print(json.dumps({"coverage": "partial", "reason": err,
                              "risk": {}}, ensure_ascii=False))
            return 0
        result = synthesize(data, rules)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(render_text(result))
        return 0

    if args.cmd == "selfcheck":
        return selfcheck(args.closure, args.lock)


if __name__ == "__main__":
    sys.exit(main())
