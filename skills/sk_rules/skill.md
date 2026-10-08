---
name: sk_rules
description: 农户贷款风险规则引擎——跨维度交叉验证、风险等级合成、冲突消解与去重。定级的唯一发生地。
version: 1.0.0
contract: v1
input_slots: [features{}, findings[], declared_inc, income_est, collateral_val, apply_amount, stage]
output_keys: [risk{}]
rules_required:
  dims: [x]
  ids: []
refs_index:
  - path: references/交叉验证规则.md
    when: 需要三方对账或冲突消解的具体口径时
  - path: references/rules.closure.md
    when: 需要本包完整规则清单时
max_reads: 2
---

# sk_rules 风险规则引擎

**全链路唯一的定级发生地。** 汇聚各领域 skill 的 findings 与因子，做跨维度交叉验证，
合成整体风险等级。

> **为什么定级必须集中**：若定级散落在各 skill，同一个客户会在 `sk_cash` 和 `sk_cred`
> 里被定成不同等级。这是致命的评分硬伤。领域 skill 只对自己领域内的原子风险点定级，
> **一切跨维度结论由本 skill 产出**（契约 D2）。

## 何时使用

- 用户提到：风险点、审查、规则、命中、交叉、一致性、定级、矛盾
- **恒为汇聚点**：主智能体的路由算法在 P 含 C1/C2/C3/C5 时自动追加 C4 于末尾
- **不适用**：单一领域问题且无需定级 → 可直接由领域 skill 回答

## 输入

| 槽 | 形状 | 必需 | 说明 |
|---|---|---|---|
| `findings[]` | L1 摘要行数组 | 是 | 各领域 skill 的 `F\|id\|level\|title\|stage` |
| `features{}` | 对象 | 否 | 各 skill 回传的因子：`factors{}`、`income_est{}`、`facts@doc{}` |
| `declared_inc` | float | 是 | 三方对账基准 |
| `income_est` | float | 否 | 由 `sk_cash` 提供；缺失则 `X001` 跳过 |
| `collateral_val` | float | 否 | 抵押物评估值；缺失则 `X004`/`X005` 跳过 |
| `apply_amount` | float | 否 | 申请额度；缺失则 `X004`/`X005` 跳过 |
| `stage` | str | 否 | 缺省贷前 |

**缺失即跳过并标注**，不得用默认值臆测——臆测出的等级比没有等级更糟。

## stage_adaptation

| 环节 | 侧重 | 口径调整 |
|---|---|---|
| 贷前 | 准入判定：是否受理、需补什么 | 从严 |
| 贷中 | 审批决策：额度、担保、定价条件 | 中 |
| 贷后 | 预警处置：是否压降/提前收贷 | 关注趋势，允许"观察后复评" |

## 工作流

1. **汇总**：收集全部 findings，按 `id` 去重，同 id 多条取**最高等级**
2. **算跨维度派生量**：`inc_gap_ratio = declared_inc / income_est`、
   `total_debt = 征信在贷余额 + guarantee_bal`、`collat_ratio = collateral_val / apply_amount`
3. **求值跨域规则**（`X00x` 系列，见内联规则）
4. **冲突消解**：若某维度输出与另一维度对同一事实判定相反 → 置 `dim_conflict=1`，
   命中 `X006`，整体强制 `高`
5. **等级合成**：按 `references/rules.closure.md` 的 `level_synthesis` 段
6. 输出 `risk{}`

> P2 应补 `scripts/synthesize.py`：把步骤 1/2/5 做成确定性脚本。
> **等级合成是纯查表逻辑，不该让模型算**——否则同一输入两次运行可能给不同等级，
> 破坏幂等性（契约测试 C-幂等）。

## 输出

```
risk|level=高|pending=0
points|X001|高|申报收入高于流水测算
points|X004|高|抵押覆盖不足
points|R203|高|多头借贷
---
X001|ev=x.inc_gap_ratio=1.9;declared_inc=8000;income_est=4200|basis=交叉验证规则1|conf=0.9
X004|ev=x.collat_ratio=0.7;collateral_val=70000;apply_amount=100000|basis=交叉验证规则4|conf=0.9
synthesis|high_cnt=3|mid_cnt=2|low_cnt=1|conflict=0|rule=任一条高则整体高
```

`pending=1` 表示存在 `conf<0.6` 的命中项，等级需人工复核（合成规则末尾一条）。

## 内联规则（top-N 高频 · 跨域）

<!--INLINE_RULES-->

## 边界

| 不做 | 归属 |
|---|---|
| 不重算领域指标（`inc_cv`、`dti` 等） | 由 `sk_cash`/`sk_cred` 提供。重算即违反 DRY，且会与领域 skill 结果不一致 |
| 不读原始流水、原始征信 | 只消费 findings 与因子 |
| 不写最终报告、不做措辞 | 主智能体。本 skill 只出 `risk{}` |
| 不判贷后**时序**预警（升级/解除） | `sk_ew`。本 skill 做**快照**判定 |

**定级纪律**：本 skill 是唯一可以产出"跨维度等级"的地方。领域 skill 上交的等级只作为
输入证据，最终 `risk.level` **只由本 skill 的合成规则产出**。

## refs

| 文件 | 何时读 |
|---|---|
| `references/交叉验证规则.md` | 需要三方对账口径或冲突消解细则时 |
| `references/rules.closure.md` | 需要完整规则清单与 `level_synthesis` 段时 |
