---
name: sk_cred
description: 农户贷款征信与负债评估——负债率、逾期画像、多头借贷、对外担保。
version: 1.0.0
contract: v1
input_slots: [credit{}, debt[], declared_inc, stage]
output_keys: [findings@cred[], factors{}]
rules_required:
  dims: [cred]
  ids: []
refs_index:
  - path: references/征信审查要点.md
    when: 需要指标口径或监管阈值依据时
  - path: references/rules.closure.md
    when: 需要本包完整规则清单时
max_reads: 2
---

# sk_cred 征信与负债

从征信字段算出**风险因子**，输出领域内原子风险点。因子同时被 `sk_rules`（交叉对账）消费。

## 何时使用

- 用户提到：征信、负债率、逾期、多头、担保、贷记卡、查询次数
- 输入中存在：`credit{}` 或 `debt[]`
- **不适用**：需要与流水收入交叉对账 → 由 `sk_rules` 的 `X003` 承接

## 输入

| 槽 | 形状 | 必需 | 说明 |
|---|---|---|---|
| `credit{}` | 对象 | 是 | 征信报告直取字段，分组见下表。**键缺失 ≠ 值为 0**：缺失一律 `None` |
| `debt[]` | 对象数组 | 否 | 每笔 `{org, type, balance, monthly_pay, remain_months}`，用于 `dti`、`multi_lend`、负债结构 |
| `declared_inc` | float | 是 | 申报月收入，`dti` 的分母 |
| `stage` | str | 否 | 缺省贷前 |

`credit{}` 直取字段分组：

| 分组 | 键 |
|---|---|
| 逾期 | `overdue_cur`（当前未结清笔数） `overdue_24m` `overdue_max_days` |
| 查询 | `query_3m`（**仅**贷款/信用卡审批类） `query_self_3m`（本人查询） |
| 贷记卡 | `card_util` `card_cnt` `semi_card_overdraft` |
| 负债与结构 | `guarantee_bal` `guarantee_cnt` `co_borrower_debt` `new_loan_3m` `new_loan_amt_3m` `new_org_6m` `settled_recent` `settled_amt_recent` `extension_cnt` `refinance_flag` |
| 记录类 | `credit_hist_len` `bad_debt_flag` `dishonest_flag` `lawsuit_flag` `five_level_bad` `guarantee_overdue_flag` `co_borrower_dishonest` |

`debt[].type` 用"信用/保证/抵押/质押"（同义英文亦可）；"保证"计入无抵押敞口。
`debt[].remain_months` 缺省 → `short_term_ratio = None`。

**三个必须区分的语义**（混淆会导致最严重的误判）：

| 情况 | 含义 | 处理 |
|---|---|---|
| 键缺失（`credit` 里没有这个字段） | 数据不可得 | 指标 `None` → 规则跳过 + 标 `partial` |
| `debt` 键缺失 | **没给**在贷明细 | `multi_lend`/`dti` = `None`（不是 0） |
| `debt: []` | 确实**没有**负债 | `multi_lend = 0`、`dti = 0.0` |

`declared_inc` 缺失 → `dti` 无法计算，相关规则跳过并在输出标注 `coverage: partial`。

## stage_adaptation

| 环节 | 侧重 | 口径/阈值调整 |
|---|---|---|
| 贷前 | 准入：是否触碰禁入类逾期 | **从严**（`dti` 0.5）；红线项（R215/R216/R217）必须人工复核 |
| 贷中 | 审批：负债结构与还款压力 | 规则库按此档编码（`dti` 0.55） |
| 贷后 | 存量：负债是否恶化、是否新增多头 | **放宽**（`dti` 0.6），关注**环比变化**而非绝对值 |

> 规则库的 `cond` **固定按"贷中"口径编码**，与 `rules/dict.yaml` 的 `thresholds` 贷中列一致。
> 贷前从严、贷后放宽由 `sk_rules` 结合 `factors` 行回传的 `dti` 与 `dti_threshold` 判定。
> **本 skill 不改写规则**——否则同一份规则会在不同环节给出两套互相矛盾的等级。

## 工作流

1. **先跑脚本聚合**（`debt[]` 明细不进上下文，只回传短码指标）：

```bash
PYTHONIOENCODING=utf-8 python scripts/cred.py aggregate \
    --input credit.json --out metrics.json
```

2. **再跑脚本求值**（规则命中由脚本判定，不由模型判）：

```bash
PYTHONIOENCODING=utf-8 python scripts/cred.py evaluate \
    --metrics metrics.json --closure references/rules.closure.md --stage 贷前
```

3. **模型只做脚本做不到的事**：解释高风险项的业务含义；核实诉讼性质、
   展期原因、结清资金来源等**非量化**事项（依据见 `references/征信审查要点.md`）。

4. 组装 L1/L2 + `factors` 返回。

