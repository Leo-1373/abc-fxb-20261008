#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sk_cred 征信聚合与规则求值（契约 IF-1.3 / IF-5.5）。

四个子命令：
  aggregate  credit{} + debt[] + declared_inc → 短码指标（明细不进模型上下文）
  evaluate   指标 + 规则闭包 → L1 摘要 / L2 证据明细 / coverage / factors
  selfcheck  闭包与 ruleset.lock 一致性（缺规则即标注 coverage: partial）
  selftest   内置回归用例（含"算不出来必须填 None"的守卫）

为什么确定性计算必须离开模型（docs/交付说明.md 规矩三）：
  dti、multi_lend、负债结构都是加减法。让模型算会算错、慢、且花钱；更致命的是
  同一份数据两次调用可能给出两个答案，评测就不可复现。脚本天然幂等。

指标缺失约定（契约 IF-1.4，本项目真实踩过坑）：
  算不出来的指标一律置 None，**绝不用 0 顶替**。0 的两种危害都能在本包复现：
    · 凭空报风险："越小越危险"的规则里 0 恰好命中。sk_cash 的 `bal_min<=0`
      （账户曾透支）就是这样在空输入下报出高风险的。
    · 静默漏报："越大越危险"的规则里 0 永远不命中。`card_util`、`guarantee_bal`、
      `overdue_cur` 若默认 0，风险点会凭空消失且**不报错**——本包主要防这一种。
  引用 None 的规则会被跳过，并在输出标注 `coverage: partial` + 缺失清单。

环境约束：一律用 `python`（非 python3），中文输出加 PYTHONIOENCODING=utf-8。

用法：
    python cred.py aggregate --input credit.json --out metrics.json
    python cred.py evaluate  --metrics metrics.json --closure references/rules.closure.md --stage 贷前
    python cred.py selfcheck --closure references/rules.closure.md --lock references/ruleset.lock
    python cred.py selftest
