## ADDED Requirements

### Requirement: 运行级 LLM 用量保持可用并可追溯
系统 SHALL 对每个报告有效 usage 的 AgentLoop 模型调用，累计 Provider、模型、调用次数、输入 Token、输出 Token 和总 Token，并将摘要原子持久化到该运行目录的 `llm_usage.json`。系统 SHALL 经既有会话 SSE `llm_usage` 事件暴露当前调用的增量，并在运行详情响应中返回已持久化摘要，作为全局聚合和单次运行追溯的共同数据源。

#### Scenario: 运行中的模型调用报告用量
- **WHEN** AgentLoop 完成一次携带有效 usage 元数据的模型调用
- **THEN** 系统更新该运行的累计摘要、原子替换 `llm_usage.json`，并通过 `llm_usage` SSE 事件发送对应增量

#### Scenario: 查看已完成运行
- **WHEN** 用户请求一个包含 `llm_usage.json` 的历史运行详情
- **THEN** 响应包含与该文件一致的 LLM 用量摘要，前端展示其累计值

#### Scenario: Provider 未报告 usage
- **WHEN** 模型调用没有有效 usage 元数据
- **THEN** 系统不估算 Token，不创建伪造的用量增量，运行详情明确显示该运行没有 Provider 报告的用量数据

### Requirement: 全部 Agent 会话用量可以按需汇总
系统 SHALL 提供只读的全局 LLM 用量聚合 API，以 Agent 会话为边界关联运行级摘要，并返回筛选时间范围内的总览、每日趋势、Provider/模型分布和分页会话明细。系统 MUST 对相同 run ID 去重，且 MUST NOT 将无法归属到任何会话的孤立运行计入“所有会话”。

#### Scenario: 查看全部历史用量
- **WHEN** 用户未指定起止时间请求全局用量
- **THEN** 系统汇总所有可关联到 Agent 会话且包含有效用量摘要的历史运行，并返回不受明细分页影响的完整总览、趋势和分布

#### Scenario: 按会话关联历史运行
- **WHEN** 一个会话的 attempt 包含 `run_dir`，或历史助手消息包含 `metadata.run_id`
- **THEN** 系统将对应运行归入该会话，优先使用 attempt 关联，并对重复出现的 run ID 只统计一次

#### Scenario: 按日期范围和用户时区筛选
- **WHEN** 用户提供有效的起止时间和 IANA 时区
- **THEN** 系统按 `[start_at, end_at)` 只统计运行时间位于该范围内的关联运行，并按该时区的本地日期生成每日趋势；消息 fallback 使用消息时间，不使用运行 ID 中的无时区时间

#### Scenario: 搜索或分页会话明细
- **WHEN** 用户搜索会话标题或 ID，或切换会话明细页码
- **THEN** 系统只筛选或分页会话明细，总览、趋势和 Provider/模型分布仍覆盖整个时间范围

#### Scenario: 个别运行用量缺失或损坏
- **WHEN** 已关联运行不存在 `llm_usage.json`，或文件无法通过严格校验
- **THEN** 系统跳过该运行的数值并返回缺失或无效计数，其余可用统计仍正常返回

### Requirement: WebUI 提供独立的全局用量中心
系统 SHALL 在 WebUI 左侧导航提供一级“用量”入口和 `/usage` 页面。页面 SHALL 默认展示全部历史，支持近 7 天、近 30 天、本月和自定义时间，展示总览、每日趋势、Provider/模型分布及按会话汇总的可展开运行明细，并只在首次进入或用户手动刷新时请求数据。

#### Scenario: 首次打开用量中心
- **WHEN** 用户从侧栏进入 `/usage`
- **THEN** 页面加载全部历史用量，展示总 Token、输入/输出、调用次数、有用量的会话数、缓存报告状态、趋势、分布和会话明细

#### Scenario: 手动刷新已有快照
- **WHEN** 页面已有成功结果且用户点击刷新
- **THEN** 页面保留当前快照并显示更新状态，成功后替换为新快照并更新刷新时间

#### Scenario: 展开会话查看运行
- **WHEN** 用户展开一个会话明细行
- **THEN** 页面显示该会话内已统计运行的 Provider、模型、Token 和调用次数，并允许跳转到对应运行详情

