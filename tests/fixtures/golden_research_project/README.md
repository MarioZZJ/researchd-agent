# Golden Research Project Fixture（SYNTHETIC — 合成数据）

本目录是端到端黄金路径测试（`tests/e2e/test_golden_path.py`）的确定性 fixture 定义。

> ⚠️ **SYNTHETIC 标注（§24 强制）**：此处所有任务、证据、决策与结论均为
> 为测试而构造的合成数据（`doi:10.1000/synthetic-*`、虚构的项目
> `interdisciplinary-citation-pilot` 之外的 `P-GOLD`）。
> **绝不导入真实项目 Evidence**——真实 pilot 数据只存在于部署数据库，
> 由 `docs/pilot.md` 的 bootstrap 流程创建。

## 内容

- `planner_batch.json` — Planner 首批任务（T-001..T-005，对应 §24 的最小覆盖）
- `conflict_candidate.json` — 两个分析的解释冲突候选（触发廉价诊断 → D-002）
- `evidence_a.json` / `evidence_b.json` — 带文献 provenance 的合成证据
- `work_result_template.json` — 合法 work_result 模板（修复循环用）

## 用法

测试通过 `tests/e2e/test_golden_path.py` 中的 `golden` fixture 直接内联这些
payload（FakeExecutor 脚本化），不读取本目录文件——本目录是**人类可读的
规格说明**，保持测试与规格一致。

## 边界（§24）

- 不调用任何真实模型/平台；
- 不写部署数据库（`pytest` 使用独立临时库）；
- 重启恢复（§26 步骤 20–22）在测试内模拟（新 SchedulerLoop + 同库）。