"""
import os
import re
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)                       # skills/sk_cred
ROOT = os.path.dirname(os.path.dirname(SKILL_DIR))       # 仓库根（源码期）

LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}
LEVELS = {"高", "中", "低"}
# L2 置信度：脚本命中的规则是确定性的，给 0.9（与 sk_cash/analyze.py 保持一致）
CONF = "0.9"

# 负债结构：抵押/质押视为有硬担保，其余（信用、保证）计入无抵押敞口。
# 先判抵押再判信用，避免"抵押担保"被"担保"关键字误判成无抵押。
COLLATERAL_KEYS = ("抵押", "质押", "mortgage", "pledge", "collateral")
UNSECURED_KEYS = ("信用", "保证", "credit", "guarantee", "unsecured")
SHORT_TERM_MONTHS = 12

# credit{} 直取标量。缺键或不可解析 → None（绝不默认 0）。
CREDIT_INTS = ["overdue_24m", "overdue_max_days", "overdue_cur", "query_3m",
               "query_self_3m", "card_cnt", "credit_hist_len", "new_loan_3m",
               "new_org_6m", "guarantee_cnt", "extension_cnt"]
CREDIT_MONEY = ["guarantee_bal", "new_loan_amt_3m", "settled_amt_recent",
                "co_borrower_debt"]
CREDIT_FLOATS = ["card_util"]
CREDIT_FLAGS = ["settled_recent", "bad_debt_flag", "dishonest_flag", "lawsuit_flag",
                "guarantee_overdue_flag", "co_borrower_dishonest", "five_level_bad",
                "semi_card_overdraft", "refinance_flag"]


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ── 取值工具：三态（数值 / 布尔 / None） ────────────────────────────

def _num(v):
    """转 float。空、非法、NaN、布尔一律 → None。

    布尔必须挡掉：JSON 里 `"balance": true` 会被 float() 变成 1.0，
    悄悄混进金额合计，这类错误不报错但会让 dti 失真。
    """
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN → None（NaN 参与比较恒为 False，会静默改判）


def _int(v):
    f = _num(v)
    return None if f is None else int(f)


def _flag(v):
    """三态布尔：True / False / None（未知）。"""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "是", "有"):
        return True
    if s in ("0", "false", "no", "n", "否", "无"):
        return False
    return None


def _is_unsecured(debt_type):
    s = str(debt_type or "").strip().lower()
    if any(k in s for k in COLLATERAL_KEYS):
        return False
    return any(k in s for k in UNSECURED_KEYS)


# ── aggregate ─────────────────────────────────────────────────────

def aggregate(credit, debt, declared_inc=None):
    """credit{} + debt[] + declared_inc → (metrics, meta, missing[])。

    `missing` 只记**真正的数据缺失**；"本来就没有" 不算缺失：
      · `debt: []`（确实无负债）→ multi_lend=0、dti=0.0，**不是** None；
      · `debt` 键缺失（没给明细）→ multi_lend/dti=None，记一条缺失。
    区分这两者是 dti 是否可信的关键：把"没给数据"当成"没有负债"，
    会把高负债客户判成低风险。
    """
    c = credit if isinstance(credit, dict) else {}
    declared = _num(declared_inc)
    missing = []

    m = {}
    for k in CREDIT_INTS:
        m[k] = _int(c.get(k))
        if m[k] is None:
            missing.append(k)
    for k in CREDIT_MONEY:
        m[k] = _num(c.get(k))
        if m[k] is None:
            missing.append(k)
    for k in CREDIT_FLOATS:
        m[k] = _num(c.get(k))
        if m[k] is None:
            missing.append(k)
    for k in CREDIT_FLAGS:
        m[k] = _flag(c.get(k))
        if m[k] is None:
            missing.append(k)

    monthly_pay = debt_balance = None
    n_debt = None
    m["multi_lend"] = None
    m["dti"] = None
    m["unsecured_ratio"] = None
    m["short_term_ratio"] = None

    # declared_inc 是 dti 的分母，也是若干跨域比率的基准。不可用时只记一次缺失。
    declared_ok = declared is not None and declared > 0
    if not declared_ok:
        missing.append("declared_inc")

    if not isinstance(debt, list):
        # 未提供，或形状不是数组（如误传对象）→ 一律算"在贷明细不可用"。
        # **绝不能当成"无负债"**：那会让 multi_lend=0、dti=0.0，
        # 把高负债客户报成低风险，而且**不报错**——正是本项目最要防的"静默给出错误答案"。
        missing.append("debt[]")
    else:
        items = [d for d in debt if isinstance(d, dict)]
        if len(items) != len(debt):
            # 有明细项不是对象：丢弃，但必须标记，否则求和会静默偏小
            missing.append("debt[].item")
        n_debt = len(items)
        orgs, pays = set(), []
        bal_total = unsec_amt = short_amt = 0.0
        pay_missing = bal_missing = months_known = org_missing = False

        for d in items:
            org = str(d.get("org") or "").strip()
            if org:
                orgs.add(org)
            else:
                # 机构名缺失 → 多头家数会静默低估（8 笔全缺就成了 0 家 = 无多头风险）
                org_missing = True
            bal = _num(d.get("balance"))
            pay = _num(d.get("monthly_pay"))
            if bal is None:
                bal_missing = True
                bal = 0.0
            if pay is None:
                pay_missing = True
                pay = 0.0
            pays.append(pay)
            bal_total += bal
            if _is_unsecured(d.get("type")):
                unsec_amt += bal
            rm = _num(d.get("remain_months"))
            if rm is not None:
                months_known = True
                if rm <= SHORT_TERM_MONTHS:
                    short_amt += bal

        # 部分和不等于真实总额 → 有缺失就给 None，绝不给一个"看起来更低"的数
        monthly_pay = None if pay_missing else round(sum(pays), 2)
        debt_balance = None if bal_missing else round(bal_total, 2)

        # 机构名不全时多头家数会低估 → None（宁可不报，不可报 0）
        if org_missing:
            missing.append("debt[].org")
        else:
            m["multi_lend"] = len(orgs)

        if bal_missing:
            missing.append("debt[].balance")
        if pay_missing:
            missing.append("debt[].monthly_pay")

        # 分母不完整时结构比率会系统性偏低 → 漏报，故宁可不报（保持 None）+ 标 partial
        if bal_total > 0 and not bal_missing:
            m["unsecured_ratio"] = round(unsec_amt / bal_total, 4)
            if months_known:
                m["short_term_ratio"] = round(short_amt / bal_total, 4)
            else:
                missing.append("debt[].remain_months")
        # bal_total == 0：确实无在贷余额，比率无定义但不算缺失（N/A）

        if declared_ok and not pay_missing:
            m["dti"] = round(monthly_pay / declared, 4)

    # total_debt：契约 IF-4 的 upstream 声明为 `total_debt <- sk_cred`，
    # 而 sk_cred/skill.md 的边界又写"不算 total_debt"。两处口径不一致（待金宇桐裁决）。
    # 本实现取**超集**：在贷余额、对外担保、以及二者之和都回传，
    # 这样无论裁决为"sk_cred 算"还是"sk_rules 算"，下游都不缺数据。
    # **只提供数值，不判"总负债超年收入"**——判定始终归 sk_rules 的 X003。
    # 任一加数不可得 → None（绝不用已知的那一半顶替，否则会低估总负债）。
    if debt_balance is not None and m["guarantee_bal"] is not None:
        total_debt = round(debt_balance + m["guarantee_bal"], 2)
    else:
        total_debt = None
        missing.append("total_debt")

    meta = {
        "monthly_pay": monthly_pay,
        "debt_balance": debt_balance,
        "total_debt": total_debt,
        "n_debt": n_debt,
        "declared_inc": declared,
    }
    return m, meta, missing


# ── 规则闭包解析与求值 ────────────────────────────────────────────

SAFE_CHARS = re.compile(r"^[A-Za-z_0-9\s\.\+\-\*/<>=!&|()]*$")
KEYWORDS = {"true", "false"}


def cond_vars(cond):
    """抽取 cond 中的变量名。本脚本随包分发，不能 import 构建期 ruleslib。"""
    return sorted({t for t in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", cond)
                   if t.lower() not in KEYWORDS})


def parse_closure(path):
    """解析 rules.closure.md → (rules, thresholds, var_types, 错误)。

    闭包里有三段表（规则 / 变量字典 / 环节阈值），都用 ``` 围栏分隔，
    表头分别是 `id|`、`code|`、`变量|`，据此分派。
    """
    if not os.path.isfile(path):
        return None, {}, {}, "闭包文件不存在：%s" % path
    rules, thresholds, var_types = [], {}, {}
    header, in_block = None, False
    for line in read_text(path).splitlines():
        if line.strip() == "```":
            in_block = not in_block
            header = None
            continue
        if not in_block:
            continue
        if line.startswith("id|") or line.startswith("code|") or line.startswith("变量|"):
            header = line.split("|")
            continue
        if header is None or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) != len(header):
            continue
        if header[0] == "id":
            rules.append(dict(zip(header, parts)))
        elif header[0] == "code":
            var_types[parts[0]] = parts[1]
        elif header[0] == "变量":
            thresholds[parts[0]] = dict(zip(header[1:], parts[1:]))
    return rules, thresholds, var_types, None


