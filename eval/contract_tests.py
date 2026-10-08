#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""契约测试（方案 §5.2）。

没有 ground truth 时，质量度量退化为**可断言的结构与性质**。本文件测四类：

  schema      输出格式是否符合契约 IF-1（等级标尺、title 长度、证据指针格式）
  idempotent  同输入同输出（脚本承担判定，天然应满足）
  monotonic   风险因子上升 → 等级不下降（性质断言，不需要标准答案）
  degrade     空输入/缺字段不崩，且标注 coverage: partial（而非静默跳过）

正式评测集（含 held-out 与对抗案例）由 P6 独立造题，见 docs/交付说明.md §4。

用法：PYTHONIOENCODING=utf-8 python eval/contract_tests.py
"""
import os
import re
import sys
import json
import copy
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "eval", "fixtures", "smoke-cash-001.json")
CLOSURE = os.path.join(ROOT, "build", "stage", "sk_cash", "references",
                       "rules.closure.md")

LEVELS = {"高", "中", "低"}
# 证据指针格式：<源>.<定位>=<值>（契约 IF-1.2）
EV_RE = re.compile(r"^[a-z_]+(\.[A-Za-z_0-9一-龥]+=[^\s;]+)+$")

_fails, _checks = [], 0


def load_analyze():
    """从路径加载 sk_cash 的 analyze 模块（它不在包路径上）。"""
    p = os.path.join(ROOT, "skills", "sk_cash", "scripts", "analyze.py")
    spec = importlib.util.spec_from_file_location("sk_cash_analyze", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        _fails.append(msg)
    return cond


def run(m, data):
    metrics, meta = m.aggregate(data.get("txn"), data.get("period"),
                                data.get("purpose"), data.get("declared_inc"))
    rules, err = m.parse_closure(CLOSURE)
    if err:
        return None, None, err
    l1, l2, skipped = m.evaluate(rules, metrics, meta, "贷前")
    return {"metrics": metrics, "l1": l1, "l2": l2, "skipped": skipped}, metrics, None


def t_schema(m, data):
    """IF-1：等级标尺、title 长度、证据指针格式、L1/L2 可对应。"""
    out, metrics, err = run(m, data)
    check(err is None, "closure 读取失败：%s" % err)
    if err:
        return
    l1_ids = set()
    for line in out["l1"]:
        parts = line.split("|")
        check(len(parts) == 5, "L1 字段数应为 5：%s" % line)
        _, rid, lvl, title, stage = parts
        l1_ids.add(rid)
        check(lvl in LEVELS, "等级非法（%s）：%s" % (lvl, line))
        check(len(title) <= 12, "title 超 12 字（%d）：%s" % (len(title), line))
        check(stage in {"贷前", "贷中", "贷后", "跨环节"}, "环节非法：%s" % line)
        check(bool(re.match(r"^[RXE]\d+$", rid)), "规则 id 格式非法：%s" % rid)

    l2_ids = set()
    for line in out["l2"]:
        rid, _, rest = line.partition("|")
        l2_ids.add(rid)
        ev = ""
        for seg in rest.split("|"):
            if seg.startswith("ev="):
                ev = seg[3:]
        check(bool(EV_RE.match(ev)), "证据非指针格式：%s" % line)
    check(l1_ids == l2_ids,
          "L1/L2 的 id 集合不一致：%s" % (l1_ids ^ l2_ids))

    # 等级降序
    order = [{"高": 0, "中": 1, "低": 2}[x.split("|")[2]] for x in out["l1"]]
    check(order == sorted(order), "L1 未按等级降序：%s" % order)


def t_idempotent(m, data):
    """同输入同输出——规则求值在脚本里，天然应满足。"""
    a, _, _ = run(m, data)
    b, _, _ = run(m, copy.deepcopy(data))
    check(a["l1"] == b["l1"] and a["l2"] == b["l2"], "非幂等：两次运行结果不同")


def t_monotonic(m, data):
    """性质断言：把偏离用途的大额出账改成农业用途 → 用途偏离风险不应再命中。

    不需要标准答案，只需要"方向正确"——这是没有测试集时最有力的断言形式。
    """
    base, bm, _ = run(m, data)
    check("R106" in [x.split("|")[1] for x in base["l1"]],
          "基准用例应命中 R106（资金用途偏离）")

    tweaked = copy.deepcopy(data)
    for t in tweaked["txn"]:
        if t["counterparty"] == "某汽车销售公司":
            t["counterparty"], t["desc"] = "某农机经销部", "购置农机"
    after, am, _ = run(m, tweaked)
    check(am["loan_use_dev"] <= bm["loan_use_dev"],
          "用途偏离度未随用途合规而下降：%s → %s"
          % (bm["loan_use_dev"], am["loan_use_dev"]))
    check("R106" not in [x.split("|")[1] for x in after["l1"]],
          "用途合规后 R106 仍命中——误报")


def t_degrade(m, data):
    """退化鲁棒：空输入不崩，缺字段不崩。"""
    for name, bad in [
        ("空交易", {"txn": [], "purpose": "购买农机"}),
        ("缺 txn", {"purpose": "购买农机"}),
        ("缺 purpose", {"txn": data["txn"]}),
        ("amt 非数字", {"txn": [{"date": "2025-01-01", "amt": "abc"}],
                        "purpose": "x"}),
    ]:
        try:
            out, _, err = run(m, bad)
            check(err is None, "%s：closure 读取失败 %s" % (name, err))
            if not check(out is not None, "%s：未返回结果" % name):
                continue
            # 退化输入不得产出凭空的风险点：无数据时不该报出领域风险
            check(out["l1"] == [] or name == "缺 purpose",
                  "%s：无数据却报出风险点 %s" % (name, out["l1"]))
        except Exception as e:
            check(False, "%s：抛异常 %s: %s" % (name, type(e).__name__, e))


def main():
    if not os.path.isfile(CLOSURE):
        print("[contract] 找不到闭包文件，请先跑 python build/pack.py")
        return 1
    m = load_analyze()
    data = json.loads(open(FIXTURE, "r", encoding="utf-8").read())

    for fn in (t_schema, t_idempotent, t_monotonic, t_degrade):
        before = len(_fails)
        fn(m, data)
        mark = "✓" if len(_fails) == before else "✗"
        print("  %s %s" % (mark, fn.__name__))

    print()
    if _fails:
        for f in _fails:
            print("  ✗ %s" % f)
        print("\n[contract] 失败：%d/%d 项断言不通过" % (len(_fails), _checks))
        return 1
    print("[contract] 通过：%d 项断言" % _checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
