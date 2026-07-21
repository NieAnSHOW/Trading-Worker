---
change: investment-news-hub
reviewed_commit_range: 0110027377a045c029f8d67f37bfc96ae823b18c..900e4e7af35a830f4cfd7b0a047469653445ec59
reviewer_lane: task-13-final-independent-verifier
reviewed_at: 2026-07-21
result: PASS
---

# 投资资讯模块最终独立构建审查

## 结论

本 change 达到 OpenSpec 验收条件。旧审查中的 1 个 P1 和 4 个 P2 均已关闭，本次复审未发现新的 P0、P1 或 P2。OpenSpec 7.5 可以勾选。

本次审查使用当前 HEAD 的本地 fixtures、FakeLLM、拦截式浏览器响应和本地回环 TLS server；未运行真实 feed、真实外网、真实 LLM、live trading、broker-write、支付或钱包流程。`open-code-review` 的外部 LLM 检查因该约束未调用，审查改用本地 Git diff、静态检查和测试证据完成。

## 阻塞发现

### P0

未发现 P0。

### P1

未发现 P1。旧 P1-1 已关闭：

- `PublicFeedClient` 同时设置 `httpx.Limits(max_keepalive_connections=0)` 和 `Connection: close`。
- 本地真实 TLS 回归测试让两个逻辑 hostname 解析到同一公网占位 IP，再将固定后的连接导向同一个回环 TLS server；测试实际观测到 2 个 TCP/TLS 连接、SNI 依次为 `feed.example` 和 `redirect.example`，证明跨 hostname redirect 不复用前一个 TLS 连接。
- 该测试不访问外网，且已随资讯后端目标集 fresh 通过。

### P2

未发现 P2。旧 P2-1 至 P2-4 均已关闭：

| 原发现 | 关闭证据 |
| --- | --- |
| P2-1 中文模型输出未校验中文字符 | `LocalizedTitle.title_zh` 和每条 highlight 均通过 Pydantic pattern 要求至少一个 `U+4E00-U+9FFF` 字符；纯 ASCII 标题或要点测试会降级该赛道 AI，原始资讯保持不变。 |
| P2-2 manifest 只在测试中校验 | `load_catalog()` 运行时校验 manifest bytes、sidecar digest、编译进代码的 golden digest、固定 repository/SHA、schema、12 个固定 track ID、108 mappings、106 unique endpoints、稳定 ID、重复项和 HTTP(S)/public-host URL 不变量；mutation tests 证明不匹配时 fail closed。 |
| P2-3 删除无关规范与审查产物 | 对原清理提交重新枚举得到 41 个删除路径；当前 HEAD 中 41/41 均存在，且逐文件 `git diff --quiet main HEAD -- <path>` 通过，内容与 `main` 完全一致。 |
| P2-4 审查区间有 unsigned commit | 历史已重写；对 `main..900e4e7` 的 64 个提交解析 trailer，并要求 `Signed-off-by` 精确匹配 commit author，结果 64/64 通过，缺失 0、身份不匹配 0。 |

## 非阻塞发现

### P3-1：进入 XML parser 前仍未校验响应 Content-Type

客户端发送 XML `Accept` header，但任意 2xx body 仍会进入安全 parser。2 MiB 限制、DTD/XXE 拒绝和解析失败隔离使其不构成本次 P1/P2；后续可明确允许的 content-type/sniffing policy 并补测试。

### P3-2：viewport evidence 文件未记录原始宽度值

Playwright 会断言 `scrollWidth <= innerWidth` 并记录 PASS，但 evidence markdown 没有持久化 `scrollWidth`、`clientWidth`、`innerWidth` 的原始值。建议后续直接写入这些诊断值。

### P3-3：完整前端 Vitest 基线不是 green

`npx vitest run` fresh 结果为 41 files passed、17 failed；455 tests passed、24 failed、9 skipped。失败包括本分支未修改且仓库中不存在实现文件的旧 auth tests，以及固定英文文案与当前中文 locale 不一致等既有基线问题。资讯目标 67 tests、Settings 相关 17 tests 和生产构建均通过；该残余风险不归类为本 change 的 P1/P2。

## Spec 覆盖