def eval_cond(cond, env):
    """安全求值规则条件。仅允许算术/比较/逻辑运算。"""
    if not SAFE_CHARS.match(cond):
        raise ValueError("cond 含非法字符：%s" % cond)
    expr = cond.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"\btrue\b", "True", expr, flags=re.I)
    expr = re.sub(r"\bfalse\b", "False", expr, flags=re.I)
    return bool(eval(expr, {"__builtins__": {}}, dict(env)))


def evaluate(rules, metrics, meta, stage="贷前", thresholds=None):
    """→ (L1[], L2[], skipped, blocked_vars[])。

    先检查变量是否齐备，再求值——这样"因缺数据而跳过"的变量能被收集起来，
    用于输出 `coverage: partial` 的缺失清单。静默跳过是契约禁止的（IF-5.5）。
    """
    env = {k: v for k, v in metrics.items() if isinstance(v, (int, float, bool))}
    env.update({k: v for k, v in meta.items() if isinstance(v, (int, float, bool))})

    l1, l2, skipped, blocked = [], [], 0, set()
    for r in rules:
        vs = cond_vars(r["cond"])
        absent = [v for v in vs if v not in env]
        if absent:
            skipped += 1
            blocked.update(absent)
            continue
        try:
            hit = eval_cond(r["cond"], env)
        except (ValueError, SyntaxError):
            skipped += 1
            continue
        if not hit:
            continue
        ev = ";".join("%s.%s=%s" % (r["dim"], v, env[v]) for v in vs if v in env)
        l1.append((LEVEL_ORDER.get(r["level"], 9),
                   "F|%s|%s|%s|%s" % (r["id"], r["level"], r["title"], stage)))
        l2.append("%s|ev=%s|basis=%s|conf=%s" % (r["id"], ev, r["basis"], CONF))
    l1.sort(key=lambda x: x[0])                       # 契约：按等级降序（高→中→低）
    return [x[1] for x in l1], l2, skipped, sorted(blocked)


