#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sk_doc 材料逐项比对与规则求值（契约 IF-1.3 / IF-5.5）。

三个子命令：
  check      材料清单 + 申请信息 → doc 维度短码（缺件 / 过期 / 矛盾 / 红线 / 流程合规）
  evaluate   短码 + 规则闭包 → L1 摘要 / L2 证据明细
  selfcheck  闭包自检

为什么比对放在脚本里而不是让模型逐项看（分工.md P3 任务 3）：
  1. 材料几十项，模型逐项比对又慢又容易漏，而且漏了不报错
  2. 比对是确定性的活，天然幂等——同输入同输出
  3. 只回传短码，不把整张材料表送进主智能体上下文

短码按「能不能给出不同处置动作」拆开，而不是只给总数：
缺件按类别拆（缺经营材料找村委、缺担保材料找保证人）、
矛盾按比对项拆（姓名矛盾要核身份、金额矛盾要核合同）。
阈值与口径全部写在 必备材料清单.md（§5 核验要点、§7 靶子、§8 脚本衔接）。

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

# 关键证照：过期即需换发后才能受理，与普通佐证件分开计数（必备材料清单 §5.1）。
# 身份证过期另有 id_valid==0（R005）拦一道，故此处不再单列身份证。
KEY_CERT_TYPES = {"BIZ_LICENSE", "ANIMAL_HEALTH_CERT", "BREED_PERMIT",
                  "WATER_PERMIT", "SPECIAL_PERMIT"}

# 缺件按「类别」拆分（清单 §1–§3 第 4 列）。总数给不出处置动作，
# 拆开才知道该找谁补——缺经营材料找村委，缺担保材料找保证人。
MISS_CATEGORIES = {
    "doc_miss_identity_cnt": {"身份"},
    "doc_miss_income_cnt": {"收入"},
    "doc_miss_use_cnt": {"用途"},
    "doc_miss_guarantee_cnt": {"担保"},
    "doc_miss_biz_cnt": {"经营"},
    "doc_miss_contract_cnt": {"合同", "支付"},
    "doc_miss_postcheck_cnt": {"检查"},
    "doc_miss_archive_cnt": {"归档"},
}

# 跨材料比对项 → 计数短码（清单 §5.2）。矛盾按比对项拆开才能说清「跟谁核什么」。
INCONSIST_CODES = {"name": "doc_inconsist_name_cnt",
                   "id_no": "doc_inconsist_idno_cnt",
                   "area": "doc_inconsist_area_cnt",
                   "amount": "doc_inconsist_amount_cnt",
                   "date": "doc_inconsist_date_cnt"}

# 临期预警窗口：距受理日 ≤30 天到期只提示，不算过期（清单 §5.1）
EXPIRED_SOON_DAYS = 30
# 首贷检查期限：发放后 3 个月内（2020贷后办法第十五条）
FIRST_CHECK_MONTHS = 3
# 现场检查频次分档（2020贷后办法第二十一条(二)1/2）：信用、非信用两条尺子。
# 区间**逐字照条文**——「信用30万（不含）到100万（含）」「非信用50万（不含）到
# 200万（含）」→ 至少 1 次；「100万以上」「200万以上」→ 至少 2 次。
# 端点开闭写错，30万整 / 100万整这种整数额度就会判错档，而这些正是最容易拿来出题的数。
# 低于起档的属「现场抽查」（第二十一条(三)：信用 30万（含）以下按不低于 10%、非信用
# 50万（含）以下按不低于 5% 考核）——那是按**管理户数比例**、不是按笔数，故返回 None。
ONSITE_CHECK_TIERS = {"信用": [(300000, 1000000, 1), (1000000, None, 2)],
                      "非信用": [(500000, 2000000, 1), (2000000, None, 2)]}
# 第二十一条(二)2 的例外：采用存单、凭证式国债、贵金属质押，以及政府背景担保公司、
# 保证保险方式的，每年至少检查一次（不适用「至少两次」）。
ONSITE_CHECK_EXEMPT = ("存单", "凭证式国债", "贵金属", "政府背景担保公司", "保证保险")
# 「信用方式 / 非信用方式」（2020贷后办法第二十条、第二十一条(二)）的判定词。
# ⚠ 不能写成 `"信用" in gua`——`"非信用"` 里也含「信用」，那样会把非信用贷款
#   判成信用尺子，额度档随之整体错位。判不出来时返回 None，**不猜**（契约 IF-1.4）。
NON_CREDIT_WORDS = ("保证", "抵押", "质押", "担保", "保险", "存单", "国债", "贵金属")


