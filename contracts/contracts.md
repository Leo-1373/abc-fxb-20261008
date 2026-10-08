# 接口契约 IF-1..5（v1 · 冻结）

> **本文件是全员并行的唯一前提。** 任何 skill 不得绕过本契约自行定义字段、等级或输出格式。
> 变更流程：提出方 → P1 评审 → 版本号 +0.1 → 全员同步。**冻结期内只增字段不改语义。**

约定符号：`[]` 数组，`{}` 对象，`@` 命名空间限定，`|` 制表符分隔。

---

## IF-1 · finding（风险点）schema

### 1.1 等级标尺（唯一，全体共用）

| 值 | 含义 | 处置倾向 |
|---|---|---|
| `高` | 可能导致拒贷/压降/提前收贷 | 必须人工复核 |
| `中` | 需补充调查或增加担保 | 补充材料后可继续 |
| `低` | 提示性，不阻断 | 记录备查 |

**禁止**各 skill 自造等级（如"严重/一般/轻微"）。**等级合成只在 `sk_rules` 发生**（见 D2 双层模型）。

### 1.2 finding 对象

```json
{
  "id": "R017",
  "dim": "cash",
  "level": "高",
  "title": "收入波动异常",
  "ev": ["txn.2025-03.amt_cv=0.62", "txn.gap_cnt=4"],
  "basis": "流水分析要点3",
  "stage": ["贷前", "贷后"],
  "conf": 0.85
}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | str | 规则库 id；无对应规则时为 `AD<n>`（ad-hoc 自由发现） |
| `dim` | str | 维度短码，见 `rules/dict.yaml`：`doc`/`cash`/`cred`/`x`(跨域)/`ew` |
| `level` | str | 只能是 `高`/`中`/`低` |
| `title` | str | **≤12 字**，名词短语，不写句子 |
| `ev` | str[] | **证据指针**，格式 `<源>.<定位>=<值>` 或 `<源>.<定位>`。**禁止自然语言句子** |
| `basis` | str | 依据出处（规则库 `basis` 列 / 监管条款号） |
| `stage` | str[] | 适用环节，取值 `贷前`/`贷中`/`贷后` |
| `conf` | float | 0–1。`<0.6` 时 `sk_rules` 应降级处理并标注 |

**`ev` 为什么强制指针格式**：证据会回流进主智能体上下文。自由文本证据每条 30–80 token，指针格式每条 5–15 token，且可被脚本校验、可被 `eval/contract_tests.py` 断言。

### 1.3 双层输出协议（省 token 的核心机制）

每个 skill 对外产出**两层**：

**L1 摘要回流**（进主智能体上下文，一行一条，制表符分隔）：
```
F|R017|高|收入波动异常|贷前,贷后
F|R021|中|交易对手集中|贷前
```

**L2 证据明细**（留 skill 侧，主智能体按需索取）：
```
R017|ev=txn.2025-03.amt_cv=0.62;txn.gap_cnt=4|basis=流水分析要点3|conf=0.85
R021|ev=txn.top1_share=0.71|basis=流水分析要点7|conf=0.72
```

**索取规则**：主智能体默认只收 L1。仅当 finding 的 `level=高`，或需要写入最终报告证据链时，才向该 skill 索取对应 id 的 L2。**低风险 finding 的明细永不进主上下文。**

### 1.4 指标缺失约定（全员强制）

**指标无法计算时一律置 `None`，绝不可用 `0` 代替。**

引用到 `None` 指标的风险点**必须跳过**，并在输出标注 `coverage: partial` + 缺失维度清单。

**为什么这条是强制的**：`0` 往往恰好落在风险阈值内。例如 `bal_min=0` 满足
"账户曾透支"（`bal_min<=0`）、`in_out_ratio=0` 满足"资金净流出"（`<0.9`）。
用 0 填充缺失值 → **没有任何数据也能报出一堆高风险**，直接摧毁结果可信度。

反例（真实踩过）：某 skill 在空输入下同时报出「账户曾透支（高）」+「资金净流出（中）」。
根因只是两个指标默认成了 0。

`eval/contract_tests.py::t_degrade` 会拦截这类回归——**退化输入不得产出凭空的风险点**。

---

## IF-2 · skill 元数据契约

每个 `skill.md` 必须以此 frontmatter 开头：

```yaml
---
name: sk_cash
description: 农户贷款银行流水分析——收入稳定性、异常交易、资金用途偏离、还款来源核查。
version: 1.0.0
contract: v1
input_slots: [txn[], period, purpose]
output_keys: [findings@cash[], income_est{}]
rules_required:
  dims: [cash]
  ids: []
