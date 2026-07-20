# Task 7 Report: 进程内单刷新协调器恢复审计

## 状态

待独立审查。

## 恢复起点

前序 agent 留下未跟踪的 `agent/src/news/coordinator.py` 与
`agent/tests/news/test_coordinator.py`。审计后保留其单任务锁、106 endpoint
去重、108 assignment 分发、12 赛道编排、空更新早退、LLM 降级、原快照保留、
惰性单例和关闭路径；未回滚其他 agent 的改动。

初始命令：

```bash
pytest agent/tests/news/test_coordinator.py -q
```

结果：`7 passed in 5.52s`。

## RED / GREEN

### RED

新增 `test_endpoint_failure_log_redacts_untrusted_error_code` 后运行：

```bash
pytest agent/tests/news/test_coordinator.py -q
```

结果：`1 failed, 7 passed`。失败符合预期：日志原样包含伪造 transport
错误码中的 `Bearer sk-live-do-not-log`。

### GREEN

协调器仅记录 transport 已定义的固定错误码；其它任意值映射为
`upstream_failed`。验证：

```bash
pytest agent/tests/news/test_coordinator.py -q
ruff check agent/src/news/coordinator.py agent/tests/news/test_coordinator.py
git diff --check
```

结果：`8 passed in 7.05s`；Ruff 和 whitespace 检查通过。

## 提交

`89b65d70caedf6c791f95ec9a747214674dafbb0`
（`feat(news): coordinate single background refresh`，DCO 已签名）。

## 约束核对

- 单进程复用同一运行任务；`start()` 在锁内创建后台任务并立即返回。
- catalog URL 分组后仅抓取 106 个唯一 endpoint，再分发 108 个 assignment；
  pipeline 返回固定 12 个赛道和更新 ID。
- 空更新在 LLM 和快照写入前失败；LLM 全失败仍可写入 feed snapshot。
- endpoint 关联日志的 assignment/track ID 均限制为 5 项，transport 错误码
  采用白名单，避免未信任字符串泄露。
- 存储失败保留旧快照并在 snapshot envelope 中标记 stale；getter 不构造 client
  或 LLM，只有刷新开始时才创建 client。

## 风险与顾虑

- `RefreshStatus` 当前持久模型只接受 `failed`，因此关闭取消使用稳定失败状态；
  若产品需要独立 `cancelled` 状态，需要在 Task 7 允许范围之外扩展模型契约。
- 已经提交到 `asyncio.to_thread` 的同步磁盘写入不能被 asyncio 强制中止。当前
  关闭测试覆盖抓取阶段取消；若关闭语义要求在 committing 阶段绝不完成写入，存储
  接口需要可取消事务或额外的提交协议。
- 所有测试使用本地 fake client、fake store 与 fake LLM；未进行真实联网或真实 LLM
  调用。

## Comet Thorough Review

审查输入：

- brief：`.superpowers/sdd/task-7-brief.md`
- report：本文件
- package：`.superpowers/sdd/review-0f39eb4..89b65d7.diff`
- 范围：`0f39eb4..89b65d7`

全新只读 reviewer 未重复运行已报告的测试，也未修改任何文件。结论：`BLOCKED`，
无 Critical，存在以下 Important：

1. `agent/src/news/coordinator.py:226`：committing 阶段取消 `asyncio.to_thread`
   仅取消 awaiter，已开始的同步 `store.write()` 仍可能在 `close()` 返回后覆盖旧快照；
   任务状态会先标记为失败，违反失败保留旧快照和单任务边界。
2. `agent/src/news/coordinator.py:208`：空更新调用 `_fail(..., "upstream_failed")`，
   而要求为稳定 `no_track_updated`；现有 `test_coordinator.py:263` 固化了错误期望，
   调用方无法区分无更新与上游失败。
3. `agent/src/news/coordinator.py:232`：`CancelledError` 被转换为 `failed`；Task 7
   要求 `cancelled` 终态，但当前 `RefreshStatus` 模型没有该值。需要扩展模型契约并
   添加关闭序列化测试，超出本任务授权修改范围。

Minor：`test_coordinator.py:271` 仅断言 secret 不出现；应补充每 endpoint 仅一条日志、
错误码已规范化、assignment/track ID 都不超过 5 的断言。

## 审查修复

### 根因

