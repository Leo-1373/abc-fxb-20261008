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
| `credit{}` | 对象 | 是 | `{overdue_24m, overdue_max_days, query_3m, card_util, credit_hist_len, settled_recent, guarantee_bal}` |
| `debt[]` | 对象数组 | 否 | 每笔 `{org, type, balance, monthly_pay}`，用于算 `dti`、`multi_lend` |
| `declared_inc` | float | 是 | 申报月收入，`dti` 的分母 |
| `stage` | str | 否 | 缺省贷前 |

`declared_inc` 缺失 → `dti` 无法计算，相关规则跳过并在输出标注 `coverage: partial`。

## stage_adaptation

| 环节 | 侧重 | 口径调整 |
|---|---|---|
| 贷前 | 准入：是否触碰禁入类逾期 | `dti` 阈值 0.5 从严 |
| 贷中 | 审批：负债结构与还款压力 | `dti` 阈值 0.55 |
| 贷后 | 存量：负债是否恶化、是否新增多头 | `dti` 阈值 0.6，关注**环比变化**而非绝对值 |

## 工作流

1. 由 `debt[]` 汇总：`multi_lend`（机构数）、月还款合计 → `dti = 月还款 / declared_inc`
2. 由 `credit{}` 直接取：`overdue_24m`、`overdue_max_days`、`query_3m`、`card_util`、
   `credit_hist_len`、`settled_recent`、`guarantee_bal`
3. 按内联规则判定等级，输出 findings
4. 回传 `factors{}` 供 `sk_rules` 做三方对账

> `dti`、`multi_lend` 是**确定性计算**。若笔数多，邱家杰（P5）应补 `scripts/cred.py`，
> 不要让模型累加金额——算错会直接导致等级判错。

## 输出

```
F|R203|高|多头借贷|贷前
F|R201|高|负债率超限|贷前
F|R209|中|查询次数偏多|贷前
---
R203|ev=cred.multi_lend=5|basis=征信审查要点9|conf=0.9
R201|ev=cred.dti=0.58;cred.declared_inc=8000|basis=征信审查要点2|conf=0.9
```

`factors{}` 回传（`sk_rules` 判 `X003` 需要）：

```
factors|dti=0.58|multi_lend=5|monthly_pay=4640|debt_balance=186000|guarantee_bal=50000
```

## 内联规则（top-N 高频）

<!--INLINE_RULES-->

## 边界

| 不做 | 归属 |
|---|---|
| 不算 `total_debt`、不判"总负债超年收入" | `sk_rules`（`X003`）。本 skill 只出 `factors` |
| 不判"申报收入与流水不符" | `sk_rules`（`X001`）。本 skill 不持有流水数据 |
| 不看材料齐备性 | `sk_doc` |
| 不做跨维度定级 | `sk_rules`（契约 D2） |

**定级纪律**：`cred` 维度等级只反映**征信本身的严重度**。凡需与收入/流水/材料交叉的结论，
输出因子即可，判定上交 `sk_rules`。

## refs

| 文件 | 何时读 |
|---|---|
| `references/征信审查要点.md` | 需要指标口径或监管阈值依据时 |
| `references/rules.closure.md` | 需要完整规则清单时 |