def coverage_line(missing, blocked):
    bad = sorted(set(missing) | set(blocked))
    if not bad:
        return "coverage|full"
    return "coverage|partial|missing=%s" % ",".join(bad)


def factors_line(metrics, meta, thresholds, stage):
    """回传 sk_rules 判 X003（总负债）等跨域规则所需的因子，单行制表分隔。

    dti 阈值一并回传：规则库按**贷中**口径编码，贷前从严/贷后放宽由 sk_rules
    结合本行与 dict.yaml 的 thresholds 决定，本 skill 不改写规则。
    """
    keys = ["dti", "multi_lend", "monthly_pay", "debt_balance", "total_debt",
            "unsecured_ratio", "short_term_ratio", "guarantee_bal", "guarantee_cnt",
            "co_borrower_debt", "overdue_cur", "new_loan_3m", "new_org_6m"]
    parts = ["factors"]
    for k in keys:
        v = metrics.get(k)
        if v is None:
            v = meta.get(k)
        if v is not None:
            parts.append("%s=%s" % (k, v))
    parts.append("stage=%s" % stage)
    t = (thresholds or {}).get("dti", {}).get(stage)
    if t and t != "-":
        parts.append("dti_threshold=%s" % t)
    return "|".join(parts)


# ── selfcheck（契约 IF-5.3） ───────────────────────────────────────

def selfcheck(closure, lock):
    rules, _, _, err = parse_closure(closure)
    if err:
        print(json.dumps({"coverage": "partial", "reason": err}, ensure_ascii=False))
        return 1
    ids = {r["id"] for r in rules}
    want = set()
    if os.path.isfile(lock):
        for line in read_text(lock).splitlines():
            if line.startswith("rule_ids:"):
                want = {x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()}
    missing = sorted(want - ids)
    print(json.dumps({
        "coverage": "full" if not missing else "partial",
        "rule_count": len(ids),
        "missing": missing,
    }, ensure_ascii=False))
    return 0 if not missing else 1


# ── selftest ──────────────────────────────────────────────────────

def _base_credit(**over):
    """干净农户的征信底稿：字段给全，避免 None 噪声干扰断言。"""
    c = {
        "overdue_24m": 0, "overdue_max_days": 0, "overdue_cur": 0,
        "query_3m": 0, "query_self_3m": 0, "card_util": 0.0, "card_cnt": 0,
        "credit_hist_len": 60, "settled_recent": False, "guarantee_bal": 0,
        "guarantee_cnt": 0, "new_loan_3m": 0, "new_loan_amt_3m": 0,
        "settled_amt_recent": 0, "co_borrower_debt": 0, "bad_debt_flag": False,
        "dishonest_flag": False, "lawsuit_flag": False, "guarantee_overdue_flag": False,
        "co_borrower_dishonest": False, "five_level_bad": False, "extension_cnt": 0,
        "semi_card_overdraft": False, "new_org_6m": 0, "refinance_flag": False,
    }
    c.update(over)
    return c


# 证据指针校验：`<源>.<定位>=<值>`，多段用 `;` 连接（契约 IF-1.2 / IF-1.3）。
# 注意：eval/contract_tests.py 的 EV_RE 少了 `;` 分段与 `-`/`.` 定位字符，
# 无法匹配 contracts.md 自己举的 `txn.2025-03.amt_cv=0.62;txn.gap_cnt=4`。
# 本脚本按契约原文实现，故比那份测试更严（见交付说明中"契约测试待修"一条）。
EV_RE = re.compile(r"^[a-z_]+(\.[A-Za-z_0-9_.\-一-龥]+=[^\s;]+)"
                   r"(;[a-z_]+(\.[A-Za-z_0-9_.\-一-龥]+=[^\s;]+))*$")
ID_RE = re.compile(r"^R\d+$")


def _ids(l1):
    return [x.split("|")[1] for x in l1]


