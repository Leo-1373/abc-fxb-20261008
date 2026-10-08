# abc-fxb-20261008 · 农户贷款风险防控智能体

农业银行「繁星杯」智能体竞赛参赛作品。

赛题要求构建集**意图识别 + 风险评估 + 动态预警**于一体、覆盖贷前/贷中/贷后、
能处理**单一或复合**问题的农户贷款风险防控智能体。

## 交付物

6 个自包含 zip：主智能体 + 5 个能力子 skill。

```
main_agent  主智能体：显式路由表 + 组合宏 + 输出 schema（只路由不分析）
sk_doc      资料核查：完整性 / 一致性 / 形式真伪
sk_cash     流水分析：收入稳定性 / 异常交易 / 资金用途偏离
sk_cred     征信负债：负债率 / 逾期画像 / 多头借贷
sk_rules    风险规则引擎：跨维度交叉验证 + 等级合成（定级的唯一发生地）
sk_ew       贷后预警：监测时点序列 / 信号升级解除 / 处置建议
```

## 快速开始

```bash
# 构建全部 zip（含闭包切片 + 完整性校验）
python build/pack.py

# 契约测试（schema / 幂等 / 单调性 / 退化鲁棒）
python eval/contract_tests.py
```

环境：Windows 下**一律用 `python`**（非 `python3`），控制台中文前加 `PYTHONIOENCODING=utf-8`。

## 目录

```
contracts/contracts.md   接口契约 IF-1..5（改接口先改这里）
rules/rules.yaml         规则库单一事实来源（SSOT，不打包）
rules/dict.yaml          短码字典（cond 只能用这里定义的变量）
main/                    主智能体源
skills/sk_*/             5 个能力 skill
skills/_template/        新建 skill 的模板
build/                   构建链路
eval/                    评测
docs/交付说明.md          ← 6 人协作手册，先读这个
```

## 三条铁律

1. **定级只在 `sk_rules` 发生。** 领域 skill 只对领域内原子风险点定级，跨维度结论一律上交。
2. **规则只写进 `rules/rules.yaml`。** 各包 `references/rules.closure.md` 是构建期生成的，手改会被覆盖。
3. **脚本承担确定性计算。** 数字让模型算是"算错 + 慢 + 贵"三重损失；原始流水不进上下文。

详见 [`docs/交付说明.md`](docs/交付说明.md)。
