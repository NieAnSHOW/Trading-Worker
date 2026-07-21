## 1. 上游来源与基础结构

- [x] 1.1 从 `https://github.com/simonlin1212/investment-news` 提交 `d98aa603228f4839fb48859812c63a58ca10cead` 导入 108 个 RSS/Atom 赛道来源映射、106 个唯一 feed 端点、12 个稳定赛道 ID 和过滤规则，并记录规范仓库 URL 与完整提交元数据
- [x] 1.2 生成覆盖 URL、赛道映射和过滤参数的 golden manifest/hash，并添加配置不变量测试，验证 108 个唯一 `(track_id, url)` 映射、106 个唯一网络端点、12 个赛道、映射 ID 唯一性、两个已知跨赛道复用 URL、映射完整性及固定提交标识
- [x] 1.3 将上游 MIT 版权与许可声明放入随资讯模块分发的 notices 路径，验证源码、Python sdist/wheel 与桌面 assemble 资源均包含声明；若采用新的 RSS/Atom 解析依赖，同步项目依赖、桌面 Tier 0 清单和第三方许可材料并运行 Tier 0 smoke
- [x] 1.4 建立资讯领域模块与 Pydantic 模型，定义版本化组合快照、标准化条目、每赛道 `generated_at`/`stale`/可用状态、AI 状态、来源结果和仅驻内存的刷新任务状态

## 2. 快照存储

- [x] 2.1 先编写快照存储测试，覆盖用户数据目录、schema 校验、0600 权限、临时文件清理、原子替换、写入故障和损坏快照读取
- [x] 2.2 实现 `get_data_dir()/news/latest.json` 单快照存储，以同目录临时文件、校验、fsync 和 `os.replace` 原子提交
- [x] 2.3 验证磁盘快照只含允许的 feed 字段、必要元数据和非敏感错误摘要，不保存全文、历史或任何凭据

## 3. Feed 采集与标准化

- [x] 3.1 使用本地 RSS 2.0/Atom fixtures 先编写采集测试，覆盖格式解析、DTD/XXE/实体扩展拒绝、纯文本化、字段/响应上限、标题/URL 必需字段、摘要/时间缺失、恶意链接协议、时间过滤、去重和 12 赛道归类
- [x] 3.2 实现固定来源的安全 XML 解析与有界并发采集，加入超时、响应大小、重定向、协议和非公网目标限制；仅接受 HTTP(S) 条目原文 URL，且不请求条目原文页面
- [x] 3.3 实现端点级异常隔离、向赛道映射分发和非敏感失败统计，测试部分端点失败仍可更新赛道、单赛道无有效条目时 stale/unavailable，以及 12 个赛道均无有效条目时拒绝提交

## 4. LLM 本地化与每赛道要点

- [x] 4.1 先编写 LLM 管线测试，覆盖每次刷新只构建一个模型实例、每个有新数据的赛道一次结构化调用、每赛道最多 16 条候选、总并发上限 3、字符上限、原中文标题不翻译、本地化 ID/非空/长度/中文字符校验、未知 ID、畸形 JSON、超长字段、提示注入文本和调用异常
- [x] 4.2 通过现有 `build_llm()` 实现有界的标题本地化与每赛道 3–5 条中文要点，在同一次赛道结构化调用中返回标题映射和要点，使用 Pydantic 验证输出且不引入 `llm.config.json`
- [x] 4.3 实现标题回退和按赛道 AI 摘要不可用状态，验证单赛道或全部 LLM 调用失败时原始资讯仍可形成有效快照

## 5. 后台刷新与 API

- [x] 5.1 先编写刷新协调器测试，覆盖一次任务刷新全部 12 个赛道、快速启动、阶段进度、单进程单任务、并发请求复用、赛道级 stale 回退、首次 unavailable、12 赛道均无更新时不写盘、取消、重启后 idle 和失败时保留旧快照
- [x] 5.2 实现进程内 `NewsRefreshCoordinator`，以仅驻内存的异步任务编排全部 12 个赛道的采集、标准化、本地化、摘要、赛道级新旧合并和原子提交，并将阻塞工作移出事件循环
- [x] 5.3 先编写 `/news-api/snapshot`、`/news-api/refresh` 和 `/news-api/refresh/status` 的响应、`202`、陈旧回退、空状态及 SPA 路由隔离测试
- [x] 5.4 在现有 FastAPI sidecar 注册 `/news-api/...` 路由，复用 `require_auth` 的本地回环与远程 `API_AUTH_KEY` 策略，不启动第二服务
- [x] 5.5 添加安全测试，确认未授权远程请求被拒绝，且 API 响应、快照、任务状态与日志不包含 LLM/API 凭据或原始 Provider 异常内容

## 6. React 投资资讯页面

- [x] 6.1 扩展前端类型化 API 客户端并为 Vite 开发服务器配置 `/news-api` 代理，支持读取快照、启动/复用刷新和查询任务状态，并为响应解析、代理与错误处理添加测试
- [x] 6.2 先编写页面交互测试，覆盖 `/news` 路由、侧栏入口、12 赛道切换及对应要点、缺失摘要/时间、条目字段、非 HTTP(S) 链接拒绝与安全属性、无快照和按赛道 AI 不可用状态
- [x] 6.3 实现懒加载 `/news` 页面与侧栏“投资资讯”入口，显示当前赛道 3–5 条要点、中文标题优先的资讯列表、来源、时间及 HTTP(S) 原文跳转
- [x] 6.4 实现仅在任务运行期间的状态轮询和进度 UI，覆盖刷新期间保留旧快照、完成后重载、整体失败、陈旧快照和部分来源失败提示
- [x] 6.5 将页面、12 赛道和状态文案接入现有 i18n locale 与回退策略，并验证窄屏/桌面布局无溢出或控件重排

## 7. 集成验证与独立审查

<!-- User decision 2026-07-20: manual-verification pause was superseded later that day. Automated TDD, test execution, and thorough review are resumed. Task 8 continues with the unresolved global unsafe-Origin middleware gap. -->

- [x] 7.1 运行资讯后端目标测试、API 安全与 SPA fallback 测试，确认测试不访问真实网络或真实 LLM
- [x] 7.2 运行前端 Vitest 目标测试和 `npm run build`，修复类型、路由、开发代理、国际化与生产构建问题
- [x] 7.3 使用临时用户数据目录完成端到端降级验证：无快照、部分来源失败、LLM 不可用、刷新失败保留旧快照和进程重启
- [x] 7.4 验证 MIT notice 实际进入 Python sdist/wheel 与桌面 assemble 资源；新增解析依赖时运行 Tier 0 smoke
- [x] 7.5 由独立 reviewer/verifier 对照 spec 检查免费范围、108 映射/106 端点/12 赛道配置与 manifest、单服务、鉴权、XML/链接协议、密钥边界、每赛道要点、无全文/无历史和 MIT notices，并处理所有阻塞发现
