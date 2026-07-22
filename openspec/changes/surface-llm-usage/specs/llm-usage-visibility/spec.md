## ADDED Requirements

### Requirement: 运行级 LLM 用量保持可用并可展示
系统 SHALL 对每个报告有效 usage 的 AgentLoop 模型调用，累计 Provider、模型、调用次数、输入 Token、输出 Token 和总 Token，并将摘要原子持久化到该运行目录的 `llm_usage.json`。系统 SHALL 经既有会话 SSE `llm_usage` 事件暴露当前调用的增量，并在运行详情响应中返回已持久化摘要。

#### Scenario: 运行中的模型调用报告用量
- **WHEN** AgentLoop 完成一次携带有效 usage 元数据的模型调用
- **THEN** 系统更新该运行的累计摘要、原子替换 `llm_usage.json`，并通过 `llm_usage` SSE 事件发送对应增量

#### Scenario: 查看已完成运行
- **WHEN** 用户请求一个包含 `llm_usage.json` 的历史运行详情
- **THEN** 响应包含与该文件一致的 LLM 用量摘要，前端展示其累计值

#### Scenario: Provider 未报告 usage
- **WHEN** 模型调用没有有效 usage 元数据
- **THEN** 系统不估算 Token，不创建伪造的用量增量，前端明确显示该运行没有 Provider 报告的用量数据

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
系统 MUST NOT 在 `llm_usage.json`、运行详情 API、SSE 事件或前端界面中暴露 API Key、登录 token、用户输入、模型响应或可识别账户凭据。

#### Scenario: 序列化用量摘要
- **WHEN** 系统持久化或传输 LLM 用量摘要
- **THEN** 输出仅包含用量计数、Provider/模型标识、调用迭代信息和已定义的计量资格字段，不包含凭据或对话内容
