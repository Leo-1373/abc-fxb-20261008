#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sk_ew 贷后时点序列趋势比对与规则求值（契约 IF-1.3 / IF-5.5）。

三个子命令：
  trend      监测时点序列 → 信号状态 + 时序派生量（升级 / 拟解除 / 逾期跨档）
  evaluate   最新时点指标 + 规则闭包 → A| 预警行（含时序升级后的等级）
  selfcheck  闭包自检

为什么时序比对放在脚本里（分工.md P3 任务 3、docs/交付说明.md §8）：
  1. 监测时点一多，模型逐时点读会把上下文撑爆，而且跨档判定很容易算错
  2. 「连续 N 个时点」是纯查表，脚本算天然幂等
  3. 只回传最新时点的结论，历史时点留在本侧

**贷后预警信号.md 是本脚本的信号引擎**：触发阈值直接从清单表里读，
改清单即改行为，不必动代码。清单里 `触发阈值` 非表达式的信号
（如信号10「见 §3.3 档位表」）由本脚本的跨档逻辑单独处理。

环境约束：一律用 `python`（非 python3），中文输出加 PYTHONIOENCODING=utf-8。

用法：
    python ew.py trend    --input monitor.json --signals references/贷后预警信号.md --out trend.json
    python ew.py evaluate --trend trend.json --closure references/rules.closure.md
    python ew.py selfcheck --closure references/rules.closure.md --lock references/ruleset.lock
