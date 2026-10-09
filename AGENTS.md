# AGENTS.md · 本项目干活前必读

农户贷款风险防控智能体，农业银行「繁星杯」参赛作品。
**我们写的是给 AI 看的说明书（markdown）+ 算数脚本（Python），不是应用代码。**

---

## 改文件之前

| 要动的东西 | 先看 |
|---|---|
| 任意契约、字段、输出格式 | `contracts/contracts.md`（唯一权威，已冻结） |
| 规则 | `rules/rules.yaml`（唯一事实来源） |
| 分工与交付 | `docs/分工.md`、`docs/交付说明.md` |
| 想照着写一个专家 | `skills/sk_cash/skill.md`（范例）、`skills/_template/skill.md`（模板） |

## 硬规则（违反即返工）

1. **最终等级只能由 `sk_rules` 定。** 其他专家只报领域内原子发现，不做跨维度定级。
2. **规则只写进 `rules/rules.yaml`。** `skills/*/references/rules.closure.md` 是构建期生成物，手改必被覆盖。
3. **算数交给脚本。** 数值聚合、比率、规则匹配、时间序列比对一律脚本化；原始明细不进上下文。
4. **算不出来填 `None`，绝不填 0。** `0` 常落在阈值内，会凭空生成假风险（`eval/contract_tests.py::t_degrade` 拦这个）。
5. **`cond` 里的变量必须先定义在 `rules/dict.yaml`**，否则构建失败。
6. **每个 `skill.md` 必须有 frontmatter 8 字段 + `## 边界` 节。**

## 命令（Mac）

```bash
PYTHONIOENCODING=utf-8 python3 build/pack.py        # 必须出现 [check_closure] 通过
PYTHONIOENCODING=utf-8 python3 eval/contract_tests.py  # 必须 通过：37 项断言
```

**提交前必跑 `pack.py`，不通过不许提交。** 故障左移：宁可构建失败，不可运行时翻车。

## 不要动

- `build/stage/`、`dist/` —— 构建产物，已在 `.gitignore`
- `skills/*/references/rules.closure.md`、`ruleset.lock` —— 自动生成
- `contracts/contracts.md` 的既有字段语义 —— 冻结期只增不改

## 目录约定

空目录靠 `.gitkeep` 占位；`build/pack.py` 打包时会剔除它，不会进交付 zip。
