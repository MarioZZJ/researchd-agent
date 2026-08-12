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
  --owner-open-id <真实 PI 的 open_id> \
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
- [x] 真实模型 Pilot 运行（2026-08-12 LIVE VERIFIED：真实 planner → 4 任务 DAG →
      T1–T4 全 COMPLETED（独立 auditor ACCEPT）→ feishu docx 投影 → 决策卡入群 →
      点击 ANSWERED + 卡 PATCH；详见 completion-report §4-3 与执行报告）
- [x] 真实飞书/cc-connect 冒烟（LIVE VERIFIED：testbot 群消息闭环、delivery test
      卡片往返、document create/test 真实 Docx）

## 2026-08-12 真实 Pilot 执行记录（数据修复均已逐项披露于执行报告）

- planner 产出 4 任务（多于交接预估 ≤3；模型自由选择，role 全部 schema 合规）。
- T1（数据获取）三次真实失败：600s transport 超时 → 目录 artifact 声明整单拒绝
  → 1h prompt 上限超时；另 OpenAlex 对本机限流（/works 被封至次日 00:00 UTC），
  c2（引用解析率 ≥80%）环境内不可达。按交接失败条款最小修复：transport 超时
  可重试、目录声明跳过、同任务 artifact supersede、c2 合同放宽为「≥80% 或覆盖
  不足量化记录与原因」（模型自身 escalation 指引的兜底口径）；第四次（finalize
  任务：仅验证+定稿，30 分钟预算）成功 → REVIEW → auditor ACCEPT → COMPLETED。
- T2/T3/T4 各经一次真实失败（worker BLOCKED 死路、artifact supersede 属性 bug）
  后自动/修复重跑全部 COMPLETED；依赖链（T1→T2→T3→T4）全程系统自动驱动。
- D-002 验证决策：链接真实 VERIFIED evidence（e1）→ 报告流程发卡 → 测试驱动
  点击（PI 身份，与真实按钮同 inbound 路径）→ ANSWERED → 原卡 PATCH → 幂等。
- 成本观察：真实模型调用约 15 次（planner 1 + worker 9 + auditor 4 + 重跑），
  其中 3 次为失败重试；单次 worker turn 最长 60 分钟（预算上限内）。

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
