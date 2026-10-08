---
name: sk_cash
description: 农户贷款银行流水分析——收入稳定性、异常交易、资金用途偏离、还款来源核查。
version: 1.0.0
contract: v1
input_slots: [txn[], period, purpose, declared_inc, stage]
output_keys: [findings@cash[], income_est{}]
rules_required:
  dims: [cash]
  ids: []
refs_index:
  - path: references/流水分析要点.md
    when: 需要某个指标的计算口径或阈值依据时
  - path: references/rules.closure.md
    when: 需要本包完整规则清单时
max_reads: 2
---

# sk_cash 流水分析

把银行流水（高基数交易序列）压成**可判定的短码指标**，再按规则库输出风险点。
**原始交易明细永不进入模型上下文**——先由 `scripts/analyze.py` 聚合成统计量。

## 何时使用

- 用户提到：流水、银行流水、交易、进出账、现金流、还款来源
- 输入中存在：`txn[]`（交易数组）
- **不适用**：只有征信字段没有交易明细 → 交给 `sk_cred`

## 输入

| 槽 | 形状 | 必需 | 说明 |
|---|---|---|---|
| `txn[]` | 对象数组 | 是 | 每项 `{date, amt, counterparty, desc, channel, type}`。`amt` 正入负出 |
| `period` | `{from,to}` | 否 | 统计期。缺省则取 `txn` 覆盖的全部月份 |
| `purpose` | str | 是 | 申报贷款用途，用于用途偏离判定 |
| `declared_inc` | float | 否 | 申报月收入，用于交叉对账（透传给 `sk_rules`） |
| `stage` | str | 否 | 贷前/贷中/贷后，缺省贷前 |

`txn` 缺失或为空 → 输出空 findings + `coverage: partial`，**不臆测**。

## stage_adaptation

| 环节 | 侧重 | 口径/阈值调整 |
|---|---|---|
| 贷前 | 还款**来源**是否真实稳定 | 阈值从严（`inc_cv` 0.5、`top1_share` 0.6） |
| 贷中 | 用信**条件**是否满足、用途是否可追踪 | 同贷前 |
| 贷后 | 资金**流向**是否偏离、经营是否延续 | 阈值放宽（`inc_cv` 0.7、`top1_share` 0.7），关注趋势而非绝对值 |

## 工作流

1. **先跑脚本聚合**——`txn` 直接喂给脚本，不进上下文：

```bash
PYTHONIOENCODING=utf-8 python scripts/analyze.py aggregate \
    --input txn.json --out metrics.json
```

2. **再跑脚本求值**——规则命中由脚本判定，不由模型判：

```bash
PYTHONIOENCODING=utf-8 python scripts/analyze.py evaluate \
    --metrics metrics.json --closure references/rules.closure.md --stage 贷前
```

3. **模型只做脚本做不到的事**：对脚本判出的高风险项补充业务解释，
   以及识别脚本未覆盖的**非量化**异常（如交易对手名称含敏感行业词）。

4. 组装 L1/L2 返回。

> 脚本不可用时（平台无执行能力）→ 按下方内联规则，用指标人工比对，并在输出标注 `coverage: partial`。

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
F|R101|高|核实收入波动原因|贷前
F|R105|高|单一对手依赖|贷前
F|R104|中|交易对手集中|贷前
---
R101|ev=cash.inc_cv=0.62;cash.gap_cnt=4|basis=流水分析要点3|conf=0.9
R105|ev=cash.top1_share=0.88|basis=流水分析要点7|conf=0.9
```

`income_est{}` 一并回传，供 `sk_rules` 做三方对账：

```
income_est|inc_mean=8200|inc_cv=0.62|n_month=6
```

## 内联规则（top-N 高频，保证无脚本时仍可判定）

<!--INLINE_RULES-->

## 边界

| 不做 | 归属 |
|---|---|
| 不判"申报收入与流水不符"的**准入风险** | `sk_rules`（`X001`/`X002`）。本 skill 只提供 `income_est` 与 `inc_cv` 事实 |
| 不做**跨维度定级**、不与其他维度结论合并 | `sk_rules`（契约 D2，防止同一客户定级漂移） |
| 不看征信负债率、多头借贷 | `sk_cred` |
| 不核验材料真伪、不缺件检查 | `sk_doc` |
| 不判贷后逾期等级、不做处置建议 | `sk_ew` |

**定级纪律**：本 skill 只对**流水领域内的原子风险点**定级。凡需与征信、材料交叉才能得出的结论，
一律输出事实（指标值）并交由 `sk_rules` 判定。

## refs

| 文件 | 何时读 |
|---|---|
| `references/流水分析要点.md` | 需要指标计算口径或阈值依据时 |
| `references/rules.closure.md` | 需要本包完整规则清单时（脚本已内嵌解析） |
