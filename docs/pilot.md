# Pilot：interdisciplinary-citation-pilot

定义见 IMPLEMENTATION.md §24。本文件是 pilot 的运维与验证记录。

## 定义

```yaml
project_id: interdisciplinary-citation-pilot
question: >
  比较 2017–2019 与 2021–2023 年论文参考文献的学科组成和跨学科引用份额变化，
  描述疫情前后知识来源结构发生了什么变化。
decision:
  id: D-001
  answer: A
  meaning: >
    研究采用描述性定位，不把前后差异直接解释为疫情的因果效应。
  status: APPLIED
```

首批 Task 由 Planner 生成，至少覆盖 T-001..T-005（见
`tests/fixtures/golden_research_project/planner_batch.json`）。

## Bootstrap（部署后执行）

```bash
# 1. 初始化数据库并启动服务
uv run researchd migrate --db "$RESEARCHD_DB"
uv run researchd service &          # 或 systemd user unit（见 docs/deployment）

# 2. 注入 pilot 项目 + D-001（确定性脚本，幂等）
uv run researchd pilot create --project-id interdisciplinary-citation-pilot \
    --question '比较 2017–2019 与 2021–2023 年论文参考文献的学科组成和跨学科引用份额变化，描述疫情前后知识来源结构发生了什么变化。' \
    --import-decision D-001=A

# 3. 观察调度器自动完成：planner → 首批 Task → 执行 → 独立 Auditor 审查 → 门控 → 报告
uv run researchctl project status interdisciplinary-citation-pilot
```

> 真实模型 Pilot（reasonix executor + 付费模型调用）为**外部付费操作**，
> 属 B-01/B-03 授权门：执行上述第 3 步前必须由用户在
> `researchd.toml`/环境变量中显式配置 `RESEARCHD_EXECUTOR=reasonix` 与
> 模型 profile，并确认费用预算。默认 `RESEARCHD_EXECUTOR=fake` 只跑
> 系统行为（黄金路径已覆盖），不产生任何模型调用。

## 完成判据（本次开发任务内）

- [x] 确定性黄金路径 22 步全链通过（`tests/e2e/test_golden_path.py`，含
      独立 Auditor 审查闭环与重启恢复）
- [ ] 真实模型 Pilot 运行（GATED：B-01 凭据 + B-03 模型付费授权）
- [ ] 真实飞书/cc-connect 冒烟（GATED：B-01；patch 已编译验证，安装步骤见
      `integrations/cc-connect/patch/README.md`；`researchctl delivery test` /
      `researchctl document test` 为显式探测命令）

## 精确完成命令（授权解除后）

```bash
export RESEARCHD_EXECUTOR=reasonix
export RESEARCHD_SCHEDULER__EXECUTOR=reasonix
export RESEARCHD_SCHEDULER__DELIVERY=cc_connect
export RESEARCHD_CC_CONNECT__TOKEN=...        # 0600 env 文件；绝不进日志
export RESEARCHD_CC_CONNECT__PROJECT=...
uv run researchd service
# 决策卡/报告经 cc-connect Delivery API 送达飞书（发送+原地更新实测）：
uv run researchctl delivery test [--chat-id oc_xxx]
```