def selftest(closure=None):
    checks, fails = [], []

    def ck(cond, msg):
        checks.append(msg)
        if not cond:
            fails.append(msg)

    closure = closure or default_closure()
    rules, thresholds, var_types, err = parse_closure(closure)
    if err:
        print("[cred.selftest] 找不到闭包，先跑 `python build/pack.py`：%s" % err)
        return 1

    # ── 静态检查：本维度规则表的格式与自洽 ──
    mine = [r for r in rules if r["dim"] == "cred"]
    ck(len(mine) >= 40, "cred 规则数应 >=40，实际 %d" % len(mine))
    seen_id, seen_cond = set(), {}
    for r in mine:
        ck(r["level"] in LEVELS, "%s 等级非法：%s" % (r["id"], r["level"]))
        ck(len(r["title"]) <= 12, "%s title 超 12 字：%s" % (r["id"], r["title"]))
        ck(len(r["advice"]) <= 16, "%s advice 超 16 字：%s" % (r["id"], r["advice"]))
        ck(ID_RE.match(r["id"]) is not None, "%s id 格式非法" % r["id"])
        ck(r["id"] not in seen_id, "%s id 重复" % r["id"])
        seen_id.add(r["id"])
        ck(r["cond"] not in seen_cond,
           "%s 与 %s 条件完全相同（重复规则）：%s"
           % (r["id"], seen_cond.get(r["cond"]), r["cond"]))
        seen_cond[r["cond"]] = r["id"]
        for v in cond_vars(r["cond"]):
            ck(v in var_types, "%s 用了闭包变量字典外的变量：%s" % (r["id"], v))

    def run(credit, debt, declared, stage="贷前"):
        m, meta, missing = aggregate(credit, debt, declared)
        l1, l2, skipped, blocked = evaluate(rules, m, meta, stage, thresholds)
        out = {"metrics": m, "meta": meta, "l1": l1, "l2": l2, "missing": missing,
               "blocked": blocked, "cov": coverage_line(missing, blocked)}
        # 契约 IF-1 schema：每次运行都断言，避免"某个用例格式跑偏"
        ck(len(_ids(l1)) == len(set(_ids(l1))), "L1 存在重复 id：%s" % l1)
        for line in l1:
            p = line.split("|")
            ck(len(p) == 5, "L1 字段数应为 5：%s" % line)
            ck(p[2] in LEVELS, "等级非法：%s" % line)
            ck(len(p[3]) <= 12, "title 超 12 字：%s" % line)
            ck(p[4] in ("贷前", "贷中", "贷后", "跨环节"), "环节非法：%s" % line)
        order = [LEVEL_ORDER[x.split("|")[2]] for x in l1]
        ck(order == sorted(order), "L1 未按等级降序：%s" % order)
        ck(set(_ids(l1)) == {x.split("|")[0] for x in l2},
           "L1/L2 的 id 集合不一致")
        for line in l2:
            ev = ""
            for seg in line.split("|"):
                if seg.startswith("ev="):
                    ev = seg[3:]
            ck(bool(EV_RE.match(ev)), "证据非指针格式：%s" % line)
        return out

    # ── 用例 A：正常农户（抵押+信用各一笔），只应命中多头迹象 ──
    a = run(_base_credit(),
            [{"org": "A银行", "type": "抵押", "balance": 100000, "monthly_pay": 2000,
              "remain_months": 24},
             {"org": "B银行", "type": "信用", "balance": 20000, "monthly_pay": 1000,
              "remain_months": 6}],
            8000)
    ck(a["metrics"]["dti"] == 0.375, "dti 应为 0.375，实际 %s" % a["metrics"]["dti"])
    ck(a["metrics"]["multi_lend"] == 2, "multi_lend 应为 2，实际 %s" % a["metrics"]["multi_lend"])
    ck(a["metrics"]["unsecured_ratio"] == 0.1667,
       "unsecured_ratio 应为 0.1667，实际 %s" % a["metrics"]["unsecured_ratio"])
    ck(a["metrics"]["short_term_ratio"] == 0.1667,
       "short_term_ratio 应为 0.1667，实际 %s" % a["metrics"]["short_term_ratio"])
    ck(_ids(a["l1"]) == ["R204"], "用例 A 应只命中 R204，实际 %s" % a["l1"])
    ck(a["cov"] == "coverage|full", "用例 A 应为 coverage|full，实际 %s" % a["cov"])

    # ── 用例 B：空输入（本包最关键的一条守卫）──
    # 无数据时必须**零风险点**，且所有指标为 None——绝不允许被 0 顶替后报出风险。
    b = run(None, None, None)
    ck(b["l1"] == [], "空输入不得报出任何风险点，实际 %s" % b["l1"])
    for k in ("dti", "multi_lend", "unsecured_ratio", "short_term_ratio",
              "guarantee_bal", "overdue_cur", "card_util", "settled_recent"):
        ck(b["metrics"][k] is None, "空输入下 %s 应为 None，实际 %r" % (k, b["metrics"][k]))
    ck(b["cov"].startswith("coverage|partial"), "空输入应标注 partial，实际 %s" % b["cov"])

    # ── 用例 C：缺 declared_inc → dti 不可算，dti 类规则必须跳过（而非按 0 判定）──
    c = run(_base_credit(),
            [{"org": "A银行", "type": "信用", "balance": 50000, "monthly_pay": 3000,
              "remain_months": 12}],
            None)
    ck(c["metrics"]["dti"] is None, "缺 declared_inc 时 dti 应为 None，实际 %s" % c["metrics"]["dti"])
    ck("R201" not in _ids(c["l1"]) and "R202" not in _ids(c["l1"]),
       "dti 不可算时不得命中 R201/R202：%s" % c["l1"])
    ck("R224" in _ids(c["l1"]), "全额信用类负债应命中 R224：%s" % c["l1"])
    ck("declared_inc" in c["missing"], "缺失清单应含 declared_inc：%s" % c["missing"])

    # ── 用例 D：准入红线（当前逾期 / 呆账 / 失信）──
    d = run({"overdue_cur": 1, "bad_debt_flag": True, "dishonest_flag": True}, [], 8000)
    ck(set(_ids(d["l1"])) == {"R215", "R216", "R217"},
       "红线用例应命中 R215/R216/R217，实际 %s" % d["l1"])
    ck(all(x.split("|")[2] == "高" for x in d["l1"]), "红线规则应全为高：%s" % d["l1"])

    # ── 用例 E：以贷养贷（结清金额 > 近期新增额）──
    e = run(_base_credit(new_loan_3m=1, new_loan_amt_3m=10000, settled_amt_recent=30000),
            [], 8000)
    ck("R231" in _ids(e["l1"]), "用例 E 应命中 R231：%s" % e["l1"])
    ck("R222" in _ids(e["l1"]), "用例 E 应命中 R222：%s" % e["l1"])

    # ── 用例 F：幂等（契约 C-幂等）──
    f1 = run(_base_credit(), [{"org": "A银行", "type": "信用", "balance": 1000,
                               "monthly_pay": 100, "remain_months": 6}], 8000)
    f2 = run(_base_credit(), [{"org": "A银行", "type": "信用", "balance": 1000,
                               "monthly_pay": 100, "remain_months": 6}], 8000)
    ck(f1["l1"] == f2["l1"] and f1["l2"] == f2["l2"], "非幂等：两次运行结果不同")

    # ── 用例 G：debt 缺失 ≠ 无负债（数据缺失不得当成 0）──
    g = run(_base_credit(), None, 8000)
    ck(g["metrics"]["multi_lend"] is None, "debt 缺失时 multi_lend 应为 None，实际 %s" % g["metrics"]["multi_lend"])
    ck(g["metrics"]["dti"] is None, "debt 缺失时 dti 应为 None，实际 %s" % g["metrics"]["dti"])
    h = run(_base_credit(), [], 8000)
    ck(h["metrics"]["multi_lend"] == 0, "debt=[] 时 multi_lend 应为 0，实际 %s" % h["metrics"]["multi_lend"])
    ck(h["metrics"]["dti"] == 0.0, "debt=[] 时 dti 应为 0.0，实际 %s" % h["metrics"]["dti"])

    # ── 用例 I：异常输入不崩（退化鲁棒）──
    try:
        run({"card_util": "abc", "overdue_24m": None},
            [{"org": "X", "type": "抵押", "balance": "abc", "monthly_pay": "abc"}], "abc")
        ck(True, "异常输入未抛异常")
    except Exception as ex:
        ck(False, "异常输入抛异常：%s: %s" % (type(ex).__name__, ex))

    # ══ 任务2 专项：负债测算的加法正确性（分工.md：客户可能借了 8 家机构的钱，
    #    让 AI 一家家加会加错，所以必须由脚本算并留下可复算的测试）══

    # ── 用例 J：8 笔 / 6 家机构，手算比对每一个数 ──
    #   orgs = A A B B C D E F → 6 家（同一家机构多笔只算 1 家）
    #   balance = 1000..8000 → 合计 36000 ； monthly_pay = 100..800 → 合计 3600
    #   dti = 3600 / 10000 = 0.36 ； total_debt = 36000 + 0（底稿 guarantee_bal=0）
    eight = [{"org": org, "type": "信用", "balance": 1000 * (i + 1),
              "monthly_pay": 100 * (i + 1), "remain_months": 12}
             for i, org in enumerate(["A", "A", "B", "B", "C", "D", "E", "F"])]
    j = run(_base_credit(), eight, 10000)
    ck(j["meta"]["debt_balance"] == 36000.0,
       "用例 J 负债合计应为 36000，实际 %s" % j["meta"]["debt_balance"])
    ck(j["meta"]["monthly_pay"] == 3600.0,
       "用例 J 月供合计应为 3600，实际 %s" % j["meta"]["monthly_pay"])
    ck(j["metrics"]["multi_lend"] == 6,
       "用例 J 8 笔落在 6 家机构，多头家数应为 6，实际 %s" % j["metrics"]["multi_lend"])
    ck(j["metrics"]["dti"] == 0.36,
       "用例 J dti 应为 0.36，实际 %s" % j["metrics"]["dti"])
    ck(j["meta"]["total_debt"] == 36000.0,
       "用例 J 总负债应为 36000，实际 %s" % j["meta"]["total_debt"])
    ck("R203" in _ids(j["l1"]), "用例 J 6 家机构应命中 R203（多头借贷）：%s" % j["l1"])

    # ── 用例 K：明细缺 balance → 总额必须 None（部分和不等于总额）──
    k = run(_base_credit(),
            [{"org": "A", "type": "信用", "balance": 1000, "monthly_pay": 100},
             {"org": "B", "type": "信用", "monthly_pay": 200}], 8000)
    ck(k["meta"]["debt_balance"] is None,
       "用例 K 有明细缺 balance 时 debt_balance 必须为 None，实际 %s" % k["meta"]["debt_balance"])
    ck(k["meta"]["total_debt"] is None, "用例 K total_debt 必须为 None")
    ck(k["meta"]["monthly_pay"] == 300.0, "用例 K 月供齐全，应为 300")
    ck("debt[].balance" in k["missing"], "用例 K 缺失清单应含 debt[].balance：%s" % k["missing"])

    # ── 用例 L：明细缺 org → 多头家数必须 None（否则静默低估）──
    l = run(_base_credit(),
            [{"type": "信用", "balance": 1000, "monthly_pay": 100},
             {"org": "B", "type": "信用", "balance": 1000, "monthly_pay": 100}], 8000)
    ck(l["metrics"]["multi_lend"] is None,
       "用例 L 有明细缺 org 时 multi_lend 必须为 None（不能算成 1），实际 %s"
       % l["metrics"]["multi_lend"])
    ck("debt[].org" in l["missing"] or "debt[].org（机构名缺失）" in l["missing"],
       "用例 L 缺失清单应含 debt[].org：%s" % l["missing"])

    # ── 用例 M：debt 形状非法（误传对象）→ 不得当成"无负债" ──
    mc = run(_base_credit(), {"org": "A", "balance": 1000}, 8000)
    ck(mc["metrics"]["multi_lend"] is None,
       "用例 M debt 不是数组时 multi_lend 必须为 None（不能是 0），实际 %s"
       % mc["metrics"]["multi_lend"])
    ck(mc["metrics"]["dti"] is None,
       "用例 M debt 形状非法时 dti 必须为 None（不能是 0.0），实际 %s" % mc["metrics"]["dti"])
    ck(mc["meta"]["debt_balance"] is None, "用例 M debt_balance 必须为 None")

    # ── 用例 N：明细项不是对象 → 丢弃但必须标记 ──
    nc = run(_base_credit(),
             [{"org": "A", "type": "信用", "balance": 1000, "monthly_pay": 100}, "坏数据"], 8000)
    ck("debt[].item" in nc["missing"], "用例 N 应标记 debt[].item：%s" % nc["missing"])
    ck(nc["meta"]["debt_balance"] == 1000.0, "用例 N 有效项应照常求和（1000）")

    # ── 用例 O：取值工具的三态纪律（布尔/NaN 不得混进金额）──
    ck(_num(True) is None, "_num(True) 必须为 None——布尔不能当 1 混进金额")
    ck(_num("abc") is None, "_num('abc') 必须为 None")
    ck(_num(float("nan")) is None, "NaN 必须为 None（NaN 参与比较恒为 False，会静默改判）")
    ck(_num("0") == 0.0, "_num('0') 应为 0.0——真实零值必须保留")
    ck(_flag("是") is True and _flag("否") is False, "_flag 应支持中文真值")
    ck(_flag(None) is None, "_flag(None) 应为 None（未知，不是 False）")
    ck(_flag(0) is False, "_flag(0) 应为 False")

    for m in fails:
        print("  ✗ %s" % m)
    if fails:
        print("\n[cred.selftest] 失败：%d/%d 项断言不通过" % (len(fails), len(checks)))
        return 1
    print("[cred.selftest] 通过：%d 项断言（cred 规则 %d 条）" % (len(checks), len(mine)))
    return 0


