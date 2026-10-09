---
name: sk_doc
description: 农户贷款申请材料核查——完整性、一致性、形式真伪。只报结构性事实，不判风险大小。
version: 1.0.0
contract: v1
input_slots: [doc[], applicant{}, stage]
output_keys: [findings@doc[], facts@doc{}]
rules_required:
  dims: [doc]
  ids: []
refs_index:
  - path: references/必备材料清单.md
    when: 需要判定缺件是否属于准入类红线时
  - path: references/rules.closure.md
    when: 需要本包完整规则清单时
max_reads: 2
---

# sk_doc 资料核查

对申请材料做**结构性核查**：缺不缺、对不对得上、是不是有效。产出「事实」而非「判定」。

## 何时使用

- 用户提到：材料、证件、身份证、营业执照、土地权证、合同、完整性、缺件
- 输入中存在：`doc[]`（材料清单）
- **不适用**：需要判断缺件带来的准入后果 → 由 `sk_rules` 的 `X007` 承接

## 输入

| 槽 | 形状 | 必需 | 说明 |
|---|---|---|---|
| `doc[]` | 对象数组 | 是 | 每项 `{type, no, issue_date, expire_date, holder, fields{}}` |
| `applicant{}` | 对象 | 是 | 申请人申报信息，用于跨材料比对 |
| `stage` | str | 否 | 贷前/贷中/贷后，缺省贷前 |

## stage_adaptation

| 环节 | 侧重 | 口径调整 |
|---|---|---|
| 贷前 | 准入材料是否齐全、能否受理 | 严格执行必备材料清单 |
| 贷中 | 放款前要件（合同、用途证明）是否完备 | 关注签章与日期逻辑 |
| 贷后 | 贷后检查记录、经营证明是否按期归档 | 关注时效性而非齐全性 |

## 工作流

1. 比对 `references/必备材料清单.md`（按环节取对应清单）→ 得 `doc_miss_cnt`
2. 逐项校验有效期 → `doc_expired_cnt`；身份证与申请人一致性 → `id_valid`
3. **跨材料比对同一事实**（姓名/面积/金额/日期）→ `doc_inconsist_cnt`
4. 土地权证面积 vs 申报面积 → `land_right_match`；签章齐全性 → `sign_complete`
5. 按内联规则判定等级，输出 findings

> 本 skill 目前无脚本。若材料量大（>20 项），建议真超奇补 `scripts/check.py` 做字段级比对，
> **比对是确定性的，不该让模型逐项看**。

## 输出

```
F|R004|高|核实材料矛盾项|贷前
F|R001|高|补齐材料后再受理|贷前
F|R003|中|更换有效证照|贷前
---
R004|ev=doc.inconsist_cnt=2;doc.土地面积=12亩vs申报15亩|basis=资料核查要点3|conf=0.9
```

同时回传 `facts@doc{}`（供 `sk_rules` 判 `doc_risk_link`）：

```
facts@doc|miss_types=土地权证,收入证明|miss_cnt=2|redline_hit=1
```

## 内联规则（top-N 高频）

<!--INLINE_RULES-->

## 边界

| 不做 | 归属 |
|---|---|
| 不判"缺件带来的**准入风险**" | `sk_rules`（`X007`）。事实 vs 判定的分界 |
| 不核查流水的真实性、不看交易 | `sk_cash` |
| 不看征信报告 | `sk_cred` |
| 不做跨维度定级 | `sk_rules`（契约 D2） |

**定级纪律**：`doc` 维度的等级只反映**材料问题的严重度**（缺件数、矛盾程度），
不反映"这笔贷款该不该批"——后者是 `sk_rules` 的职责。

## refs

| 文件 | 何时读 |
|---|---|
| `references/必备材料清单.md` | 判断缺件是否触及准入红线时 |
| `references/rules.closure.md` | 需要完整规则清单时 |
