# 数字人审片、返工与发布闭环验收报告

- 日期：2026-09-21
- 范围：数字人精剪候选版本、自动 QA、完整审片、分类返工、发布包绑定
- 关联设计：[`2026-09-21-digital-human-review-publish-closure-design.md`](../superpowers/specs/2026-09-21-digital-human-review-publish-closure-design.md)
- 关联计划：[`2026-09-21-digital-human-review-publish-closure.md`](../superpowers/plans/2026-09-21-digital-human-review-publish-closure.md)

## 结论

自动化闭环已经完成：候选成片按不可覆盖版本保存；自动 QA、完整观看、批准、返工单和发布包都绑定到明确候选；执行返工会使旧发布包过期，下一次渲染会原子认领返工血缘，且新候选不继承旧观看进度或批准记录。

本报告只把自动化测试写为自动化证据。真实数字人成片的人工完整观看仍需由同事操作，未用测试时钟、数据库写入或单元测试冒充人工验收。

## 自动化闭环证据

重点场景 `test_review_rework_publish_closure_keeps_candidate_evidence_isolated` 覆盖：

1. 生成并完整观看 `candidate-v1`，批准后建立绑定 v1 的七文件发布包；
2. 分类驳回 v1，保存并执行局部返工；
3. 生成 `candidate-v2`，核验 `source_candidate_id` 与 `rework_request_id`；
4. 核验 v1 发布包状态为 `expired`，文件仍保留；
5. 核验 v2 没有观看会话，且 v1 的完成会话不能批准 v2；
6. 通过任务详情 API 核验当前候选和返工结果候选一致。

重点回归命令：

```powershell
.venv\Scripts\python -m unittest tests.test_workbench_actions tests.test_workbench_api tests.test_candidate_versions tests.test_rework_requests tests.test_full_review tests.test_publish_package -v
```

结果：`Ran 106 tests in 153.183s`，`OK`。

最终代码评审后的收口回归又覆盖了以下边界：执行返工后旧门禁不会重新放行、QA 阻断候选仍可分类返工、旧候选返工单不可作用于新候选、发布队列精确绑定候选、旧数据库可信候选回填、候选媒体路径边界、异常 QA 数据安全降级，以及首版后的新候选/重试 QA 界面入口。

```powershell
.venv\Scripts\python -m unittest tests.test_rework_requests tests.test_candidate_versions tests.test_database.DatabaseTests.test_v035_upgrade_backfills_one_verified_legacy_candidate_idempotently tests.test_workbench_actions tests.test_workbench_api tests.test_full_review tests.test_publish_package tests.test_workflow -v
```

结果：`Ran 132 tests in 135.900s`，`OK`。

## 全项目自动化验证

| 命令 | 结果 |
| --- | --- |
| `.venv\Scripts\python -m unittest discover -s tests -v` | 862/862 通过，`Ran 862 tests in 1079.278s`，`OK` |
| `npm run test:ts` | 91/91 通过，`fail 0` |
| `npm run test:web` | 76/76 通过，`fail 0` |
| `npx tsc --noEmit` | 退出码 0 |

环境说明：第一次 Python 全套验证使用了过深的临时目录，并发现共享 `.venv` 缺少已声明包的传递依赖。已改回系统临时目录，并按 `requirements.txt` / `requirements-mcp.txt` 恢复环境；`pip check` 返回 `No broken requirements found.`。该轮失败不计作功能通过证据，以上表第二轮全量复跑结果为准。

## 人工验收清单（保持未勾选）

- [ ] 打开真实 `candidate-v1` 并从头到尾完整观看；
- [ ] 选择准确返工分类、问题位置和说明，提交驳回；
- [ ] 检查影响预览和费用提示，确认局部返工；
- [ ] 等待生成真实 `candidate-v2`，确认版本血缘和 v1 历史仍可只读查看；
- [ ] 确认 v2 观看进度为 0，再从头到尾完整观看 v2；
- [ ] 批准 v2，生成发布包并人工核对七个文件都绑定 v2；
- [ ] 核对旧 v1 发布包显示为已过期但历史文件没有被删除。

人工操作路径：

```text
打开 candidate-v1 → 完整观看 → 分类驳回
→ 确认局部返工 → 产生 candidate-v2
→ 确认 v2 观看进度为 0 → 完整观看 v2
→ 批准 → 生成并检查绑定 v2 的发布包
```

## 边界

- 自动化观看使用服务端时间与覆盖率门禁，只证明防跳播和候选隔离逻辑，不代表真人已经看完样片。
- 本次没有触发任何付费模型生成，也没有上传或发布到外部平台。
- 人工清单完成前，只能宣称“软件闭环通过自动化验证”，不能宣称“真实新样片已人工验收”。