| 审查项 | 结果 | 证据 |
| --- | --- | --- |
| 免费 `/news` 范围，无产品登录/付费门控 | PASS | lazy `/news` 路由与侧栏入口存在；没有资讯专用账户或权益门控 |
| 108 mappings / 106 endpoints / 12 tracks | PASS | 独立 `jq` 查询和 runtime catalog 测试均返回 108 / 106 / 12 |
| 固定上游 SHA 与 golden hash | PASS | repository/SHA 固定；manifest、sidecar、代码 golden digest 均为 `5471c28bd8ad6fe13af7a6d335073fb8dad90ddb316269e092071412c9d8d3f6`，运行时 fail closed |
| 源码/wheel/sdist/desktop 中完整 MIT 条款 | PASS | notice 与 distribution/desktop assemble 目标测试通过 |
| 单 FastAPI sidecar | PASS | 路由注册到现有 `agent/api_server.py`；没有第二个 server/process |
| 现有 `require_auth` 边界 | PASS | 三个端点均通过 `Depends(require_auth)`；remote/loopback 测试通过 |
| SSRF、redirect、DNS/IP pinning 与 TLS hostname 隔离 | PASS | 公网 IP 与逐跳 redirect 校验存在；本地真实 TLS 测试证明不同 hostname/同 IP 建立两个连接并发送两个 SNI |
| DTD/XXE/entity 处理 | PASS | 延迟导入 `defusedxml` 并拒绝声明；fixture 测试通过 |
| 仅允许 HTTP(S) article URL | PASS | backend model/pipeline 与 frontend parser/rendering 均拒绝其他 scheme |
| 凭据与错误脱敏边界 | PASS | 稳定 public error、URL 凭据拒绝、access-log 脱敏，不传播原始 Provider 异常 |
| 赛道级 fresh/stale/unavailable | PASS | pipeline/coordinator 与端到端降级测试覆盖三种状态及 no-update 保留 |
| 每刷新一个 LLM 实例 | PASS | `enrich_tracks()` 只构造一次并共享；FakeLLM 断言一次 factory call |
| 每更新赛道一次调用 | PASS | updated-track indexes 为每个更新赛道创建一次 `enrich_one` |
| 最多 16 候选 / LLM 并发 3 | PASS | 常量与测试固定 16，测试观测 peak concurrency 为 3 |
| 中文标题/3-5 条中文要点 | PASS | Pydantic 中文字符、数量、长度验证；ASCII 拒绝与原文回退测试通过 |
| 无原文全文抓取 / 无历史 | PASS | 仅请求固定 feed endpoint；storage 只有 `news/latest.json`，没有历史 UI/API |
| 两个 frontend viewport | PASS | intercepted Chromium 在 390x844 和 1440x900 通过；fresh 截图人工检查无重叠/溢出 |

## Fresh 验证证据

原 P1/P2 的窄回归：

```text
pytest <TLS real-connection + catalog invariants + Chinese LLM cases> -q
13 passed in 0.24s
```

后端资讯/API 目标测试（排除唯一会 clone 上游 Git 仓库的用例）：

```text
pytest agent/tests/news agent/tests/test_news_routes.py agent/tests/test_security_auth_api.py agent/tests/test_spa_fallback.py -W error::RuntimeWarning -q -k 'not importer_reads_git_objects'
222 passed, 2 skipped, 1 deselected, 21 warnings in 14.97s
```

前端目标测试、Settings 相关测试与生产构建：

```text
npx vitest run <7 news target files>
7 files passed; 67 tests passed

npx vitest run src/pages/__tests__/SettingsChannels.test.tsx src/pages/__tests__/SettingsQVeris.test.tsx
2 files passed; 17 tests passed

npm run build
tsc -b && vite build: exit 0（仅有 chunk-size warning）
```

拦截式 Chromium viewport 验证：

```text
npx playwright test e2e/news-responsive.spec.ts
2 passed (4.5s)
```

静态、OpenSpec、artifact 与 DCO 检查：

```text
sha256(upstream_manifest.json): 5471c28bd8ad6fe13af7a6d335073fb8dad90ddb316269e092071412c9d8d3f6
manifest: 108 mappings, 106 unique endpoints, 12 tracks
openspec validate investment-news-hub --strict: valid
原清理提交的删除路径：41；与 main 完全一致恢复：41；mismatch：0
main..900e4e7 DCO：64 commits；missing：0；author mismatch：0
git diff --check main...900e4e7: exit 0
```

截图：

- `docs/superpowers/reviews/assets/investment-news-mobile.png`（390x844）
- `docs/superpowers/reviews/assets/investment-news-desktop.png`（1440x900）

## 验收门禁

- 阻塞发现：0 个 P0、0 个 P1、0 个 P2。
- 原审查的 1 个 P1 和 4 个 P2 全部关闭。
- `result: PASS`，允许勾选 OpenSpec 7.5 与 Task 13 四个步骤。