"""
import os
import re
import sys
import json
import argparse

# 贷后预警信号.md 里信号表的列序（改清单必须同步改这里）
SIGNAL_COLS = ["编号", "信号", "类别", "短码", "触发阈值", "初判",
               "升级条件", "解除条件", "处置动作"]

# §3.3 逾期档位（必备：与风险分类办法的 90/270/360 天口径联动）
OVERDUE_BANDS = [(0, 0, "正常"), (1, 30, "关注"), (31, 90, "次级"),
                 (91, 270, "可疑"), (271, 10 ** 9, "损失")]
# 跨档起点：升到「次级」及以上才算跨档。
# 正常→关注（刚出现逾期）不算——否则任何一笔新的小额逾期都会直接判高，是误报。
CROSS_MIN_BAND = 2

# 时序阈值（贷后预警信号.md §3.1 / §3.2）
STREAK_UPGRADE = 2      # 连续 ≥2 个时点出现 → 升级一档
STREAK_FORCE = 3        # 连续 ≥3 个时点出现 → 进入强制处置通道
RELEASE_ABSENT = 2      # 最近 ≥2 个时点未出现 → 拟解除

LEVELS = ["低", "中", "高"]
LEVEL_ORDER = {"高": 0, "中": 1, "低": 2}
# 触发阈值不写成表达式、由脚本另行处理的信号
DERIVED_SIGNALS = {"信号10": "逾期跨档"}

SAFE_CHARS = re.compile(r"^[A-Za-z_0-9\s\.\+\-\*/<>=!&|()]*$")
KEYWORDS = {"true", "false"}


# ── 信号表解析 ─────────────────────────────────────────────────────

def parse_signals(path):
    """从 贷后预警信号.md 的代码块里取信号表。

    只认表头精确等于 SIGNAL_COLS 的块——文件里还有处置动作库、
    新增字段清单等别的代码块，不能误取。
    """
    if not os.path.isfile(path):
        return None, "信号清单不存在：%s" % path
    try:
        text = open(path, "r", encoding="utf-8").read()
    except OSError as e:
        return None, "信号清单读取失败：%s" % e

    header = "|".join(SIGNAL_COLS)
    out, in_block, block = [], False, []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_block:
                if block and block[0].strip() == header:
                    for raw in block[1:]:
                        parts = [p.strip() for p in raw.split("|")]
                        if len(parts) == len(SIGNAL_COLS):
                            out.append(dict(zip(SIGNAL_COLS, parts)))
                block, in_block = [], False
            else:
                in_block, block = True, []
            continue
        if in_block:
            block.append(line)
    if not out:
        return None, "信号清单里没有解析到信号表（表头应为 %s）" % header
    return out, None


# ── 表达式求值（与 analyze.py 同构；本包自包含，不能跨包复用）────

def cond_vars(cond):
    return sorted({t for t in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", cond)
                   if t.lower() not in KEYWORDS})


def eval_cond(cond, env):
    """安全求值。变量缺失抛 ValueError——**跳过优于臆测**。"""
    if not SAFE_CHARS.match(cond):
        raise ValueError("表达式含非法字符：%s" % cond)
    expr = cond.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"\btrue\b", "True", expr, flags=re.I)
    expr = re.sub(r"\bfalse\b", "False", expr, flags=re.I)
    scope = {k: v for k, v in env.items() if isinstance(v, (int, float, bool))}
    try:
        return bool(eval(expr, {"__builtins__": {}}, scope))
    except NameError as e:
        raise ValueError("表达式引用了未提供的变量：%s（%s）" % (e, cond))


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


# ── 时序比对 ───────────────────────────────────────────────────────

def band_of(days):
    """逾期天数 → (档位序号, 档位名)。"""
    d = days if isinstance(days, (int, float)) else 0
    for i, (lo, hi, name) in enumerate(OVERDUE_BANDS):
        if lo <= d <= hi:
            return i, name
    return 0, "正常"


def _band_label(i):
    """档位标签，带区间——轨迹要能被人一眼看懂。"""
    lo, hi, name = OVERDUE_BANDS[i]
    if i == 0:
        return name
    return "%s(%d+)" % (name, lo) if hi >= 10 ** 9 else "%s(%d-%d)" % (name, lo, hi)


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


# ── 输入形状兼容层 ─────────────────────────────────────────────────
# 为什么必须有：契约 IF-3 声明的槽是 `monitor_ts[]`（**时点数组**），但上游给的
# 可能是 `monitor{}`（**单对象快照**，题目里现在就是这个形状）。当前实现只读
# `monitor_ts`，拿到单对象会**空转**：一条信号都不判、也不报错。
# 单对象的正确处理是：当作 1 个时点跑，然后**明说时序判定不可用**——
# 「连续 ≥2 个时点才升级」「最近 2 个时点消失才算拟解除」在 1 个时点上无意义。
TS_ALIASES = {"monitor_date": "date", "业务日期": "date", "监测日期": "date"}
BIZ_OK_WORDS = ("正常经营", "正常")
CONTACT_OK_WORDS = ("可联系", "能联系", "可联络", "正常")


def pick_series(data):
    """从输入里取监测时点序列。返回 (序列, 降级说明)。

    认三种写法：`monitor_ts[]` / `monitor[]` / `monitor{}`（单对象）。
    后一种要带降级说明——它是快照，不是趋势。
    """
    for key in ("monitor_ts", "monitor"):
        v = data.get(key)
        if isinstance(v, list) and v:
            return v, None
        if isinstance(v, dict) and v:
            return [v], ("输入给的是单对象快照（%s{}），只有 1 个监测时点 → "
                         "升级/解除/跨档等**时序判定不可用**，coverage: partial" % key)
    return None, None


def normalize_ts(ts):
    """单时点的字段名/取值适配。返回 (归一化时点, 说明列表)。

    只翻译形状，**不改口径**：连续时点数、跨档、余额下降率仍按 契约 §3 算。
    """
    out, notes = {}, []
    for k, v in (ts or {}).items():
        out[TS_ALIASES.get(k, k)] = v
    # 字符串状态 → 布尔标记（仅在布尔字段缺失时翻译，不覆盖已明确给出的值）
    for src, dst, ok_words in (("biz_status", "biz_abnormal", BIZ_OK_WORDS),
                               ("contact", "contact_fail", CONTACT_OK_WORDS)):
        if out.get(dst) is None and isinstance(out.get(src), str) and out[src].strip():
            s = out[src].strip()
            out[dst] = 0 if any(w in s for w in ok_words) else 1
            notes.append("按形状兼容把 %s=%r 翻成 %s=%d（口径见 B1 提案）"
                         % (src, s, dst, out[dst]))
    if out.get("bal_drop") is not None and out.get("balance") is None:
        # 契约口径是 (上期余额-本期余额)/上期余额。输入直给一个数就有了两套口径，
        # 迟早对不上——所以**不采信**，宁可不判这个信号（跳过优于臆测）。
        notes.append("输入直接给了 bal_drop；契约口径由相邻两期余额现算，"
                     "已忽略输入值（该信号可能因此不判）")
    return out, notes


def _timepoint_env(ts, prev_balance):
    """单时点 → 变量作用域。

    `bal_drop` 不在时点数据里，由相邻两期余额现算——口径见 dict.yaml：
    (上期余额-本期余额)/上期余额。没有上期 → None（**不是 0**，
    0 会让 `bal_drop>0.5` 这类阈值静默失效或误报）。
    """
    env = {}
    # 必须覆盖 贷后预警信号.md §1 信号表「触发阈值」列引用到的**全部**短码，
    # 否则那些信号会因变量缺失被整体跳过（见 eval_cond 的 NameError → skipped）。
    for k in ("overdue_days", "repay_delay_cnt", "use_dev_flag", "biz_abnormal",
              "contact_fail", "natural_disaster", "price_shock", "guarantee_deplete",
              "repay_by_new_loan", "fund_backflow_flag", "split_disburse_flag",
              "forbidden_use_flag", "litigation_flag", "penalty_flag", "biz_idle_flag",
              "family_change_flag", "staff_drop_ratio", "collateral_value_drop",
              "guarantor_ability_down", "mortgage_reg_expired", "joint_guarantee_risk",
              "epidemic_flag", "policy_shock_flag", "agri_insurance_lapse",
              "upstream_down_flag", "refuse_check_flag", "account_frozen_flag",
              "other_bank_overdue", "new_multi_lend_post", "query_surge_post",
              "check_overdue_days", "impersonation_flag", "multi_borrow_one_use",
              "company_use_personal", "aml_level_up", "collateral_disposed_flag",
              "illegal_fundraise_flag", "convicted_flag", "check_freq_shortfall",
              # 〔2020〕70号第八条的风险线索类型（110号第十六条点名该规程）
              "multi_loan_same_account", "multi_loan_same_repay", "batch_cash_repay",
              "staff_client_fund"):
        if ts.get(k) is not None:
            env[k] = ts[k]
    bal = _num(ts.get("balance"))
    if bal is not None and prev_balance not in (None, 0):
        env["bal_drop"] = round((prev_balance - bal) / prev_balance, 4)
    return env


def trend(monitor_ts, signals):
    """逐时点求值信号 + 时序比对。

    返回 (latest_env, derived, states, notes, skipped)。
    """
    notes = []
    if not monitor_ts:
        # 退化输入：没有监测时点就没有"趋势"可言。返回空，绝不臆测。
        return None, None, None, ["monitor_ts 缺失或为空 → 无法做时序比对"], 0

    # 时点字段的形状适配（见 normalize_ts）——只翻译形状，不改口径。
    norm, tnote = [], []
    for t in monitor_ts:
        n, ns = normalize_ts(t)
        norm.append(n)
        for x in ns:
            if x not in tnote:
                tnote.append(x)
    monitor_ts = norm
    notes.extend(tnote)

    tss = sorted(monitor_ts, key=lambda t: str(t.get("date", "")))
    if len(tss) < 2:
        notes.append("只有 1 个监测时点 → 无法判定升级/解除，全部按「新增」处理"
                     "（coverage: partial）")

    envs, bands, prev_bal = [], [], None
    for t in tss:
        envs.append(_timepoint_env(t, prev_bal))
        bal = _num(t.get("balance"))
        prev_bal = bal if bal is not None else prev_bal
        bands.append(band_of(envs[-1].get("overdue_days")))

    # ① 每个信号在每个时点命中与否
    states, skipped = [], 0
    for sig in signals:
        no, cond = sig["编号"], sig["触发阈值"]
        if no in DERIVED_SIGNALS:
            continue
        hits = []
        for env in envs:
            try:
                hits.append(eval_cond(cond, env))
            except ValueError:
                # 变量未提供（如 check_overdue_days 尚未进 monitor_ts）→ 整体跳过。
                # 记一次 skipped，让调用方知道有多少信号没判。
                hits, skipped = None, skipped + 1
                break
        if hits is None:
            continue
        st = _state_of(sig, hits, tss, bands)
        if st is not None:      # 从没命中过的信号不进状态表
            states.append(st)

    # ② 逾期跨档（信号10）：升到「次级」及以上才算跨档
    band_idx = [b[0] for b in bands]
    crossed = 0
    if len(band_idx) >= 2:
        crossed = 1 if (max(band_idx) >= CROSS_MIN_BAND
                        and max(band_idx) > band_idx[0]) else 0
    if crossed:
        states.append({"no": "信号10", "name": DERIVED_SIGNALS["信号10"],
                       "类别": "还款行为", "hits": len(band_idx),
                       "streak": 1, "state": "升级", "level": "高",
                       "first_date": str(tss[0].get("date", "")),
                       "last_date": str(tss[-1].get("date", "")),
                       "trajectory": ">".join(_band_label(b[0]) for b in bands)})

    # 时序升级后的等级只涨不跌，便于调用方直接取用
    for s in states:
        s["level_升级"] = _elevate(s["level"]) if s["state"] == "升级" else s["level"]

    active = [s for s in states if s["state"] != "拟解除"]
    derived = {
        "overdue_band": bands[-1][1],
        "overdue_band_crossed": crossed,
        "overdue_trajectory": ">".join(_band_label(b[0]) for b in bands),
        "n_ts": len(tss),
        # 机器可读的降级标记：只有 1 个时点时，本 skill 的核心能力（时序判定）
        # 不成立，调用方应据此标 coverage: partial 而不是当成完整结论。
        "coverage": "partial" if len(tss) < 2 else "full",
        # 时序聚合量（贷后预警信号.md §3.1/§3.2 的可判定化）。
        # **只放数值/布尔**——`eval_cond` 的作用域会滤掉字符串，
        # 规则引用了 `overdue_band`/`overdue_trajectory` 只会被静默跳过。
        "ew_max_streak": max([s["streak"] for s in active] or [0]),
        "ew_upgrade_cnt": len([s for s in active if s["state"] == "升级"]),
        "ew_force_cnt": len([s for s in active
                             if s["state"] == "升级" and s["streak"] >= STREAK_FORCE]),
        "ew_release_cnt": len([s for s in states if s["state"] == "拟解除"]),
    }
    return envs[-1], derived, states, notes, skipped


def _state_of(sig, hits, tss, bands):
    """单个信号的时序状态。

    状态取值（与 skill.md 的输出示例对齐）：
      新增   最近时点才出现（streak 1）
      持续   连续 ≥2 个时点仍是同档
      升级   连续 ≥3 个时点，或跨档
      拟解除 最近 ≥2 个时点都没出现（**不自动解除**，等人工确认）
    """
    last = len(hits) - 1
    hit_idx = [i for i, h in enumerate(hits) if h]
    # 从没命中过 → 不是信号，不进状态表（否则每个没发生的信号都会变成"拟解除"，噪音）
    if not hit_idx:
        return None
    # 尾部连续命中数
    streak = 0
    for h in reversed(hits):
        if h:
            streak += 1
        else:
            break
    # 尾部连续未命中数
    absent = 0
    for h in reversed(hits):
        if not h:
            absent += 1
        else:
            break

    if hits[last]:
        state = "升级" if streak >= STREAK_FORCE else ("持续" if streak >= STREAK_UPGRADE else "新增")
    elif absent >= RELEASE_ABSENT:
        state = "拟解除"
    else:
        state = "持续"
    return {
        "no": sig["编号"], "name": sig["信号"], "类别": sig["类别"],
        "hits": len(hit_idx), "streak": streak, "state": state,
        "level": sig["初判"],
        "ask": sig["处置动作"],
        "first_date": str(tss[hit_idx[0]].get("date", "")),
        "last_date": str(tss[hit_idx[-1]].get("date", "")),
        "trajectory": ">".join(_band_label(b[0]) for b in bands),
    }


def _elevate(level):
    """升级一档，封顶「高」。"""
    i = LEVELS.index(level) if level in LEVELS else 0
    return LEVELS[min(i + 1, len(LEVELS) - 1)]


# ── 规则求值 ───────────────────────────────────────────────────────

def signal_no_of(rule):
    """规则 → 信号编号。靠 basis 列的「贷后预警信号N」建立映射。"""
    m = re.search(r"贷后预警信号\s*(\d+)", rule.get("basis", ""))
    return ("信号%s" % m.group(1)) if m else None


def evaluate(rules, latest, derived, states, stage="贷后"):
    """返回 (A| 行, L2 行, 跳过数, 拟解除行)。

    时序升级是**脚本的职责**，不是规则的：现有规则全是单点快照，
    没有任何一条能表达「信号在恶化」。规则判"现在什么情况"，
    脚本判"这情况在往哪走"，两者合并才是贷后动态预警。

    但规则要能**参与**时序判定，就必须拿得到连续时点数——故按规则自己的
    信号注入 `signal_streak`（见 `signal_no_of`，靠 `basis` 列的
    「贷后预警信号N」映射）。这条短码把"连续 N 个时点"从每个信号各自的
    派生量变成通用输入，省下三十多个信号各写一套 streak 的重复（信号表 §5）。
    """
    env = dict(latest or {})
    env.update(derived or {})
    by_no = {s["no"]: s for s in (states or [])}

    l1, l2, skipped = [], [], 0
    for r in rules:
        st = by_no.get(signal_no_of(r))
        # 信号从没命中过（或该规则不由信号驱动）→ 不注入，规则引用它即变量缺失
        # → 跳过。**跳过优于臆测**：拿不到 streak 时把"连续"当成立才是误报。
        scope = env
        if st is not None:
            scope = dict(env)
            scope["signal_streak"] = st["streak"]
        try:
            hit = eval_cond(r["cond"], scope)
        except ValueError:
            skipped += 1
            continue
        if not hit:
            continue
        state = st["state"] if st else "新增"
        level = st["level_升级"] if st else r["level"]
        if state == "拟解除":
            # 最新时点已不成立，降级提示而不报风险（避免"已经好了还报高"）
            level = "低"
        ev = ";".join("ew.%s=%s" % (v, scope[v])
                      for v in cond_vars(r["cond"]) if v in scope)
        if st and st.get("trajectory"):
            ev += ";ew.升级轨迹=%s" % st["trajectory"]
        conf = "0.95" if state == "升级" else "0.9"
        # 建议取规则自己的 advice——它带逾期档位，比信号表的通用动作更具体
        l1.append((LEVEL_ORDER.get(level, 9),
                   "A|%s|%s|%s|%s|%s" % (r["id"], level, r["title"], state,
                                         r["advice"])))
        l2.append("%s|ev=%s|basis=%s|conf=%s" % (r["id"], ev, r["basis"], conf))

    l1.sort(key=lambda x: x[0])

    # 拟解除的信号在最新时点不命中任何规则，但必须报出来等人工确认——
    # 系统擅自解除会掩盖真实风险（贷后预警信号.md §3.2）。
    released = []
    for i, s in enumerate(states or [], 1):
        if s["state"] == "拟解除":
            released.append("A|AD%d|低|%s|拟解除|确认后解除预警" % (i, s["name"]))

    return [x[1] for x in l1], l2, skipped, released


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

    t = sub.add_parser("trend")
    t.add_argument("--input", required=True)
    t.add_argument("--signals", default="references/贷后预警信号.md")
    t.add_argument("--out", default=None)

    e = sub.add_parser("evaluate")
    e.add_argument("--trend", required=True)
    e.add_argument("--closure", required=True)
    e.add_argument("--stage", default="贷后")

    s = sub.add_parser("selfcheck")
    s.add_argument("--closure", required=True)
    s.add_argument("--lock", required=True)

    args = ap.parse_args()

    if args.cmd == "trend":
        data = json.loads(open(args.input, "r", encoding="utf-8").read())
        signals, err = parse_signals(args.signals)
        if err:
            print(json.dumps({"coverage": "partial", "reason": err}, ensure_ascii=False))
            return 0
        series, sneak = pick_series(data)
        if sneak:
            print("# %s" % sneak)
        latest, derived, states, notes, skipped = trend(series, signals)
        if latest is None:
            print(json.dumps({"coverage": "partial", "reason": notes[0],
                              "alerts": []}, ensure_ascii=False))
            return 0
        for n in notes:
            print("# %s" % n)
        if skipped:
            print("# 跳过 %d 个信号（其触发阈值引用的变量未提供）" % skipped)
        active = [s for s in states if s["state"] != "拟解除"]
        print("信号状态（共 %d 个命中）:" % len(active))
        for s in sorted(active, key=lambda x: LEVEL_ORDER.get(x["level_升级"], 9)):
            print("  %-8s %-12s %-4s 连续%d次 %s" %
                  (s["no"], s["name"], s["level_升级"], s["streak"], s["state"]))
        for s in states:
            if s["state"] == "拟解除":
                print("  %-8s %-12s %-4s %s" % (s["no"], s["name"], "低", "拟解除"))
        print("逾期轨迹：%s（跨档=%s）" % (derived["overdue_trajectory"],
                                          "是" if derived["overdue_band_crossed"] else "否"))
        if derived.get("coverage") != "full":
            print("# coverage: %s —— %s 个监测时点，时序能力不可用"
                  % (derived.get("coverage"), derived.get("n_ts")))
        if args.out:
            open(args.out, "w", encoding="utf-8", newline="\n").write(
                json.dumps({"latest": latest, "derived": derived,
                            "signals": states}, ensure_ascii=False, indent=2))
            print("[sk_ew] 趋势结果已写入 %s" % args.out)
        return 0

    if args.cmd == "evaluate":
        data = json.loads(open(args.trend, "r", encoding="utf-8").read())
        rules, err = parse_closure(args.closure)
        if err:
            print(json.dumps({"coverage": "partial", "reason": err,
                              "alerts": []}, ensure_ascii=False))
            return 0
        l1, l2, skipped, released = evaluate(rules, data.get("latest", {}),
                                             data.get("derived", {}),
                                             data.get("signals", []), args.stage)
        print("L1:")
        for x in l1:
            print(x)
        for x in released:
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