#### Scenario: 聚合数据为空或请求失败
- **WHEN** 时间范围内没有 Provider 报告的用量，或聚合请求失败
- **THEN** 页面分别显示明确空状态或可重试错误；若已有成功快照，失败时保留该快照并标明刷新失败

### Requirement: Agent 聊天流不展示单次用量
系统 SHALL 从 Agent 聊天流的实时区域和历史消息中移除单次用量卡片，同时 SHALL 保留运行详情中的运行级用量面板。

#### Scenario: Agent 运行产生用量事件
- **WHEN** Agent 页面收到 `llm_usage` SSE 事件或恢复包含用量的历史会话
- **THEN** 聊天流不插入或渲染用量卡片，既有对话、工具状态和运行卡片行为保持不变

#### Scenario: 从全局明细追溯运行
- **WHEN** 用户从用量中心打开一个运行详情
- **THEN** 运行详情继续展示该运行的 Provider 报告用量摘要

### Requirement: 缓存 Token 以供应商报告为准
系统 SHALL 在 Provider usage 元数据包含可识别的缓存读取或缓存写入 Token 时，保留这些值并在运行级摘要和前端展示中标明其可用性。系统 MUST NOT 用零值或启发式算法替代缺失的缓存数据。

#### Scenario: Provider 返回缓存读取 Token
- **WHEN** Provider usage 元数据包含已识别的缓存读取 Token 字段
- **THEN** 该调用增量和运行摘要包含对应缓存读取值，前端将其与输入、输出 Token 一同展示

#### Scenario: Provider 未返回缓存分项
- **WHEN** Provider usage 元数据不包含可识别的缓存读取或缓存写入 Token 字段
- **THEN** 运行摘要不将缺失分项表示为真实零值，前端显示缓存用量未提供

#### Scenario: 读取旧运行产物
- **WHEN** 历史 `llm_usage.json` 仅包含既有输入、输出和总 Token 字段
- **THEN** API 和前端继续读取并展示既有字段，且不因缺少缓存字段失败

### Requirement: 平台计量资格与自带 Key 用量隔离
系统 SHALL 在运行级用量摘要中标明调用是否属于平台托管 VIP 模型的未来可计量范围。仅平台托管 VIP Provider 的调用可以标记为 `metering_eligible`；用户自行配置 API Key 的调用 SHALL 继续展示本地运行用量，但 MUST NOT 被标记为平台额度消耗。

#### Scenario: 使用平台托管 VIP 模型
- **WHEN** 运行使用平台托管的 VIP Provider 执行模型调用
- **THEN** 用量摘要标记该调用为未来可计量，且不暴露登录 token、API Key 或账户凭据

#### Scenario: 使用自带 API Key
- **WHEN** 运行使用用户自行配置的 Provider 或 API Key 执行模型调用
- **THEN** 用量摘要仍可显示 Provider 报告的 Token，但标记为不可计入平台额度

#### Scenario: 尚未提供账户计量服务
- **WHEN** 用户查看当前用量且服务端尚未实现充值、余额或周期结算
- **THEN** 前端仅显示运行统计和计量资格，不展示或推算可扣费余额、每日重置或月度账单

### Requirement: 用量展示不泄露敏感信息
系统 MUST NOT 在 `llm_usage.json`、运行详情用量摘要、SSE 用量事件或全局聚合计数中暴露 API Key、登录 token、消息正文、模型响应或可识别账户凭据。聚合会话明细 MAY 返回既有会话 API 已公开的会话 ID 和用户可见标题，用于搜索与导航，但 MUST NOT 返回会话消息、运行 prompt 或模型响应。

#### Scenario: 序列化用量摘要
- **WHEN** 系统持久化或传输 LLM 用量摘要
- **THEN** 输出仅包含用量计数、Provider/模型标识、调用迭代信息和已定义的计量资格字段，不包含凭据或对话内容

#### Scenario: 返回聚合会话明细
- **WHEN** 系统返回全局用量的会话和运行行
- **THEN** 响应最多包含既有会话 ID、标题、运行 ID、时间和用量字段，不包含消息正文、运行 prompt、模型响应或凭据