def default_closure():
    """闭包位置：打包后在同包 references/，源码期在 build/stage/。"""
    cands = [
        os.path.join(SKILL_DIR, "references", "rules.closure.md"),
        os.path.join(ROOT, "build", "stage", "sk_cred", "references", "rules.closure.md"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[1]


def default_lock():
    cands = [
        os.path.join(SKILL_DIR, "references", "ruleset.lock"),
        os.path.join(ROOT, "build", "stage", "sk_cred", "references", "ruleset.lock"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[1]


# ── main ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("aggregate")
    a.add_argument("--input", required=True)
    a.add_argument("--out", default=None)

    e = sub.add_parser("evaluate")
    e.add_argument("--metrics", required=True)
    e.add_argument("--closure", default=None)
    e.add_argument("--stage", default="贷前")

    s = sub.add_parser("selfcheck")
    s.add_argument("--closure", default=None)
    s.add_argument("--lock", default=None)

    t = sub.add_parser("selftest")
    t.add_argument("--closure", default=None)

    args = ap.parse_args()

    if args.cmd == "aggregate":
        data = json.loads(read_text(args.input))
        m, meta, missing = aggregate(data.get("credit"), data.get("debt"),
                                     data.get("declared_inc"))
        out = {
            "metrics": m,
            "meta": meta,
            "coverage": {"status": "full" if not missing else "partial",
                         "missing": sorted(set(missing))},
        }
        txt = json.dumps(out, ensure_ascii=False, indent=2)
        if args.out:
            write_text(args.out, txt)
            print("[sk_cred] 指标已写入 %s（在贷 %s 笔）" % (args.out, meta["n_debt"]))
        else:
            print(txt)
        return 0

    if args.cmd == "evaluate":
        data = json.loads(read_text(args.metrics))
        closure = args.closure or default_closure()
        rules, thresholds, _, err = parse_closure(closure)
        if err:
            # 降级协议：不崩溃，标注 partial（契约 IF-5.5）
            print(json.dumps({"coverage": "partial", "reason": err,
                              "findings": []}, ensure_ascii=False))
            return 0
        metrics = data.get("metrics", {})
        meta = data.get("meta", {})
        l1, l2, skipped, blocked = evaluate(rules, metrics, meta, args.stage, thresholds)
        missing = (data.get("coverage") or {}).get("missing") or []
        print("L1:")
        for x in l1:
            print(x)
        print("L2:")
        for x in l2:
            print(x)
        if skipped:
            print("# 跳过 %d 条规则（变量未提供）" % skipped)
        print(coverage_line(missing, blocked))
        print(factors_line(metrics, meta, thresholds, args.stage))
        return 0

    if args.cmd == "selfcheck":
        return selfcheck(args.closure or default_closure(), args.lock or default_lock())

    if args.cmd == "selftest":
        return selftest(args.closure)


if __name__ == "__main__":
    sys.exit(main())