> **自测**：`PYTHONIOENCODING=utf-8 python scripts/cred.py selftest`
> 应显示"通过：N 项断言"（N 随用例增加而增长，**不要把具体数字抄进文档**——
> 抄了必然过期，本项目已踩过这个坑）。内含两条关键守卫：
> ① **空输入必须零风险点**（禁止用 `0` 顶替算不出来的指标）；
> ② **部分和不等于总额**——任一明细缺 `balance`／`org` 时，总额与多头家数必须是 `None`
> 而非"少加一点"的数，否则会把高负债客户静默报成低风险（详见要点9）。
>
> 脚本不可用时（平台无执行能力）→ 按下方内联规则人工比对，并标 `coverage: partial`。

## 输出

**L1 摘要回流**（默认只回传这层，一行一条）：

```
F|<id>|<level>|<title>|<stage>
```

**L2 证据明细**（留本 skill 侧，主智能体按 id 索取时才返回）：

```
<id>|ev=<源>.<定位>=<值>|basis=<出处>|conf=<0-1>
```

示例：

```
F|R215|高|当前存在逾期|贷前
F|R201|高|负债率超限|贷前
F|R224|高|负债几乎无抵押|贷前
F|R204|中|存在多头借贷迹象|贷前
F|R220|低|本人查询过频|贷前
---
R215|ev=cred.overdue_cur=1|basis=征信审查要点4|conf=0.9
R201|ev=cred.dti=0.58|basis=征信审查要点2|conf=0.9
R224|ev=cred.unsecured_ratio=1.0|basis=征信审查要点12|conf=0.9
R204|ev=cred.multi_lend=3|basis=征信审查要点9|conf=0.9
coverage|full
```

**降级标注**（契约 IF-1.4 / IF-5.5）：有指标算不出来时必须显式声明，
被跳过的规则**绝不静默消失**：

```
coverage|partial|missing=declared_inc,dti
```

`missing` 里可能出现的码与含义（**每个码都对应一个"故意不给数"的决定**）：

| 码 | 含义 | 受影响的指标 |
|---|---|---|
| `debt[]` | 未提供在贷明细，或形状不是数组 | `multi_lend`、`dti`、`debt_balance`、`total_debt`、结构比率 |
| `debt[].item` | 有明细项不是对象（已丢弃但标记） | 各项合计 |
| `debt[].org` | 有明细项缺机构名 | **`multi_lend`**（否则静默低估多头家数） |
| `debt[].balance` | 有明细项缺余额 | **`debt_balance`／`total_debt`**（部分和不等于总额） |
| `debt[].monthly_pay` | 有明细项缺月供 | **`dti`**（分子不完整） |
| `debt[].remain_months` | 全部明细缺剩余期限 | `short_term_ratio` |
| `declared_inc` | 申报月收入缺失或非正 | **`dti`**（分母） |

**出现 partial 时，结论必须标注"数据不全"、不得给出确定性结论。**

`factors{}` 一并回传，供 `sk_rules` 做三方对账与总额汇总（`X003` 判"总负债超年收入"需要）：

```
factors|dti=0.58|multi_lend=5|monthly_pay=4640.0|debt_balance=186000.0|total_debt=236000.0|unsecured_ratio=0.72|short_term_ratio=0.61|guarantee_bal=50000.0|guarantee_cnt=2|co_borrower_debt=0.0|overdue_cur=1|new_loan_3m=2|new_org_6m=1|stage=贷前|dti_threshold=0.5
```

> `dti_threshold` 是该环节适用的 DTI 审慎线（取自 `dict.yaml` 的 `thresholds`）。
> 规则库按贷中口径编码，`sk_rules` 用这两个数判断是否需要按环节升降档。

## 内联规则（top-N 高频）

<!--INLINE_RULES-->

## 边界

| 不做 | 归属 |
|---|---|
| 算并回传 `total_debt`，但**不判**"总负债超年收入" | `sk_rules`（`X003`）。本 skill 只**提供数值** |
| 不判"申报收入与流水不符" | `sk_rules`（`X001`）。本 skill 不持有流水数据 |
| 不识别农业季节性、不算流水指标 | `sk_cash`。本 skill 只回传 `dti_threshold` 供其参考 |
| 不把对外担保、共借人负债并入 `dti` | 本 skill 按要点13/14 **独立定级**；总额由 `sk_rules`（`X003`）汇总 |
| 不看材料齐备性 | `sk_doc` |
| 不做跨维度定级 | `sk_rules`（契约 D2） |

**为什么要写明"不并入 dti"**：对外担保和共借人负债若并入 `dti`，
会让同一份负债同时命中 R201（高）与 R211/R229，形成
"同一个现象两个等级"的矛盾——`sk_rules` 汇总时定级会漂移。

**定级纪律**：`cred` 维度等级只反映**征信本身的严重度**。凡需与收入/流水/材料交叉的结论，
输出因子即可，判定上交 `sk_rules`。同一变量只用**互不重叠**的区间分档
（如 R224 `>0.9` 高 / R225 `0.7–0.9` 中），禁止对同一区间给出两个等级。

## refs

| 文件 | 何时读 |
|---|---|
| `references/征信审查要点.md` | 需要指标口径或监管阈值依据时 |
| `references/rules.closure.md` | 需要完整规则清单时 |