refs_index:
  - path: references/rules.closure.md
    when: 需要规则明细或阈值时
max_reads: 2
---
```

| 字段 | 约束 |
|---|---|
| `name` | 与目录名一致，前缀 `sk_` |
| `input_slots` | 从主智能体接收的字段；**未列出的字段一律不传**（上下文裁剪的依据） |
| `output_keys` | 产出键，供主智能体汇总 |
| `rules_required` | `dims` 或 `ids` 至少填一个，供 `distribute_rules.py` 切片 |
| `refs_index` | refs 索引。**`when` 是触发条件，不是描述**——主智能体/子 skill 据此判断是否读取 |
| `max_reads` | 单次调用最多读几个 refs 文件。超出即视为设计缺陷 |

### 2.1 skill.md 正文章节（统一 skeleton）

```
# <skill 名>
## 何时使用          ← 触发条件（主智能体回填路由表时参考）
## 输入              ← 严格对应 input_slots，说明每个槽的形状
## stage_adaptation  ← 贷前/贷中/贷后 各自侧重什么（环节为表）
## 工作流            ← 步骤。有脚本的先跑脚本
## 输出              ← L1/L2 两层格式，附最小示例
## 边界              ← 明确不做什么（防越界到邻居 skill）
## refs              ← refs_index 的复述 + 触发条件
```

`## 边界` 是防 skill 膨胀的关键节，**必填**。

---

## IF-3 · 路由格式 + 组合宏 + 原语词典

### 3.1 能力路由主表

```
C1|资料核查|材料,证件,身份证,营业执照,土地权证,合同,完整性,缺件|sk_doc|doc[]|findings@doc
C2|流水分析|流水,银行流水,交易,进出账,现金流,还款来源|sk_cash|txn[],period|findings@cash
C3|征信负债|征信,负债率,逾期,多头,担保,贷记卡,查询次数|sk_cred|credit{},debt[]|findings@cred
C4|风险规则|风险点,审查,规则,命中,交叉,一致性,定级,矛盾|sk_rules|features{},findings[]|risk{}
C5|贷后预警|贷后,预警,监测,处置,回访,信号,存量|sk_ew|monitor_ts[],signals|alerts[]
```

列序固定：`能力码|名称|触发词|目标skill|输入槽|输出键`。**触发词按区分度降序**（专有词在前）。

### 3.2 环节修饰符（不改路由目标，只改适用性与侧重）

```
S1|贷前|准入,首次,申请,调查前置|适用:C1,C2,C3,C4|侧重:准入判定
S2|贷中|审批,放款,签约,用信|适用:C1,C2,C3,C4|侧重:审批决策
S3|贷后|存量,在用,监测期,到期|适用:C2,C4,C5|侧重:预警处置
```

### 3.3 组合宏

```
M_FULL_PRE|贷前全面体检,首次授信调查 => C1,C2,C3 -> C4
M_BIG     |大额准入,大额授信         => C2,C3 -> C4
M_POST    |贷后复盘,存量风险排查     => C2,C5 -> C4
M_USE     |资金用途核查,挪用排查     => C2 -> C4
M_CREDIT  |纯征信体检                => C3 -> C4
```

**DAG 语法**：`->` 串行依赖，`,` 可并行。平台支持并行则扇出，否则按拓扑序串行——**同一份表两种情况都成立**。

### 3.4 原语词典（消歧用，短码）

```
资料 <- 材料,证件,文档,证明,合同,权证
流水 <- 流水,交易,进出账,走账,明细
征信 <- 征信,信用报告,负债,逾期,贷记卡
风险 <- 风险,隐患,问题,异常,疑点
预警 <- 预警,监测,信号,处置,跟踪
```

---

## IF-4 · 规则表 + 短码字典

### 4.1 规则表列定义

`rules/rules.yaml` 为唯一事实来源（SSOT）。每条规则**一行一记录**：

