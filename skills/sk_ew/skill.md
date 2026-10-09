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

1. **先跑脚本做时序比对**——历史时点不进上下文，脚本只回传最新时点的状态：

```bash
PYTHONIOENCODING=utf-8 python scripts/ew.py trend \
    --input monitor.json --signals references/贷后预警信号.md --out trend.json
```

2. **再跑脚本求值**——规则命中 + 时序升级后的等级都在脚本里定：

```bash
PYTHONIOENCODING=utf-8 python scripts/ew.py evaluate \
    --trend trend.json --closure references/rules.closure.md
```

3. **时序判定由脚本负责**（区别于快照判定，这是本 skill 的核心）：
   - 信号连续 ≥2 个时点出现 → 升级一档；连续 ≥3 个 → 进强制处置通道
   - 信号在最近 2 个时点消失 → 标记「拟解除」，**不自动解除**，等人工确认
   - 逾期升到「次级」及以上 → 跨档强制升级（刚逾期不算跨档，避免误报）

4. 按最新时点输出 `alerts[]`，附升级轨迹。

> 规则只判"现在什么情况"，脚本判"这情况在往哪走"——现有规则全是单点快照，
> **能表达"信号在恶化"的只有脚本这一层**。脚本不可用时 → 按内联规则人工判最新时点，
> 并标注 `coverage: partial`（时序升级无法人工可靠复现）。

## 输出

**L1 预警行**：`A|<id>|<level>|<title>|<状态>|<建议>`

`状态` 取值：`新增`（最新时点才出现）/ `持续` / `升级`（连续 ≥3 时点或跨档）/ `拟解除`。

```
A|E002|高|逾期超30天|升级|上门催收并评估处置
A|E010|高|主营产品价格冲击|升级|评估收入影响
A|AD4|低|客户失联|拟解除|确认后解除预警
---
E002|ev=ew.overdue_days=52;ew.升级轨迹=正常>关注(1-30)>次级(31-90)|basis=贷后预警信号1|conf=0.95
E010|ev=ew.price_shock=1;ew.升级轨迹=正常>关注(1-30)>次级(31-90)|basis=贷后预警信号7|conf=0.95
```

**L2 证据明细**：`<id>|ev=<指针>|basis=<出处>|conf=<0-1>`

`id` 用 `AD<n>` 的行是**拟解除**的提示——这类信号在最新时点已不成立、命中不到任何规则，
但按 `贷后预警信号.md` §3.2 必须报出来等人工确认，**系统不擅自解除预警**。
主智能体收到后应转述为"该预警已消退，是否解除待确认"，**不得当作风险点计入等级合成**。

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