- `RefreshStatus` 未声明 `cancelled`，而 `PublicError` 的固定消息表缺少
  `no_track_updated` 与 `cancelled`，导致状态机和公开错误契约不能表达设计要求。
- pipeline 无赛道更新时错误复用了 `upstream_failed`，虽然已在 LLM 和存储前早退，
  但调用方不能区分正常的无更新和上游故障。
- `close()` 无条件取消运行 task。处于 `committing` 的 task 正 await
  `asyncio.to_thread(store.write, snapshot)`；取消只会取消 awaiter，同步原子写仍可在
  `close()` 返回后完成，留下内存终态失败但磁盘已更新的分裂状态。

### RED / GREEN

1. 模型契约：先新增固定错误码和 `RefreshStatus(state="cancelled")` 测试。
   RED：`pytest agent/tests/news/test_models.py::test_public_error_accepts_refresh_stable_codes agent/tests/news/test_models.py::test_refresh_status_accepts_cancelled_terminal_state -q`
   返回 `3 failed`，分别缺少两条公开消息和 `cancelled` 枚举。GREEN：补齐模型消息表和
   枚举后同命令返回 `3 passed`。
2. 无更新早退：将已有本地 fake 协调器测试期望改为 `no_track_updated`。
   RED：`pytest agent/tests/news/test_coordinator.py::test_no_track_updated_fails_before_llm_or_storage -q`
   返回 `1 failed`，实际为 `upstream_failed`。GREEN：仅替换该早退分支的公开码后返回
   `1 passed`，且测试继续断言 LLM/enricher/存储均未调用。
3. 关闭边界：先把抓取期取消的期望改为 `cancelled`，再新增本地 gate 控制的 committing
   写入用例，要求 `close()` 在写门打开前保持 pending，并在写完后以 `succeeded` 返回。
   RED：两个用例返回 `2 failed`，原实现为 `failed` 并取消 committing 的 awaiter。
   GREEN：`close()` 仅在观察到非 `committing` 状态时取消 task；`CancelledError` 由独立
   `cancelled` 终态处理。两个用例返回 `2 passed`。

### 实际语义与验证

- 尚未进入 `committing` 的关闭取消任务，并公开 `state=cancelled` 和 `error.code=cancelled`；
  不会写入部分结果。
- 一旦 coordinator 已发布 `state=committing`，关闭等待该 write 的真实成功或失败结果，
  然后才关闭 client；不会承诺中止已经开始的原子提交。
- 无更新以 `failed/no_track_updated` 结束，并保持在 LLM 与写入之前。
- 验证已通过：`pytest agent/tests/news/test_models.py agent/tests/news/test_coordinator.py -q`
  (`57 passed`)；`pytest agent/tests/news -q` (`125 passed`)；Ruff、`py_compile`、
  `compileall` 与 `git diff --check`。

### 提交

本次修复提交：`fix(news): correct refresh cancellation and error codes`（DCO）。

## 第二轮审查修复：shutdown gate

### 根因

`close()` 过去只在锁内读取当前 task，随后释放锁并等待它完成。若旧 task 在
`committing` 阶段完成，等待期间调用的 `start()` 可以观察到已完成 task 并创建新的
刷新任务；随后 shutdown 继续关闭 client，可能关闭新任务共享的 client。

### RED / GREEN

先新增独立并发用例
`test_start_is_rejected_once_close_has_begun`。旧实现在 `close()` 等待 committing
write 时允许 `start()` 复用 task，因此该用例以“未抛出 RuntimeError”失败。

最小修复为 coordinator 的不可逆 `_closing` 生命周期门禁：`close()` 在同一把锁内先
置位，`start()` 一旦观察到该门禁即稳定抛出
`RuntimeError("news refresh coordinator is closed")`。close 完成后门禁不重置，
coordinator 仍保持 closed，之后的 `start()` 继续被拒绝。该用例 GREEN：`1 passed`。

### 验证与提交

- `pytest agent/tests/news/test_coordinator.py agent/tests/news/test_models.py -q`：`58 passed`
- `pytest agent/tests/news -q`：`126 passed`
- `ruff check agent/src/news/coordinator.py agent/tests/news/test_coordinator.py`、
  `py_compile`、`compileall` 与 `git diff --check`：通过。
- 提交：`fix(news): gate refresh starts during shutdown`（DCO）。