```
id|dim|cond|level|advice|basis|owner
R017|cash|inc_cv>0.5 && gap_cnt>=3|高|核实收入波动原因|流水分析要点3|sk_cash
R044|cred|multi_lend>=4 && dti>0.55|高|压降额度或追加担保|征信审查要点9|sk_cred
X003|x|declared_inc>income_est*1.5|高|三方对账不一致|交叉验证规则1|sk_rules
```

| 列 | 说明 |
|---|---|
| `id` | 全局唯一。`R` 领域规则 / `X` 跨域规则 / `EW` 贷后规则 |
| `dim` | `doc`/`cash`/`cred`/`x`/`ew` |
| `cond` | **短码表达式**，变量取自 `dict.yaml`。支持 `> >= < <= == != && \|\|` |
| `level` | `高`/`中`/`低`，受 IF-1.1 标尺约束 |
| `advice` | ≤16 字处置建议 |
| `basis` | 依据出处 |
| `owner` | 归属 skill，`distribute_rules.py` 据此切片 |

**为什么表驱动而非自然语言段落**：一条规则从 3–5 行散文（约 60–100 token）压到 1 行（约 20–25 token）。300 条规则从约 12k token 降到约 3k，且可 grep、可 diff、**可被脚本直接求值**。

### 4.2 短码字典

变量与短码的映射在 `rules/dict.yaml`，**所有 `cond` 只能用字典中已定义的短码**。新增短码需走 P1 评审。

---

## IF-5 · 目录结构 + 打包规范

### 5.1 仓库源（SSOT 在此，**不打包**）

```
repo/
├─ contracts/contracts.md      ← 本文件
├─ rules/rules.yaml            ← 规则 SSOT
├─ rules/dict.yaml             ← 短码字典
├─ main/                       ← 主智能体源
├─ skills/sk_*/                ← 各 skill 源（含 _template/）
├─ build/                      ← 构建脚本
├─ eval/                       ← 评测
└─ dist/                       ← 产物
```

### 5.2 产物 zip（**自包含**，每个可独立运行）

```
sk_cash.zip
├─ skill.md                      ← 元数据 + 正文 + top-N 内联规则 + stage_adaptation
├─ references/
│  ├─ rules.closure.md           ← 构建期按 owner/dim 切片，懒加载
│  └─ ruleset.lock               ← 版本 + 包内规则 id 清单（降级判断用）
└─ scripts/
   └─ analyze.py                 ← 数值聚合 + 规则表解析 + 闭包自检
```

### 5.3 自包含硬规则

1. **禁止跨包运行时依赖**。`sk_cash` 不得引用 `sk_cred` 的任何文件或规则。
2. 共享内容（规则、公共背景）在**构建期复制**进各包，**源码期保持单一事实来源**。
3. 每个包必须内含 `ruleset.lock`，记录本包规则 id 清单与版本。

### 5.4 构建脚本接口

| 脚本 | 契约 |
|---|---|
| `build/distribute_rules.py` | 读 `rules/rules.yaml` + 各 skill 的 `rules_required` → 写各包 `references/rules.closure.md` + `ruleset.lock` |
| `build/check_closure.py` | 校验每个 skill 声明引用但未打包的规则 id → **缺失即 exit 1（构建失败）** |
| `build/pack.py` | 组装 zip 到 `dist/`。**先跑 check_closure，不通过则拒绝打包** |

**故障左移原则**：宁可构建失败，不可运行时翻车。缺规则的 `exit 1` 优于运行时静默跳过。

### 5.5 降级协议（工具不可用时的兜底）

子 skill 可用工具尚不确定。因此：

- **脚本是唯一确定项**——所有数值计算、规则表解析、闭包自检都落到 `scripts/`。
- 工具的有无**只影响 references 的读取方式**，不影响任何契约。
- 若 refs 不可读，skill 必须在输出中标注 `coverage: partial` + 缺失维度清单，**绝不静默跳过或崩溃**。

---

## 附：契约自检清单（每次提交前跑）

- [ ] 所有 finding 的 `level` ∈ {高,中,低}
- [ ] 所有 `ev` 符合 `<源>.<定位>=<值>` 指针格式，无自然语言句子
- [ ] 所有 `title` ≤12 字
- [ ] 所有 `cond` 中的短码均已在 `dict.yaml` 定义
- [ ] 每个 `skill.md` frontmatter 含全部 8 个必需字段
- [ ] 每个 `skill.md` 含 `## 边界` 节
- [ ] `check_closure.py` 通过