def credit_bar(gua):
    """担保方式 → '信用' / '非信用' / None（判不出）。

    '非信用' 必须先于 '信用' 判定（前者是后者的超串）。
    """
    if "非信用" in gua:
        return "非信用"
    if "信用" in gua:
        return "信用"
    if any(w in gua for w in NON_CREDIT_WORDS):
        return "非信用"
    return None

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


def parse_type_codes(path):
    """取清单里**全部**材料 type 代码——含 §1.1/§2.1/§3.1 的经办留档附表。

    只认 `type|` 打头的代码块。附表用的表头与主表不同（7 列），
    `parse_checklist` 会按表头精确匹配跳过它们，但**它们仍是合法 type**：
    不收集就会把「面谈记录」「贷后现场检查表」误判成"清单外材料"。
    """
    if not os.path.isfile(path):
        return set()
    out, in_block, first = set(), False, None
    for line in open(path, "r", encoding="utf-8").read().splitlines():
        if line.strip().startswith("```"):
            in_block, first = (not in_block), None
            continue
        if not in_block:
            continue
        if first is None:
            first = line.strip()
            continue
        if first.startswith("type|"):
            code = line.split("|")[0].strip()
            if code:
                out.add(code)
    return out


DATE_RE = re.compile(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def _date(v):
    """取日期值。取不出来返回 None——**绝不默认为今天**（契约 IF-1.4）。

    容忍 `2030-01-01` / `2030-1-1` / `2030/1/1` 三种写法：材料上的日期是客户填的，
    格式不统一很常见。**但解析不出来必须返回 None 而不是猜**——把"读不懂"
    当成"已过期"会凭空判一条证照过期（高）。
    """
    m = DATE_RE.match(str(v or ""))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _add_months(d, months):
    """加 N 个自然月，日按目标月天数截断（3-31 + 1 月 → 4-30）。"""
    y, m = d.year, d.month + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    last = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return datetime.date(y, m, min(d.day, last))


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


# ── 输入形状兼容层 ─────────────────────────────────────────────────
# 为什么必须有：契约的 doc[] 每项带 `type`；而 `docs/评测方案.md` §四 给的是
# `{name, present, expired}`——**没有 type**。只认 type 会让"交了的材料"被静默丢掉，
# 于是每一份必交要件都被算成缺件。实测 5 份齐全的材料 → doc_miss_cnt=6、
# redline_hit=1 → 凭空报出 R001（高）+ X007（高）。这跟契约 IF-1.4 要拦的
# 「空输入报出一堆高风险」是同一类错误，只是入口不同。
#
# 规矩：两种形状都认。没有 type 就按「名称」回查清单（精确 → 别名 → 子串）；
# **回查不到的记 unresolved，绝不当作缺件**（清单 §8 的规定）。
NAME_ALIASES = {
    "身份证": "ID_CARD", "借款人身份证": "ID_CARD", "身份证复印件": "ID_CARD",
    "配偶身份证": "SPOUSE_ID", "结婚证": "MARRIAGE_CERT",
    "婚姻状况证明": "MARRIAGE_CERT", "户口簿": "HUKOU", "户口本": "HUKOU",
    "常住地证明": "RESIDENCE_PROOF", "共同借款人名单": "MEMBER_LIST",
    "农户贷款业务申请表": "LOAN_APPLY", "申请表": "LOAN_APPLY",
    "征信查询授权书": "CREDIT_AUTH", "征信授权": "CREDIT_AUTH",
    "土地承包合同": "LAND_CERT", "土地承包经营权证": "LAND_CERT",
    "土地经营权证": "LAND_CERT", "土地权证": "LAND_CERT",
    "经营场所证明": "PREMISE_PROOF", "营业执照": "BIZ_LICENSE",
    "动物防疫条件合格证": "ANIMAL_HEALTH_CERT", "种畜禽生产经营许可证": "BREED_PERMIT",
    "水域滩涂养殖证": "WATER_PERMIT", "特殊行业许可": "SPECIAL_PERMIT",
    "购建房合同或协议": "HOUSE_BUY_DOCS", "购销合同": "PURCHASE_CONTRACT",
    "购销/订单合同": "PURCHASE_CONTRACT", "订单合同": "PURCHASE_CONTRACT",
    "农机购置合同": "FARM_MACHINE_CONTRACT", "农机购置合同及补贴确认表": "FARM_MACHINE_CONTRACT",
    "项目计划书": "PROJECT_PLAN", "收入证明": "INC_PROOF", "银行流水": "BANK_FLOW",
    "完税证明": "TAX_PROOF", "免税证明": "TAX_PROOF", "销售台账": "SALES_LEDGER",
    "收购凭证": "SALES_LEDGER", "资产证明": "ASSET_PROOF",
    "村级信用评定结果": "VILLAGE_RATING", "农业保险单": "AGRI_INSURANCE",
    "保证人身份证": "GUARANTOR_ID", "同意担保承诺书": "GUARANTOR_CONSENT",
    "抵质押物权属证明": "COLLATERAL_CERT", "抵押物权属证明": "COLLATERAL_CERT",
    "抵（质）押物权属证明": "COLLATERAL_CERT",
    "处分权人同意抵质押证明": "COOWNER_CONSENT", "共有人同意证明": "COOWNER_CONSENT",
    "评估报告": "APPRAISAL_REPORT", "价值确认书": "APPRAISAL_REPORT",
    "农户联保协议": "JOINT_GUARANTEE_PACT", "联保协议": "JOINT_GUARANTEE_PACT",
    "结算账户开立证明": "ACCOUNT_PROOF", "账户开立证明": "ACCOUNT_PROOF",
    "借款合同": "LOAN_CONTRACT", "担保合同": "GUARANTEE_CONTRACT",
    "面签影像": "SIGN_RECORD", "核验记录": "SIGN_RECORD",
    "受托支付委托书": "ENTRUST_PAY_ORDER", "自主支付约定条款": "SELF_PAY_CLAUSE",
    "放款条件落实单": "DISBURSE_CHECKLIST", "他项权证": "OTHER_RIGHT_CERT",
    "交接清单": "OTHER_RIGHT_CERT", "首次贷后检查记录": "FIRST_POST_CHECK",
    "首贷检查记录": "FIRST_POST_CHECK", "用途核查凭证": "USE_CHECK_EVIDENCE",
    "还款提示记录": "REPAY_REMINDER", "贷后检查报告": "POST_CHECK_REPORT",
    "担保复评记录": "GUARANTEE_REVIEW", "档案归档清单": "ARCHIVE_LIST",
    "展期申请": "EXTEND_APPROVAL", "面谈记录": "INTERVIEW_RECORD",
    "面谈记录及影像": "INTERVIEW_RECORD", "贷后现场检查表": "POST_CHECK_FORM",
    "催收回执": "COLLECTION_RECEIPT",
}


def build_name_index(items):
    """清单 → {名称: type}，用于按名称回查。"""
    out = {}
    for it in items:
        n, t = (it.get("名称") or "").strip(), (it.get("type") or "").strip()
        if n and t:
            out.setdefault(n, t)
    return out


def _match_by_substring(nm, table):
    """子串兜底：按候选长度降序，优先匹配更长（更具体）的名称。"""
    for k in sorted(table, key=len, reverse=True):
        if len(k) >= 2 and (k in nm or nm in k):
            return table[k]
    return None


def resolve_type(item, name_idx):
    """doc[] 项 → (规范 type, 匹配依据)。认不出来返回 (None, None)。"""
    t = str(item.get("type") or "").strip()
    if t:
        return t, "type"
    nm = str(item.get("name") or item.get("名称") or "").strip()
    if not nm:
        return None, None
    if nm in name_idx:
        return name_idx[nm], "清单名称"
    if nm in NAME_ALIASES:
        return NAME_ALIASES[nm], "常见别名"
    t = _match_by_substring(nm, name_idx)
    if t:
        return t, "清单名称部分匹配"
    t = _match_by_substring(nm, NAME_ALIASES)
    if t:
        return t, "别名部分匹配"
    return None, None


def normalize_doc(doc, items):
    """把 doc[] 归一化成带 `type` 的形状。

    返回 (已交项, 认不出的名称, 显式标为未交的名称)。
    `present: false` 的项**不计入已交**——它本来就是"没交"的意思，
    丢掉它才能让对应的必交要件正确地被算成缺件。
    """
    idx = build_name_index(items)
    out, unresolved, absent = [], [], []
    for d in doc or []:
        if not isinstance(d, dict):
            unresolved.append(str(d))
            continue
        nm = str(d.get("name") or d.get("名称") or d.get("type") or "?").strip()
        if d.get("present") is False:
            absent.append(nm)
            continue
        t, how = resolve_type(d, idx)
        if not t:
            unresolved.append(nm)
            continue
        nd = dict(d)
        nd["type"] = t
        if how != "type":
            # 这一项是靠「名称」认出来的。在评测方案形状里 `name` 是**材料名**
            # （"借款人身份证"），而一致性比对里的 `name` 是**持有人姓名**——
            # 两个语义撞在同一个键上。留着它会让 §5.2 的姓名比对把"材料名"
            # 当成"姓名"去和申请人比 → 凭空报出材料矛盾（实测 3 处）。
            # 所以改挂到 `名称` 下，把 `name` 腾出来。
            nd["名称"] = d.get("name") or d.get("名称")
            nd.pop("name", None)
        out.append(nd)
    return out, unresolved, absent


def check(doc, applicant, stage, items, situations, as_of, known_types=None):
    """核心比对。返回 (metrics, facts, notes)。"""
    applicant = applicant or {}
    notes = []

    # 退化输入（缺失或空表）→ 无法计算，一律 None。
    # 关键：**空材料表不等于「43 项全缺」**。按后者处理会凭空报出
    # 「材料大量缺失（高）」，正是契约 IF-1.4 与 t_degrade 拦的那类回归。
    if not doc:
        return None, None, ["doc 缺失或为空 → 无法比对，coverage: partial"]

    # 形状归一（见 normalize_doc）：没有 type 的项按名称回查清单。
    # 一项都认不出来时**不做比对**——宁可标 partial，也不把"读不懂的材料表"
    # 当成"一份都没交"，那会凭空报出 R001（高）+ X007（高）。
    norm_doc, unresolved_names, absent = normalize_doc(doc, items)
    if not norm_doc and not absent:
        return None, None, [
            "doc[] 里 %d 项都没能识别出材料类型（形状不认识）→ 不做比对，"
            "coverage: partial" % len(doc)]

    present = _by_type(norm_doc)
    required, pending = required_items(items, stage, situations)
    # 只提醒**确实没交**的待定项——已交的材料不管情形怎么判都不缺件，报出来是噪音
    unresolved = sorted({c for p in pending if p["type"] not in present
                         for c in p["conds"]})

    # ① 缺件（总数 + 按类别拆 + 命中红线的件数）
    miss = [it for it in required if it["type"] not in present]
    miss_names = [it["名称"] for it in miss]
    redline_hit = 1 if any(it.get("红线") == "1" for it in miss) else 0
    miss_cat = {code: 0 for code in MISS_CATEGORIES}
    for it in miss:
        for code, cats in MISS_CATEGORIES.items():
            if it.get("类别") in cats:
                miss_cat[code] += 1
    doc_miss_redline_cnt = len([it for it in miss if it.get("红线") == "1"])

    # ② 过期。expire_date 为空 = 没有有效期，**不是过期**（清单 §5.1）。
    #    临期（≤30 天）与已过期分开，临期只提示、不算过期。
    #    日期优先；没给日期时用 `expired` 旗标兜底（评测方案形状里有这个字段）。
    expired, expired_key, expired_soon = [], [], []
    as_of_d = _date(as_of)
    soon_d = as_of_d + datetime.timedelta(days=EXPIRED_SOON_DAYS) if as_of_d else None
    for d in norm_doc:
        exp = _date(_value_of(d, "expire_date"))
        if exp is not None and as_of_d is not None:
            if exp < as_of_d:
                expired.append(d.get("type"))
                if d.get("type") in KEY_CERT_TYPES:
                    expired_key.append(d.get("type"))
            elif soon_d and exp <= soon_d:
                expired_soon.append(d.get("type"))
        elif d.get("expired") is True:
            expired.append(d.get("type"))
            if d.get("type") in KEY_CERT_TYPES:
                expired_key.append(d.get("type"))

    # ③ 跨材料矛盾：同一事实对不上，逐处计 1；同时记下**比对项**以便分类计数。
    #    任一侧缺值一律跳过——缺值是"没数据"，不是"对不上"。
    inconsis = []          # [(比对项, 描述)]
    for field, types, app_key in COMPARE_RULES:
        declared = _norm(applicant.get(app_key))
        for t in sorted(types):
            for d in present.get(t, []):
                got = _norm(_value_of(d, field))
                if got is not None and declared is not None and got != declared:
                    inconsis.append((field, "%s.%s=%s≠申报%s" % (t, field, got, declared)))
        # 材料之间互比（≥2 份材料都写了这个字段时）
        seen = [(t, _norm(_value_of(d, field)))
                for t in sorted(types) for d in present.get(t, [])]
        seen = [(t, v) for t, v in seen if v is not None]
        for i in range(len(seen)):
            for j in range(i + 1, len(seen)):
                if seen[i][1] != seen[j][1]:
                    inconsis.append((field, "%s.%s≠%s.%s"
                                     % (seen[i][0], field, seen[j][0], field)))

    # ③b 日期倒挂（清单 §5.2「日期」行）：合同签订日晚于放款日、或早于申请日。
    #     两个日期任一缺值 → 不判（缺值是"没数据"，不是"矛盾"）。
    disburse_d = _date(applicant.get("disburse_date") or applicant.get("借款日期")
                       or applicant.get("loan_date"))
    apply_d = _date(applicant.get("apply_date") or applicant.get("申请日期"))
    for d in norm_doc:
        sd = _date(_value_of(d, "sign_date"))
        if sd is None:
            continue
        if disburse_d and sd > disburse_d:
            inconsis.append(("date", "%s.sign_date=%s>放款日%s"
                             % (d.get("type"), sd, disburse_d)))
        if apply_d and sd < apply_d:
            inconsis.append(("date", "%s.sign_date=%s<申请日%s"
                             % (d.get("type"), sd, apply_d)))

    # 材料**一项可比对字段都没带**（如评测方案形状 {name, present, expired}）→
    # 矛盾项是「无法判定」，不是「没有矛盾」。填 0 会让 R004/R008 静默不命中、
    # 却看起来像"已经查过"，填 None 才是实话（契约 IF-1.4）。
    comparables_seen = any(_value_of(d, f) not in (None, "")
                           for d in norm_doc
                           for f in ("name", "id_no", "area", "amount", "sign_date"))
    inconsis_by = ({f: len([1 for it in inconsis if it[0] == f])
                    for f in INCONSIST_CODES} if comparables_seen
                   else {f: None for f in INCONSIST_CODES})
    inconsis_cnt = len(inconsis) if comparables_seen else None
    if not comparables_seen:
        notes.append("材料未带可比对字段（name/id_no/area/amount/sign_date）"
                     "→ 矛盾项无法判定，相关规则跳过")

    # ④ 身份证核验：在有效期内 且 与申请人一致。
    #    **读不到姓名是"没数据"，不是"对不上"**——一律判 0 会凭空报 R005（高）。
    id_valid = None
    for d in present.get("ID_CARD", []):
        exp = _date(_value_of(d, "expire_date"))
        got = _norm(_value_of(d, "name")) or _norm(d.get("holder"))
        want = _norm(applicant.get("name"))
        date_ok = (exp is None) or (as_of_d is None) or (exp >= as_of_d)
        if not date_ok:
            id_valid = 0                       # 过期 → 明确不通过
        elif got is not None and want is not None:
            id_valid = 0 if (id_valid == 0 or got != want) else 1
        # 姓名读不到 → 保持 None：无法核验 ≠ 核验不通过

    # ⑤ 土地权属：面积偏差是否在 ±5% 内。任一侧缺值 → None
    #    偏差比例同时回传，供「接近容差」的苗头规则使用。
    land_right_match, land_area_dev = None, None
    declared_area = _num(applicant.get("declared_area"))
    for d in present.get("LAND_CERT", []):
        got = _num(_value_of(d, "area"))
        if got is None or declared_area in (None, 0):
            continue
        land_area_dev = round(abs(got - declared_area) / declared_area, 4)
        land_right_match = 1 if land_area_dev <= AREA_TOLERANCE else 0

    # ⑥ 签章齐全性。只看**已提交**的应签章材料——没交属于缺件，另行计数。
    sign_complete, sign_missing_cnt = None, 0
    for t in sorted(SIGN_ITEMS):
        for d in present.get(t, []):
            f = d.get("fields") or {}
            if f.get("sign_complete") is False:
                ok = False
            elif f.get("sign_complete") is True:
                ok = True
            elif (_num(f.get("seal_count")) is not None
                  or _num(f.get("seal_required")) is not None):
                need = _num(f.get("seal_required"))
                got = _num(f.get("seal_count"))
                ok = not (need is not None and got is not None and got < need)
            else:
                # 完全没带签章信息（如 {name, present, expired} 形状）→ 无法判定。
                # **不要默认"齐全"**：那会把"没查"说成"查过且没问题"。
                continue
            if sign_complete is None:
                sign_complete = 1
            if not ok:
                sign_complete, sign_missing_cnt = 0, sign_missing_cnt + 1

    # ⑦ 清单未覆盖的材料：**不当作缺件**（可能是清单还没收录的新材料），
    #    只报出来供迭代（清单 §8）。两个来源：
    #      ① 名称回查不到清单的（unresolved_names）
    #      ② type 码不在清单里的 —— 需要 known_types，缺了就不判，
    #         避免把 §1.1/§2.1/§3.1 附表里的 type 误判成"未知"。
    unknown_types = sorted({d["type"] for d in norm_doc
                            if known_types and d["type"] not in known_types})
    doc_unknown_type_cnt = len(unresolved_names) + len(unknown_types)

    # ⑧ 首贷检查超期（2020贷后办法第十五条：发放后 3 个月内）。
    #    只拿到检查日才判超期；记录整份没交 → 交给缺件规则 R017，不重复报。
    first_check_overdue_days = None
    if stage == "贷后" and disburse_d:
        due = _add_months(disburse_d, FIRST_CHECK_MONTHS)
        for d in present.get("FIRST_POST_CHECK", []):
            done = _date(_value_of(d, "check_date")) or _date(_value_of(d, "sign_date"))
            if done:
                first_check_overdue_days = max(0, (done - due).days)

    # ⑨ 现场检查频次（2020贷后办法第二十一条(二)）：按额度与担保方式分档。
    #    口径简化一：监测期不足一年时按「每年至少 N 次」直接比 N 次，不做年度折算。
    #    口径简化二：「贷款期限超过三年的，从第四年开始每年至少一次」未实现
    #                （input 里没有合同期限/到期日，不臆测）。
    #    低于起档额度的属"现场抽查"（按管理户数比例），没有固定次数 → None。
    onsite_check_shortfall = None
    if stage == "贷后":
        amt = _num(applicant.get("declared_amount"))
        gua = str(applicant.get("guarantee_type") or "")
        bar = credit_bar(gua)
        req = None
        if amt and bar:
            for lo, hi, n in ONSITE_CHECK_TIERS[bar]:
                # 端点严格照条文：> 下界（「不含」）、<= 上界（「（含）」）
                if amt > lo and (hi is None or amt <= hi):
                    req = n
                    break
        if req == 2 and any(w in gua for w in ONSITE_CHECK_EXEMPT):
            req = 1
        if req is not None:
            onsite_check_shortfall = max(0, req - len(present.get("POST_CHECK_FORM", [])))

    # ⑩ 面谈留痕（2020办法第十八条）。只判贷前；非现场核实有豁免，
    #    故这是"未留痕"的事实，不是"违规"的判定。
    interview_missing = None
    if stage == "贷前":
        interview_missing = 0 if present.get("INTERVIEW_RECORD") else 1

    # ⑪ 受托支付合同金额（2020办法第二十五条）：购销合同金额低于申请额度即不足。
    entrust_amt_short, declared_amount = None, _num(applicant.get("declared_amount"))
    for d in present.get("PURCHASE_CONTRACT", []):
        amt = _num(_value_of(d, "amount"))
        if amt is None or declared_amount in (None, 0):
            continue
        entrust_amt_short = 1 if amt < declared_amount else 0

    metrics = {
        "doc_miss_cnt": len(miss),
        "doc_miss_redline_cnt": doc_miss_redline_cnt,
        "doc_miss_identity_cnt": miss_cat["doc_miss_identity_cnt"],
        "doc_miss_income_cnt": miss_cat["doc_miss_income_cnt"],
        "doc_miss_use_cnt": miss_cat["doc_miss_use_cnt"],
        "doc_miss_guarantee_cnt": miss_cat["doc_miss_guarantee_cnt"],
        "doc_miss_biz_cnt": miss_cat["doc_miss_biz_cnt"],
        "doc_miss_contract_cnt": miss_cat["doc_miss_contract_cnt"],
        "doc_miss_postcheck_cnt": miss_cat["doc_miss_postcheck_cnt"],
        "doc_miss_archive_cnt": miss_cat["doc_miss_archive_cnt"],
        "doc_expired_cnt": len(expired),
        "doc_expired_key_cnt": len(expired_key),
        "doc_expired_soon_cnt": len(expired_soon),
        "doc_inconsist_cnt": inconsis_cnt,
        "doc_inconsist_name_cnt": inconsis_by["name"],
        "doc_inconsist_idno_cnt": inconsis_by["id_no"],
        "doc_inconsist_area_cnt": inconsis_by["area"],
        "doc_inconsist_amount_cnt": inconsis_by["amount"],
        "doc_inconsist_date_cnt": inconsis_by["date"],
        "doc_sign_missing_cnt": sign_missing_cnt,
        "doc_unknown_type_cnt": doc_unknown_type_cnt,
        "doc_land_area_dev": land_area_dev,
        "doc_interview_missing": interview_missing,
        "doc_first_check_overdue_days": first_check_overdue_days,
        "doc_onsite_check_shortfall": onsite_check_shortfall,
        "doc_entrust_amt_short": entrust_amt_short,
        "id_valid": id_valid,
        "land_right_match": land_right_match,
        "sign_complete": sign_complete,
    }
    facts = {
        "miss_types": ",".join(miss_names),
        "miss_cnt": len(miss),
        "redline_hit": redline_hit,
        "expired_types": ",".join(sorted(set(t for t in expired if t))),
        "expired_key_types": ",".join(sorted(set(t for t in expired_key if t))),
        "inconsist": ";".join(t for _, t in inconsis),
        "stage": stage,
    }
    if unknown_types:
        facts["unknown_types"] = ",".join(unknown_types)
    if unresolved_names:
        # 认不出的材料名称**不是缺件**（清单 §8）。列出来是为了让它可见——
        # 静默丢掉正是"交了的材料被判成缺件"那个 bug 的根。
        facts["unmapped_materials"] = ",".join(unresolved_names[:10]) + \
            ("…" if len(unresolved_names) > 10 else "")
        notes.append("%d 项材料名称没能对上清单 → 未计入缺件，也不当作已交：%s"
                     "（如确认是新材料，应补进清单）"
                     % (len(unresolved_names),
                        ",".join(unresolved_names[:6])
                        + ("…" if len(unresolved_names) > 6 else "")))
    if absent:
        facts["marked_absent"] = ",".join(absent[:10])
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
    order = ["miss_types", "miss_cnt", "redline_hit", "expired_types",
             "expired_key_types", "unknown_types", "unmapped_materials",
             "marked_absent", "inconsist", "stage"]
    return "facts@doc|" + "|".join("%s=%s" % (k, facts[k])
                                   for k in order if facts.get(k) not in (None, ""))


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
                                      items, situations, as_of,
                                      known_types=parse_type_codes(args.checklist))
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
