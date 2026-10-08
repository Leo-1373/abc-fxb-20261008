---
name: sk_ew
description: 农户贷款贷后动态预警——监测时点序列、预警信号升级/解除、处置建议。
version: 1.0.0
contract: v1
input_slots: [monitor_ts[], signals, loan{}, stage]
output_keys: [alerts[]]
rules_required:
  dims: [ew]
  ids: []
refs_index:
  - path: references/贷后预警信号.md
    when: 需要信号定义、升级/解除条件或处置口径时
  - path: references/rules.closure.md
    when: 需要本包完整规则清单时
max_reads: 2
---

# sk_ew 贷后动态预警

在**时间序列**上判预警，与 `sk_rules` 的快照判定本质不同：信号有**持续性**，
会随监测时点**升级或解除**。

## 何时使用

- 用户提到：贷后、预警、监测、处置、回访、信号、存量
- 输入中存在：`monitor_ts[]`（监测时点序列）
- **不适用**：只有单一时点的静态数据 → 走 `sk_rules` 快照判定

## 输入

| 槽 | 形状 | 必需 | 说明 |
|---|---|---|---|
| `monitor_ts[]` | 对象数组 | 是 | 每项 `{date, overdue_days, balance, repay_delay_cnt, use_dev_flag, biz_abnormal, contact_fail, natural_disaster, price_shock, guarantee_deplete}` |
| `signals` | str[] | 否 | 外部已上报的信号（如客户经理回访记录），用于补充 |
| `loan{}` | 对象 | 否 | `{amount, balance, due_date, rate}`，用于处置建议的量化 |
| `stage` | str | 否 | 缺省贷后 |

## stage_adaptation

| 环节 | 侧重 | 口径调整 |
|---|---|---|
| 贷前 | —— | 本 skill 不适用贷前（预警是贷后概念） |
| 贷中 | —— | 同上 |
| 贷后 | 信号升级/解除、处置优先级排序 | 关注**连续两个时点**的趋势，单点异常不升级 |

## 工作流

1. 按 `date` 升序排列 `monitor_ts[]`
2. **逐时点求值**内联规则 → 每个时点的命中信号集合
3. **时序比对**（这是本 skill 的核心，区别于快照判定）：
   - 信号在连续 ≥2 个时点出现 → 升级一档
   - 信号在最近 2 个时点消失 → 标记"拟解除"，但不自动解除，标注待人工确认
   - 逾期档位跨档（1–30 → 31–90 → 90+）→ 强制升级
4. 按当前最新时点输出 `alerts[]`，附升级轨迹

> P3 应补 `scripts/ew.py`：步骤 1/3 是确定性时序比对，**不该让模型逐时点读**——
> 时点数多时上下文会爆，且跨档判定容易被模型算错。

## 输出

```
A|E002|高|逾期超30天|升级|建议提前收贷
A|E004|高|资金用途偏离|持续|要求说明资金去向
A|E007|低|余额大幅下降|拟解除|继续观察
---
E002|ev=ew.overdue_days=45;ew.升级轨迹=正常>1-30>31-90|basis=贷后预警信号1|conf=0.95
E004|ev=ew.use_dev_flag=1;ew.连续时点=3|basis=贷后预警信号3|conf=0.9
```

## 内联规则（top-N 高频）

<!--INLINE_RULES-->

## 边界

| 不做 | 归属 |
|---|---|
| 不做贷前准入判定 | `sk_rules` |
| 不重算收入稳定性、不看贷前流水 | `sk_cash` |
| 不做跨维度快照定级 | `sk_rules`（契约 D2） |
| 不直接输出最终报告 | 主智能体 |

**定级纪律**：本 skill 的等级反映**单笔贷款在时间轴上的恶化程度**。若需与征信、材料
交叉（如"逾期且新增多头"），输出两个维度的事实，由 `sk_rules` 合成。

## refs

| 文件 | 何时读 |
|---|---|
| `references/贷后预警信号.md` | 需要信号定义、升级/解除条件时 |
| `references/rules.closure.md` | 需要完整规则清单时 |
