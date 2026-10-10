#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sk_cash 流水聚合与规则求值（契约 IF-1.3 / IF-5.5）。

两个子命令：
  aggregate  原始交易 → 短码指标（高基数数据不进模型上下文）
  evaluate   指标 + 规则闭包 → L1 摘要 / L2 证据明细

为什么规则求值放在脚本里而不是让模型判：
  1. 确定性——不会因为措辞变化而漏判，天然幂等（契约测试 C-幂等）
  2. 省 token——原始流水与规则表都不进上下文，只回传结论行
  3. 可回归——同输入同输出，diff 即回归

环境约束：一律用 `python`（非 python3），中文输出加 PYTHONIOENCODING=utf-8。

用法：
    python analyze.py aggregate --input txn.json --out metrics.json
    python analyze.py evaluate  --metrics metrics.json --closure references/rules.closure.md
    python analyze.py selfcheck  --closure references/rules.closure.md --lock references/ruleset.lock
"""
import os
import re
import sys
import json
import math
import argparse

# 内部互转/冲正不计入经营收入（流水分析要点2）
INTERNAL_TYPES = {"transfer_internal", "reversal", "interest", "fee_refund"}
# 现金存入渠道
CASH_CHANNELS = {"cash", "counter_cash", "atm_cash"}
# 大额出账判定阈值（元）
LARGE_DEBIT = 5000.0
# 与申报用途相关的通用农业词（用于用途偏离判定，流水分析要点11）
AGRI_WORDS = ["农资", "化肥", "种子", "农药", "农机", "饲料", "兽药", "地租",
              "承包", "大棚", "苗木", "牲畜", "仔猪", "鱼苗", "柴油", "水电"]
# 季节性经营品类（流水分析要点3/4）——命中即 seasonal_flag=1
SEASONAL_INDUSTRIES = ["种植", "种粮", "粮食", "养殖", "畜牧", "水产", "苗木", "蔬菜",
                       "水果", "茶叶", "棉花", "油料", "中药材", "大棚", "林果", "花卉", "甘蔗"]
# 民间借贷/网贷对手关键词（流水分析要点8）
P2P_WORDS = ["网贷", "借呗", "微粒贷", "花呗", "白条", "小额贷款", "民间借贷", "典当",
             "担保公司", "融资租赁", "消费金融", "现金贷", "网络小贷"]


# ── aggregate ─────────────────────────────────────────────────────

def _month(date_str):
    return date_str[:7]


def _hour(date_str):
    m = re.search(r"[T ](\d{2}):", date_str)
    return int(m.group(1)) if m else 12


def _purpose_words(purpose):
    if not purpose:
        return []
    return [w for w in re.split(r"[、,，/\s]+", purpose) if len(w) >= 2]


def _seasonal_flag(industry):
    """季节性经营标记（要点3/4）。industry 缺失默认 0——保持既有断档/波动判定不回退。"""
    if not industry:
        return 0
    return 1 if any(w in str(industry) for w in SEASONAL_INDUSTRIES) else 0


def _round_trips(txns):
    """当日等额对敲次数（要点8）：同一自然日内入账与出账金额相等（差<0.01元）的对数。"""
    day = {}
    for t in txns:
        d = t["date"][:10]
        day.setdefault(d, {"in": [], "out": []})
        a = round(t["amt"], 2)
        (day[d]["in"] if a > 0 else day[d]["out"]).append(a)
    cnt = 0
    for d in day.values():
        ins = sorted(d["in"])
        outs = sorted(-x for x in d["out"])
        i = j = 0
        while i < len(ins) and j < len(outs):
            if abs(ins[i] - outs[j]) < 0.01:
                cnt += 1
                i += 1
                j += 1
            elif ins[i] < outs[j]:
                i += 1
            else:
                j += 1
    return cnt


def _month_range(period, fallback):
    """统计期的完整月份序列。

    断档统计必须覆盖"整月无任何交易"的月份——那正是最该算作断档的情形。
    若只看有交易的月份，断档数会系统性偏低（漏报）。
    """
    frm, to = (period or {}).get("from"), (period or {}).get("to")
    if not frm or not to:
        return sorted(fallback)
    try:
        y1, m1 = int(frm[:4]), int(frm[5:7])
        y2, m2 = int(to[:4]), int(to[5:7])
    except (ValueError, IndexError):
        return sorted(fallback)
    if (y1, m1) > (y2, m2):
        return sorted(fallback)
    out, y, m = [], y1, m1
    while (y, m) <= (y2, m2):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if len(out) > 600:      # 防御：异常输入不无限循环
            break
    return out


def aggregate(txn, period, purpose, declared_inc=None, industry=None):
    txns = []
    for t in txn or []:
        if t.get("type") in INTERNAL_TYPES:
            continue
        try:
            amt = float(t.get("amt", 0))
        except (TypeError, ValueError):
            continue
        txns.append({
            "date": str(t.get("date", "")),
            "month": _month(str(t.get("date", ""))),
            "amt": amt,
            "cp": str(t.get("counterparty", "") or ""),
            "desc": str(t.get("desc", "") or ""),
            "channel": str(t.get("channel", "") or ""),
            "type": t.get("type", ""),
        })
    txns.sort(key=lambda x: x["date"])

    credits = [t for t in txns if t["amt"] > 0]
    debits = [t for t in txns if t["amt"] < 0]

    present = {t["month"] for t in txns if t["month"]}
    months = _month_range(period, present)
    n_month = max(len(months), 1)
    total_in = sum(t["amt"] for t in credits)
    total_out = abs(sum(t["amt"] for t in debits))

    # 收入稳定性
    by_month = {}
    for t in credits:
        by_month[t["month"]] = by_month.get(t["month"], 0.0) + t["amt"]
    series = [by_month.get(m, 0.0) for m in months]
    inc_mean = (total_in / n_month) if n_month else 0.0
    if len(series) >= 2 and inc_mean > 0:
        var = sum((x - inc_mean) ** 2 for x in series) / len(series)
        inc_cv = math.sqrt(var) / inc_mean
    else:
        inc_cv = 0.0

    # 断档：连续无入账的自然月段数
    gap_cnt, run = 0, False
    for x in series:
        if x <= 0:
            if not run:
                gap_cnt += 1
                run = True
        else:
            run = False
    if not series:
        gap_cnt = 0

    # 交易对手集中度
    cp_sum = {}
    for t in credits:
        key = t["cp"] or "未知"
        cp_sum[key] = cp_sum.get(key, 0.0) + t["amt"]
    top1_share = (max(cp_sum.values()) / total_in) if total_in > 0 else 0.0

    # 进出比。无出账时比值无定义 → None（规则跳过），不可默认 0——
    # 0 恰好满足 R107（in_out_ratio<0.9，资金净流出）的阈值，会凭空报风险。
    in_out_ratio = (total_in / total_out) if total_out > 0 else None

    # 最低日终余额。
    # 注意：初值必须为空而非 0——若以 0 起算，bal_min 恒 <= 0，
    # 会让 R108（bal_min<=0，账户曾透支）对每一个客户都误报。
    # 无交易时保持 None（无法计算），而非默认 0——否则又会误报 R108。
    bal, bal_min = 0.0, None
    for t in txns:
        bal += t["amt"]
        bal_min = bal if bal_min is None else min(bal_min, bal)

    # 用途偏离：与申报用途无关的大额出账占比
    pwords = _purpose_words(purpose)
    related_amt, deviant_amt = 0.0, 0.0
    for t in debits:
        amt = abs(t["amt"])
        if amt < LARGE_DEBIT:
            continue
        hay = t["cp"] + t["desc"]
        related = any(w in hay for w in pwords) or any(w in hay for w in AGRI_WORDS)
        if related:
            related_amt += amt
        else:
            deviant_amt += amt
    big_total = related_amt + deviant_amt
    loan_use_dev = (deviant_amt / big_total) if big_total > 0 else 0.0

    # 非经营时段占比
    night = sum(1 for t in txns if _hour(t["date"]) >= 22 or _hour(t["date"]) < 6)
    night_txn_share = (night / len(txns)) if txns else 0.0

    # 现金入账占比
    cash_in = sum(t["amt"] for t in credits if t["channel"] in CASH_CHANNELS)
    cash_inc_ratio = (cash_in / total_in) if total_in > 0 else 0.0

    # 季节性经营标记（要点3/4）
    seasonal_flag = _seasonal_flag(industry)

    # 出账侧交易对手集中度（要点7）
    cp_debit = {}
    for t in debits:
        key = t["cp"] or "未知"
        cp_debit[key] = cp_debit.get(key, 0.0) + abs(t["amt"])
    top1_debit_share = (max(cp_debit.values()) / total_out) if total_out > 0 else None

    # 交易活跃度（要点1）
    n_txn = len(txns)
    txn_per_month = n_txn / n_month if n_month else 0.0

    # 单笔最大入账占比（要点8，过桥资金弱代理）
    max_in = max((t["amt"] for t in credits), default=0.0)
    large_in_share = (max_in / total_in) if total_in > 0 else None

    # 民间借贷/网贷对手（要点8）
    p2p_cps = set()
    for t in txns:
        hay = t["cp"] + t["desc"]
        if any(w in hay for w in P2P_WORDS):
            p2p_cps.add(t["cp"] or t["desc"] or "未知")
    p2p_cnt = len(p2p_cps)

    # 大额现金取现（要点10）
    large_cash_out = sum(1 for t in debits
                         if t["channel"] in CASH_CHANNELS and abs(t["amt"]) >= LARGE_DEBIT)

    # 当日等额对敲（要点8）
    round_trip_cnt = _round_trips(txns)

    # 近期入账比（要点3）：最近2个完整月 / 更早2个完整月
    recent_in_ratio = None
    if len(months) >= 4:
        rec = sum(by_month.get(m, 0.0) for m in months[-2:])
        prev = sum(by_month.get(m, 0.0) for m in months[-4:-2])
        if prev > 0:
            recent_in_ratio = rec / prev

    # 无入账时变异系数无定义 → None，而非 0
    if total_in <= 0:
        inc_cv = None

    def _r(v, n):
        return None if v is None else round(v, n)

    # 约定（重要）：指标为 None 表示「无法计算」。eval_cond 不会把 None 放进
    # 求值作用域，引用到它的规则会抛 NameError 并被跳过。
    # **绝不可用 0 代替 None**——0 往往恰好落在风险阈值内，会凭空生成风险点。
    m = {
        "inc_mean": _r(inc_mean, 2),
        "inc_cv": _r(inc_cv, 4),
        "gap_cnt": gap_cnt,
        "top1_share": _r(top1_share, 4),
        "in_out_ratio": _r(in_out_ratio, 4),
        "bal_min": _r(bal_min, 2),
        "loan_use_dev": _r(loan_use_dev, 4),
        "night_txn_share": _r(night_txn_share, 4),
        "cash_inc_ratio": _r(cash_inc_ratio, 4),
        "seasonal_flag": seasonal_flag,
        "top1_debit_share": _r(top1_debit_share, 4),
        "txn_per_month": _r(txn_per_month, 4),
        "large_in_share": _r(large_in_share, 4),
        "p2p_cnt": p2p_cnt,
        "large_cash_out": large_cash_out,
        "round_trip_cnt": round_trip_cnt,
        "recent_in_ratio": _r(recent_in_ratio, 4),
    }
    # 供 sk_rules 做三方对账用的派生量
    meta = {
        "n_txn": len(txns), "n_month": n_month,
        "total_in": round(total_in, 2), "total_out": round(total_out, 2),
        "period": period or {}, "purpose": purpose or "",
        "declared_inc": declared_inc,
    }
    return m, meta


# ── 规则闭包解析与求值 ─────────────────────────────────────────────

SAFE_CHARS = re.compile(r"^[A-Za-z_0-9\s\.\+\-\*/<>=!&|()]*$")
KEYWORDS = {"true", "false"}
LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}


def cond_vars(cond):
    """抽取 cond 中的变量名（排除数字与布尔字面量）。

    本脚本随包分发，不能 import 构建期的 ruleslib，故自带一份。
    """
    return sorted({t for t in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", cond)
                   if t.lower() not in KEYWORDS})


def parse_closure(path):
    """从 rules.closure.md 的代码块里取规则表。"""
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


def eval_cond(cond, env):
    """安全求值规则条件。仅允许算术/比较/逻辑运算。"""
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


def evaluate(rules, metrics, meta, stage="贷前"):
    """返回 (L1 摘要行, L2 证据行, 未求值规则数)。

    L1 用 title（风险点名称），L2 记录**实际指标值**而非布尔值——
    证据链要能被人工复核，`inc_cv>0.5=True` 这种没有信息量。
    """
    env = dict(metrics)
    env.update({k: v for k, v in meta.items() if isinstance(v, (int, float, bool))})
    l1, l2, skipped = [], [], 0
    for r in rules:
        try:
            hit = eval_cond(r["cond"], env)
        except ValueError:
            skipped += 1
            continue
        if not hit:
            continue
        ev = ";".join("%s.%s=%s" % (r["dim"], v, env[v])
                      for v in cond_vars(r["cond"]) if v in env)
        l1.append((LEVEL_ORDER.get(r["level"], 9),
                   "F|%s|%s|%s|%s" % (r["id"], r["level"], r["title"], stage)))
        l2.append("%s|ev=%s|basis=%s|conf=%s" % (r["id"], ev, r["basis"], "0.9"))
    l1.sort(key=lambda x: x[0])          # 契约：按等级降序（高→中→低）
    return [x[1] for x in l1], l2, skipped


# ── selfcheck ─────────────────────────────────────────────────────

def selfcheck(closure, lock):
    """闭包自检（契约 IF-5.3）：确认本包规则齐全，缺则标注 partial。"""
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

    a = sub.add_parser("aggregate")
    a.add_argument("--input", required=True)
    a.add_argument("--out", default=None)

    e = sub.add_parser("evaluate")
    e.add_argument("--metrics", required=True)
    e.add_argument("--closure", required=True)
    e.add_argument("--stage", default="贷前")

    s = sub.add_parser("selfcheck")
    s.add_argument("--closure", required=True)
    s.add_argument("--lock", required=True)

    args = ap.parse_args()

    if args.cmd == "aggregate":
        data = json.loads(open(args.input, "r", encoding="utf-8").read())
        m, meta = aggregate(data.get("txn"), data.get("period"),
                            data.get("purpose"), data.get("declared_inc"),
                            data.get("industry"))
        out = {"metrics": m, "meta": meta}
        txt = json.dumps(out, ensure_ascii=False, indent=2)
        if args.out:
            open(args.out, "w", encoding="utf-8", newline="\n").write(txt)
            print("[sk_cash] 指标已写入 %s（%d 笔交易）" % (args.out, meta["n_txn"]))
        else:
            print(txt)
        return 0

    if args.cmd == "evaluate":
        data = json.loads(open(args.metrics, "r", encoding="utf-8").read())
        rules, err = parse_closure(args.closure)
        if err:
            # 降级协议：不崩溃，标注 partial（契约 IF-5.5）
            print(json.dumps({"coverage": "partial", "reason": err,
                              "findings": []}, ensure_ascii=False))
            return 0
        l1, l2, skipped = evaluate(rules, data.get("metrics", {}),
                                   data.get("meta", {}), args.stage)
        print("L1:")
        for x in l1:
            print(x)
        print("L2:")
        for x in l2:
            print(x)
        if skipped:
            print("# 跳过 %d 条规则（变量未提供）" % skipped)
        return 0

    if args.cmd == "selfcheck":
        return selfcheck(args.closure, args.lock)


if __name__ == "__main__":
    sys.exit(main())
