//! 桌面控制台 IPC —— 环境/服务状态、启停服务、bootstrap 转发、打开 WebUI/日志。
//! 逻辑尽量做成纯函数(可 cargo 测);Tauri command 是薄壳(设计 D3)。

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[cfg(windows)]
use windows_sys::Win32::{
    Foundation::ERROR_MORE_DATA,
    System::Registry::{
        RegCloseKey, RegEnumKeyExW, RegOpenKeyExW, RegQueryValueExW, HKEY, HKEY_CURRENT_USER,
        HKEY_LOCAL_MACHINE, KEY_READ, KEY_WOW64_32KEY, KEY_WOW64_64KEY, REG_EXPAND_SZ,
        REG_SAM_FLAGS, REG_SZ,
    },
};

use tauri::{AppHandle, Emitter, Manager, State};

use crate::auth::{self, AuthError, AuthState, Captcha, LoginRaw, UserInfo};
use crate::runtime_dir::Layout;

pub type SharedChild = Arc<Mutex<Option<Child>>>;
pub type SharedPort = Arc<Mutex<Option<u16>>>;

/// 依赖安装(bootstrap)进行中标志。托盘「退出」据此判断是否需要二次确认。
pub struct InstallingFlag(pub Arc<AtomicBool>);

/// Serializes operations that mutate or execute the embedded runtime.
#[derive(Clone)]
pub struct RuntimeOperationLock(Arc<AtomicBool>);

pub struct RuntimeOperationGuard(Arc<AtomicBool>);

impl RuntimeOperationLock {
    pub fn new() -> Self {
        Self(Arc::new(AtomicBool::new(false)))
    }

    pub fn try_acquire(&self) -> Option<RuntimeOperationGuard> {
        self.0
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .ok()
            .map(|_| RuntimeOperationGuard(self.0.clone()))
    }
}

impl Drop for RuntimeOperationGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

async fn run_blocking<F, R>(operation: F) -> Result<R, String>
where
    F: FnOnce() -> R + Send + 'static,
    R: Send + 'static,
{
    tauri::async_runtime::spawn_blocking(operation)
        .await
        .map_err(|e| format!("blocking task join: {e}"))
}

/// 托盘「退出」在有活跃工作时,发给前端(触发二次确认框)的事件名。
/// 窗口关闭按钮 X 不再触发确认——它一律静默收纳到后台(见 main.rs 的 CloseRequested 处理)。
pub const QUIT_REQUESTED_EVENT: &str = "app://quit-requested";

// ── 纯函数(状态判定、命令构造) ──────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EnvStatus {
    NotInstalled,
    Incomplete,
    Ready,
}

/// bootstrap 进度事件:由 `--sse` 帧解析而来,发给前端驱动进度条 + 日志。
/// `stage` 取值 venv | installing | smoke | done | failed;`message` 是该阶段的
/// 人类可读行(installing 阶段即 pip 的原始 stdout 行)。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ProgressEvent {
    pub stage: String,
    pub message: String,
    pub ok: bool,
}

/// 解析 `--sse` 输出的一个 data 行的 JSON,提取 stage/message/ok。
///
/// bootstrap 的 SSE 帧形如:
/// ```text
/// event: progress
/// data: {"stage": "installing", "message": "Collecting pandas"}
///
/// event: done
/// data: {"ok": true, "message": "environment ready"}
/// ```
/// 我们只关心 `data:` 行的 JSON;`event:` 名与 data 里的 stage 语义重复,
/// 且 done/failed 的 data 不含 stage,故解析时用 `event_name` 兜底 stage。
/// 返回 None 表示该 data 行不是合法 JSON(理论上不会发生,防御性跳过)。
pub fn parse_sse_data(event_name: &str, data_json: &str) -> Option<ProgressEvent> {
    let v: serde_json::Value = serde_json::from_str(data_json).ok()?;
    // progress 帧 data 内带 stage;done/failed 帧不带,退回 event 名。
    let stage = v
        .get("stage")
        .and_then(|s| s.as_str())
        .unwrap_or(event_name)
        .to_string();
    let message = v
        .get("message")
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_string();
    // 缺省 ok=true;仅 failed 帧显式带 ok=false。
    let ok = v.get("ok").and_then(|b| b.as_bool()).unwrap_or(true);
    Some(ProgressEvent { stage, message, ok })
}

/// 依据磁盘上的 venv 解释器与 bootstrap hash marker 判定环境状态。
pub fn compute_env_status(layout: &Layout) -> EnvStatus {
    if !layout.venv_python.exists() {
        return EnvStatus::NotInstalled;
    }
    let marker = layout.venv_dir.join(".requirements_hash");
    if marker.exists() {
        EnvStatus::Ready
    } else {
        EnvStatus::Incomplete
    }
}

/// 环境检查判定（纯函数，供设置页「环境检查」与单元测试）。
/// 依赖完整：venv 已就绪（Ready）。运行时代码最新：installed marker 与
/// bundle 版本一致，且 runtime/agent 目录存在（marker 残留但代码被删时判旧）。
pub fn decide_environment(
    env: EnvStatus,
    runtime_agent_is_dir: bool,
    installed: Option<&str>,
    bundle: &str,
) -> (bool, bool) {
    let deps_ok = env == EnvStatus::Ready;
    let runtime_ok = runtime_agent_is_dir && installed.is_some_and(|v| v.trim() == bundle.trim());
    (deps_ok, runtime_ok)
}

fn remember_until(remember: bool, now: i64) -> Option<i64> {
    remember.then_some(now + auth::REMEMBER_LOGIN_SECS)
}

/// 构造 `vibe-trading bootstrap --sse` 子进程命令。
/// bootstrap 用 bundle 的 Tier 0 python 执行(此时 venv 尚不存在),它内部再建 venv。
pub fn build_bootstrap_cmd(tier0_python: &Path, runtime_agent: &Path) -> std::process::Command {
    let mut cmd = std::process::Command::new(tier0_python);
    cmd.arg("-c")
        .arg("import cli,sys; raise SystemExit(cli.main(sys.argv[1:]))")
        .arg("bootstrap")
        .arg("--sse")
        .current_dir(runtime_agent)
        .env("PYTHONPATH", runtime_agent)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());

    // 从 GUI 应用 spawn 控制台子进程时,Windows 会为其分配新控制台,
    // 表现为弹出空白命令窗口。CREATE_NO_WINDOW 抑制该行为(与 sidecar 一致)。
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    cmd
}

/// 渠道名只允许小写字母/数字/连字符/下划线。channel 来自前端并拼进 pip extras,
/// 是信任边界输入,必须校验以防构造怪异 extras 名(如空串、空格、路径片段)。
pub fn validate_channel(channel: &str) -> Result<(), String> {
    let ok = !channel.is_empty()
        && channel
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_');
    if ok {
        Ok(())
    } else {
        Err(format!("非法渠道名: {channel}"))
    }
}

/// 构造 `pip install 'vibe-trading-ai[<channel>]'` 子进程命令(用 venv 解释器)。
/// --no-input 防止 pip 在非交互环境下因依赖冲突提示而卡死。
pub fn build_channel_dep_cmd(venv_python: &Path, channel: &str) -> std::process::Command {
    let mut cmd = std::process::Command::new(venv_python);
    cmd.arg("-m")
        .arg("pip")
        .arg("install")
        .arg("--no-input")
        .arg(format!("vibe-trading-ai[{channel}]"))
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());

    // 从 GUI 应用 spawn 控制台子进程时,Windows 会为其分配新控制台,
    // 表现为弹出空白命令窗口。CREATE_NO_WINDOW 抑制该行为(与 sidecar 一致)。
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    cmd
}

fn prepare_runtime_from_bundle(app: &AppHandle, layout: &Layout) -> Result<(), String> {
    let res = crate::resources::Resources::resolve(app).map_err(|e| format!("resources: {e}"))?;
    crate::runtime_dir::prepare(
        &res.agent_template,
        &res.env_seed,
        &res.version_file,
        Some(&res.frontend_dist),
        layout,
    )
}

// ── Tauri IPC 命令 ──────────────────────────────────────────────────

#[derive(serde::Serialize)]
pub struct StatusReport {
    pub env: EnvStatus,
    pub service_running: bool,
    pub port: Option<u16>,
}

pub fn build_status_report(
    env: EnvStatus,
    service_running: bool,
    port: Option<u16>,
) -> StatusReport {
    StatusReport {
        env,
        service_running,
        // 仅在 sidecar 仍由本应用持有时返回端口,避免暴露失效地址。
        port: service_running.then_some(port).flatten(),
    }
}

/// 登录命令返回给前端的结构（不含 token）。
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LoginResultView {
    pub user_info: UserInfo,
    pub has_password: bool,
    pub expire_at: i64, // epoch 秒
    pub message: String,
}

/// 无业务 data 的命令返回给前端的用户可见消息。
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CommandMessage {
    pub message: String,
}

/// console_auth_status 返回。
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthStatusView {
    pub authenticated: bool,
    pub user_info: Option<UserInfo>,
    pub expire_at: Option<i64>,
    #[serde(default)]
    pub membership_changed: bool,
}

#[derive(Debug, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
struct PythonCustomReadiness {
    custom_configured: bool,
}

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CustomReadinessView {
    pub custom_configured: bool,
}

fn map_custom_readiness(payload: PythonCustomReadiness) -> CustomReadinessView {
    CustomReadinessView {
        custom_configured: payload.custom_configured,
    }
}

#[derive(Debug, serde::Deserialize)]
struct StagedProvider {
    name: String,
    #[serde(default)]
    api_key_env: Option<String>,
    base_url_env: String,
    api_key_required: bool,
    #[serde(default)]
    auth_type: String,
}

fn configured_secret(value: Option<&str>) -> bool {
    let value = value.unwrap_or_default().trim();
    !value.is_empty()
        && !matches!(
            value.to_ascii_lowercase().as_str(),
            "xxx" | "sk-xxx" | "sk-or-v1-your-key-here" | "gsk_xxx" | "your-api-key"
        )
}

fn stopped_custom_readiness(layout: &Layout) -> CustomReadinessView {
    let result: Result<bool, String> = (|| {
        let values =
            auth::parse_env_to_map(&fs::read_to_string(&layout.user_env).unwrap_or_default());
        let provider_name = values
            .get("LANGCHAIN_PROVIDER")
            .map(|value| value.trim().to_ascii_lowercase())
            .unwrap_or_else(|| "openai".into());
        let config_path = layout
            .runtime_agent
            .join("src/providers/llm_providers.json");
        let config = fs::read_to_string(config_path).map_err(|error| error.to_string())?;
        let providers: Vec<StagedProvider> =
            serde_json::from_str(&config).map_err(|error| error.to_string())?;
        let provider = providers
            .into_iter()
            .find(|provider| provider.name == provider_name)
            .ok_or_else(|| format!("unknown provider {provider_name}"))?;
        if provider.name == "vip_server" || provider.auth_type == "oauth" {
            return Ok(false);
        }
        let model = values
            .get("LANGCHAIN_MODEL_NAME")
            .map(String::as_str)
            .unwrap_or("")
            .trim();
        let base_url = values
            .get(&provider.base_url_env)
            .map(String::as_str)
            .unwrap_or("")
            .trim();
        if model.is_empty() || base_url.is_empty() {
            return Ok(false);
        }
        if provider.api_key_required
            && !configured_secret(
                provider
                    .api_key_env
                    .as_deref()
                    .and_then(|key| values.get(key).map(String::as_str)),
            )
        {
            return Ok(false);
        }
        Ok(true)
    })();
    CustomReadinessView {
        custom_configured: result.unwrap_or(false),
    }
}

async fn custom_readiness_for_port(port: u16) -> Result<CustomReadinessView, String> {
    let body = proxy_settings(
        port,
        reqwest::Method::GET,
        "/settings/llm/custom-readiness",
        None,
    )
    .await?;
    serde_json::from_str::<PythonCustomReadiness>(&body)
        .map(map_custom_readiness)
        .map_err(|error| format!("解析自定义 LLM 状态失败: {error}"))
}

async fn logout_to_custom_inner(
    service_port: &SharedPort,
    auth_state: &AuthState,
    runtime_operation: &RuntimeOperationLock,
    layout: &Layout,
) -> Result<CustomReadinessView, String> {
    let _operation = runtime_operation
        .try_acquire()
        .ok_or_else(|| "运行环境正在维护，请等待当前操作完成".to_string())?;
    let original_env = fs::read_to_string(&layout.user_env).unwrap_or_default();
    auth::persist_custom_mode(layout).map_err(|error| error.to_string())?;
    let current_port = *service_port.lock().unwrap();
    let readiness = if let Some(port) = current_port {
        match proxy_settings(
            port,
            reqwest::Method::POST,
            "/settings/llm/desktop-exit-vip",
            None,
        )
        .await
        {
            Ok(body) => serde_json::from_str::<PythonCustomReadiness>(&body)
                .map(map_custom_readiness)
                .map_err(|error| format!("解析自定义 LLM 状态失败: {error}"))?,
            Err(error) => {
                let _ = auth::write_env_atomic(&layout.user_env, &original_env);
                return Err(error);
            }
        }
    } else {
        stopped_custom_readiness(layout)
    };
    invalidate_authentication(auth_state, || auth::clear_env_token_section(layout))
        .map_err(|error| format!("自定义模型已切换，但清理登录信息失败；请重试退出：{error}"))?;
    Ok(readiness)
}

#[tauri::command]
pub async fn console_custom_llm_readiness(
    service_port: State<'_, SharedPort>,
) -> Result<CustomReadinessView, String> {
    let layout = Layout::from_home()?;
    let current_port = *service_port.lock().unwrap();
    if let Some(port) = current_port {
        Ok(custom_readiness_for_port(port)
            .await
            .unwrap_or(CustomReadinessView {
                custom_configured: false,
            }))
    } else {
        Ok(stopped_custom_readiness(&layout))
    }
}

#[tauri::command]
pub async fn console_logout_to_custom(
    service_port: State<'_, SharedPort>,
    auth_state: State<'_, AuthState>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<CustomReadinessView, String> {
    let layout = Layout::from_home()?;
    logout_to_custom_inner(
        service_port.inner(),
        auth_state.inner(),
        runtime_operation.inner(),
        &layout,
    )
    .await
}

/// Complete a login transition from custom models back to the member runtime.
/// A running custom sidecar must be replaced so its process environment receives
/// the freshly fetched VIP credential; a stopped service only needs the mode
/// persisted and will be started by the onboarding flow.
#[tauri::command]
pub async fn console_login_activate_vip(
    app: AppHandle,
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    auth_state: State<'_, AuthState>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<Option<u16>, String> {
    let layout = Layout::from_home()?;
    let was_custom = auth::read_llm_mode(&layout) == auth::DesktopLlmMode::Custom;
    let was_running = state.lock().unwrap().is_some();
    let operation = if was_custom && was_running {
        Some(
            runtime_operation
                .try_acquire()
                .ok_or_else(|| "运行环境正在维护，请等待当前操作完成".to_string())?,
        )
    } else {
        None
    };
    auth::persist_vip_mode(&layout).map_err(|error| error.to_string())?;
    if operation.is_none() {
        return Ok(None);
    }

    let shared = state.inner().clone();
    let port = service_port.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _operation = operation.expect("running custom login transition holds the lock");
        stop_service_blocking(&shared, &port);
    })
    .await
    .map_err(|error| format!("停止旧服务失败: {error}"))?;

    start_service_inner(
        &app,
        state.inner(),
        service_port.inner(),
        auth_state.inner(),
        runtime_operation.inner(),
    )
    .await
    .map(Some)
    .map_err(|error| error.to_string())
}

fn membership_level_changed(
    previous: Option<&auth::MemberLevel>,
    current: Option<&auth::MemberLevel>,
) -> bool {
    previous.is_some() && previous != current
}

fn unauthenticated_status() -> AuthStatusView {
    AuthStatusView {
        authenticated: false,
        user_info: None,
        expire_at: None,
        membership_changed: false,
    }
}

fn invalidate_authentication<F>(state: &AuthState, clear_persistence: F) -> Result<(), AuthError>
where
    F: FnOnce() -> Result<(), AuthError>,
{
    let mut guard = state.0.lock().unwrap();
    clear_persistence()?;
    *guard = None;
    Ok(())
}

fn clear_invalid_authentication_for_token(
    state: &AuthState,
    layout: &Layout,
    token: &str,
) -> Result<bool, AuthError> {
    let mut guard = state.0.lock().unwrap();
    if !matches!(guard.as_ref(), Some(current) if current.token == token) {
        return Ok(false);
    }
    auth::clear_env_token_section(layout)?;
    *guard = None;
    Ok(true)
}

fn is_authentication_error(error: &AuthError) -> bool {
    match error {
        AuthError::LoginExpired
        | AuthError::NotAuthenticated
        | AuthError::Api { code: 401, .. } => true,
        // UserMiddleware wraps a rejected app token as Cool COMMFAIL (1001) with this
        // message and a 200 response. Keep the message check so unrelated 1001 errors do
        // not discard a usable remembered session.
        AuthError::Api {
            code: 1001,
            message,
        } => message.contains("登录失效"),
        _ => false,
    }
}

fn session_is_still_current(state: &AuthState, session: &auth::UserSession) -> bool {
    matches!(state.0.lock().unwrap().as_ref(), Some(current) if current.token == session.token)
}

/// console_start_service 的错误。
#[derive(Debug, serde::Serialize)]
#[serde(tag = "variant")]
pub enum ServiceStartError {
    EnvNotReady,
    AlreadyRunning,
    SpawnFailed { message: String },
    HealthTimeout,
    ProcessExited { code: Option<i32> },
    Other { message: String },
}

fn can_start_without_vip(error: &AuthError) -> bool {
    matches!(error, AuthError::NotAuthenticated | AuthError::LoginExpired)
}

impl std::fmt::Display for ServiceStartError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EnvNotReady => write!(f, "环境未就绪，请先完成依赖安装"),
            Self::AlreadyRunning => write!(f, "服务已在运行"),
            Self::SpawnFailed { message } => write!(f, "启动失败: {message}"),
            Self::HealthTimeout => write!(f, "后端 120 秒内未就绪"),
            Self::ProcessExited { code } => write!(f, "后端提前退出（退出码 {code:?}）"),
            Self::Other { message } => write!(f, "{message}"),
        }
    }
}

/// 环境 + 服务状态快照,供控制台首屏与轮询。
#[tauri::command]
pub fn console_status(
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
) -> Result<StatusReport, String> {
    let layout = Layout::from_home()?;
    let running = state.lock().unwrap().is_some();
    let port = *service_port.lock().unwrap();
    Ok(build_status_report(
        compute_env_status(&layout),
        running,
        port,
    ))
}

/// 触发依赖 bootstrap:spawn `vibe-trading bootstrap --sse`,把 SSE 帧解析为
/// 结构化进度事件 emit 到 "bootstrap://event"(前端据 stage 驱动进度条 + 日志)。
///
/// bootstrap 的 stdout 是标准 SSE 流(`event:`/`data:` 两行 + 空行分隔一帧)。
/// 逐行累积,遇空行组帧解析:比逐行透传干净(前端拿不到 `event:`/`data:` 噪声),
/// 也让进度条能按 stage 推进。
#[tauri::command]
pub async fn console_bootstrap(
    app: AppHandle,
    installing: State<'_, InstallingFlag>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<(), String> {
    let operation = runtime_operation
        .try_acquire()
        .ok_or("运行环境正在维护，请等待当前操作完成")?;
    bootstrap_inner(&app, &installing, operation).await
}

/// bootstrap 的执行体（console_bootstrap 与 console_repair_environment 共用）。
/// 调用方须已持有 RuntimeOperationLock 并移交；spawn 失败时锁随 guard 释放。
/// 子进程退出后由事件线程置位/复位 InstallingFlag。
async fn bootstrap_inner(
    app: &AppHandle,
    installing: &InstallingFlag,
    operation: RuntimeOperationGuard,
) -> Result<(), String> {
    let layout = Layout::from_home()?;
    prepare_runtime_from_bundle(app, &layout)?;
    let res = crate::resources::Resources::resolve(app).map_err(|e| format!("resources: {e}"))?;
    let mut child = build_bootstrap_cmd(&res.runtime_python, &layout.runtime_agent)
        .spawn()
        .map_err(|e| format!("spawn bootstrap: {e}"))?;
    let stdout = child.stdout.take().ok_or("no bootstrap stdout")?;
    // 安装期间置位:关闭窗口时据此弹「安装未完成」确认。
    let flag = installing.0.clone();
    flag.store(true, Ordering::SeqCst);
    let app2 = app.clone();
    std::thread::spawn(move || {
        let _operation = operation;
        let reader = BufReader::new(stdout);
        // 累积一帧的 event 名与 data JSON,遇空行(帧边界)组装并 emit。
        let mut event_name = String::new();
        let mut data_json = String::new();
        for line in reader.lines().map_while(Result::ok) {
            if let Some(rest) = line.strip_prefix("event:") {
                event_name = rest.trim().to_string();
            } else if let Some(rest) = line.strip_prefix("data:") {
                data_json = rest.trim().to_string();
            } else if line.is_empty() {
                // 帧结束:解析并派发,随后重置累积器。
                if !data_json.is_empty() {
                    if let Some(ev) = parse_sse_data(&event_name, &data_json) {
                        let _ = app2.emit("bootstrap://event", ev);
                    }
                }
                event_name.clear();
                data_json.clear();
            }
        }
        // 末帧无尾随空行时的兜底派发。
        if !data_json.is_empty() {
            if let Some(ev) = parse_sse_data(&event_name, &data_json) {
                let _ = app2.emit("bootstrap://event", ev);
            }
        }
        let code = child.wait().ok().and_then(|s| s.code());
        flag.store(false, Ordering::SeqCst);
        let _ = app2.emit("bootstrap://exit", code);
    });
    Ok(())
}

/// 启动服务：先尝试静默刷新登录态（未登录/过期不阻塞，用户可自行配 .env），
/// 再 spawn serve + 健康门控。
#[tauri::command]
pub async fn console_start_service(
    app: AppHandle,
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    auth_state: State<'_, AuthState>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<u16, ServiceStartError> {
    start_service_inner(&app, &state, &service_port, &auth_state, &runtime_operation).await
}

/// 启动服务的纯逻辑（IPC 与启动自动启动共用）。成功后在 shared 中挂载子进程。
pub async fn start_service_inner(
    app: &AppHandle,
    state: &SharedChild,
    service_port: &SharedPort,
    auth_state: &AuthState,
    runtime_operation: &RuntimeOperationLock,
) -> Result<u16, ServiceStartError> {
    let _operation = runtime_operation
        .try_acquire()
        .ok_or_else(|| ServiceStartError::Other {
            message: "运行环境正在维护，请等待当前操作完成".to_string(),
        })?;
    let layout = Layout::from_home().map_err(|e| ServiceStartError::Other { message: e })?;
    crate::sidecar::log_vip_runtime_event(&layout.logs_dir, "service start requested");
    if compute_env_status(&layout) != EnvStatus::Ready {
        return Err(ServiceStartError::EnvNotReady);
    }
    if state.lock().unwrap().is_some() {
        return Err(ServiceStartError::AlreadyRunning);
    }
    // custom 模式完全不依赖会员登录；VIP 模式的未登录用户同样可按 .env 配置启动。
    // 已登录但无法获取 VIP 凭据时保持失败，避免悄然切换到其他供应商。
    let vip_session = match auth::read_llm_mode(&layout) {
        auth::DesktopLlmMode::Custom => {
            crate::sidecar::log_vip_runtime_event(
                &layout.logs_dir,
                "selected LLM mode=custom; VIP credential flow skipped",
            );
            None
        }
        auth::DesktopLlmMode::Vip => {
            crate::sidecar::log_vip_runtime_event(
                &layout.logs_dir,
                "selected LLM mode=vip; preparing runtime credential",
            );
            match run_blocking({
                let auth_state = auth_state.clone();
                let layout = layout.clone();
                move || auth::ensure_vip_credential(&auth_state, &layout)
            })
            .await
            .map_err(|message| ServiceStartError::Other { message })?
            {
                Ok(session) => {
                    crate::sidecar::log_vip_runtime_event(
                        &layout.logs_dir,
                        "VIP runtime credential is ready for sidecar injection",
                    );
                    Some(session)
                }
                Err(error) if can_start_without_vip(&error) => {
                    let event = match error {
                        AuthError::NotAuthenticated => {
                            "no login found; starting with existing .env model configuration"
                        }
                        AuthError::LoginExpired => {
                            "login expired; starting with existing .env model configuration"
                        }
                        _ => unreachable!("fallback predicate only accepts unauthenticated errors"),
                    };
                    crate::sidecar::log_vip_runtime_event(&layout.logs_dir, event);
                    None
                }
                Err(error) => {
                    crate::sidecar::log_vip_runtime_event(
                        &layout.logs_dir,
                        "VIP runtime credential preparation failed; service not started",
                    );
                    return Err(ServiceStartError::Other {
                        message: error.to_string(),
                    });
                }
            }
        }
    };

    let port =
        crate::port::pick_free_port().map_err(|e| ServiceStartError::Other { message: e })?;
    crate::sidecar::log_vip_runtime_event(
        &layout.logs_dir,
        &format!(
            "starting sidecar (port={port}, vip_runtime_credential={})",
            vip_session.is_some()
        ),
    );
    let mut child = crate::sidecar::spawn(
        &layout.venv_python,
        &layout.runtime_agent,
        port,
        &layout.runtime_libs,
        &layout.sessions_dir,
        &layout.logs_dir,
        vip_session
            .as_ref()
            .and_then(|session| session.vip.as_ref()),
    )
    .map_err(|e| ServiceStartError::SpawnFailed { message: e })?;

    // await_health 是同步阻塞(reqwest::blocking + thread::sleep,最长 120s)。
    // 甩到阻塞线程池执行——否则会卡死 Tauri main thread,整个窗口假死、
    // 前端 spinner 也转不动(对照 console_bootstrap 用 async + 后台线程,从不卡)。
    crate::sidecar::log_vip_runtime_event(
        &layout.logs_dir,
        &format!("waiting for sidecar health check (port={port})"),
    );
    let shared = state.clone();
    let (ready, mut child) = tauri::async_runtime::spawn_blocking(move || {
        let ready = crate::sidecar::await_health(&mut child, port);
        (ready, child)
    })
    .await
    .map_err(|e| ServiceStartError::Other {
        message: e.to_string(),
    })?;
    match ready {
        crate::sidecar::Ready::Ok => {
            crate::sidecar::log_vip_runtime_event(
                &layout.logs_dir,
                &format!("sidecar health check succeeded (port={port})"),
            );
            // 启动后台线程消费 stdout/stderr 管道，防止缓冲区（~64 KB）写满后
            // Python uvicorn write() 阻塞，导致长时间运行假死（根因 A）。
            // 日志按天写入 ~/.vibe-trading/logs/sidecar-YYYY-MM-DD.log，
            // 跨天自动切分并清理保留窗口外的旧文件（见 sidecar::drain_child_pipes）。
            if let Err(e) = std::fs::create_dir_all(&layout.logs_dir) {
                eprintln!("warn: cannot mkdir logs_dir: {e}");
            }
            crate::sidecar::drain_child_pipes(&mut child, &layout.logs_dir);
            *service_port.lock().unwrap() = Some(port);
            shared.lock().unwrap().replace(child);
            // 崩溃守护:主窗口内嵌 WebUI 后控制台页已被替换,前端无法感知
            // sidecar 异常退出。后台轮询子进程,崩溃时回收句柄、广播
            // service://stopped,并把主窗口带回控制台。正常停止
            // (console_stop_service / 退出流程先取走 child)时本线程自退。
            {
                let app = app.clone();
                let shared = shared.clone();
                let service_port = service_port.clone();
                let logs_dir = layout.logs_dir.clone();
                std::thread::spawn(move || loop {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                    let exited = {
                        let mut guard = shared.lock().unwrap();
                        let Some(child) = guard.as_mut() else {
                            // child 已被停止/退出流程取走,不再归本线程监视。
                            return;
                        };
                        match child.try_wait() {
                            Ok(Some(_)) => {
                                guard.take();
                                *service_port.lock().unwrap() = None;
                                true
                            }
                            Ok(None) => false,
                            // try_wait 异常(句柄失效等):放弃监视,避免误报崩溃。
                            Err(_) => return,
                        }
                    };
                    if exited {
                        crate::sidecar::log_vip_runtime_event(
                            &logs_dir,
                            "sidecar exited unexpectedly; broadcasting stop and returning to console",
                        );
                        let _ = app.emit("service://stopped", ());
                        crate::webui_embed::return_to_console(&app);
                        // 崩溃必须让用户看见:内嵌页已死,窗口带回控制台并前置。
                        crate::tray::show_main_window(&app);
                        return;
                    }
                });
            }
            let _ = app.emit("service://started", port);
            Ok(port)
        }
        crate::sidecar::Ready::ProcessExited(c) => {
            crate::sidecar::log_vip_runtime_event(
                &layout.logs_dir,
                &format!("sidecar exited before health check (code={c:?})"),
            );
            Err(ServiceStartError::ProcessExited { code: c })
        }
        crate::sidecar::Ready::Timeout => {
            crate::sidecar::log_vip_runtime_event(
                &layout.logs_dir,
                &format!("sidecar health check timed out (port={port})"),
            );
            Err(ServiceStartError::HealthTimeout)
        }
    }
}

// ── 环境检查与修复（设置页「维护」板块） ──────────────────────────

/// console_check_environment 返回的环境报告（serde 默认字段名，与 StatusReport 一致）。
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EnvironmentReport {
    pub env: EnvStatus,
    pub installed_version: Option<String>,
    pub bundle_version: String,
    pub deps_ok: bool,
    pub runtime_ok: bool,
}

/// 设置页「环境检查」：只读判定依赖是否完整、运行时代码是否最新，不做任何修改。
#[tauri::command]
pub fn console_check_environment(app: AppHandle) -> Result<EnvironmentReport, String> {
    let layout = Layout::from_home()?;
    let resources =
        crate::resources::Resources::resolve(&app).map_err(|e| format!("resources: {e}"))?;
    let bundle = fs::read_to_string(&resources.version_file)
        .map_err(|e| format!("read bundle VERSION: {e}"))?;
    let installed = fs::read_to_string(&layout.marker).ok();
    let env = compute_env_status(&layout);
    let (deps_ok, runtime_ok) = decide_environment(
        env,
        layout.runtime_agent.is_dir(),
        installed.as_deref(),
        bundle.trim(),
    );
    Ok(EnvironmentReport {
        env,
        installed_version: installed.map(|v| v.trim().to_string()),
        bundle_version: bundle.trim().to_string(),
        deps_ok,
        runtime_ok,
    })
}

/// 设置页「环境检查」的针对性修复：停止服务 → 从 bundle 同步运行时代码
/// （venv 失效时强制刷新代码与 .env，并重装依赖；仅代码落后时只同步代码）。
/// 修复是否到位由用户随后再次点击「环境检查」判定。
#[tauri::command]
pub async fn console_repair_environment(
    app: AppHandle,
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    installing: State<'_, InstallingFlag>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<(), String> {
    let operation = runtime_operation
        .try_acquire()
        .ok_or("运行环境正在维护，请等待当前操作完成")?;
    let layout = Layout::from_home()?;
    let resources =
        crate::resources::Resources::resolve(&app).map_err(|e| format!("resources: {e}"))?;
    let shared = state.inner().clone();
    let service_port = service_port.inner().clone();
    // 停服 + 同步代码都在阻塞线程池执行，避免卡 Tauri 主线程。
    let fresh = tauri::async_runtime::spawn_blocking(move || -> Result<bool, String> {
        let _operation = operation;
        stop_service_blocking(&shared, &service_port);
        let env = compute_env_status(&layout);
        match env {
            // venv 失效（NotInstalled/Incomplete）：强制清掉版本 marker，让
            // prepare 视作升级，从 bundle 重同步代码并刷新 .env。
            EnvStatus::NotInstalled | EnvStatus::Incomplete => {
                crate::runtime_dir::refresh_from_bundle(
                    &resources.agent_template,
                    &resources.env_seed,
                    &resources.version_file,
                    Some(&resources.frontend_dist),
                    &layout,
                )?;
                Ok(true)
            }
            // venv 已就绪：只同步运行时代码（版本落后/标记残留时）。
            EnvStatus::Ready => {
                crate::runtime_dir::prepare(
                    &resources.agent_template,
                    &resources.env_seed,
                    &resources.version_file,
                    Some(&resources.frontend_dist),
                    &layout,
                )?;
                Ok(false)
            }
        }
    })
    .await
    .map_err(|e| format!("repair environment task join: {e}"))??;

    crate::webui_embed::return_to_console(&app);

    if fresh {
        // 仅新装/重装时自动重装依赖（走 bootstrap，复用进度事件与 InstallingFlag）；
        // venv 本来就 Ready 时只同步代码，不重复安装。
        // 锁已在 spawn_blocking 内随 guard 释放，这里重新获取。
        let operation = runtime_operation
            .try_acquire()
            .ok_or("运行环境正在维护，请等待当前操作完成")?;
        bootstrap_inner(&app, &installing, operation).await?;
    }
    Ok(())
}

// ── 设置(桌面壳偏好,~/.vibe-trading/settings.json) ──────────────

/// 读取桌面端设置。纯本地文件读取,失败回退默认值。
#[tauri::command]
pub fn console_get_settings() -> Result<crate::settings::Settings, String> {
    let layout = Layout::from_home()?;
    Ok(crate::settings::load(&layout.root))
}

/// 更新桌面端设置。目前只暴露「启动应用时自动启动服务」开关。
#[tauri::command]
pub fn console_set_autostart(
    app: AppHandle,
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    auth_state: State<'_, AuthState>,
    runtime_operation: State<'_, RuntimeOperationLock>,
    enabled: bool,
) -> Result<(), String> {
    let layout = Layout::from_home()?;
    let mut settings = crate::settings::load(&layout.root);
    settings.autostart_service = enabled;
    crate::settings::save(&layout.root, &settings)?;
    // 打开开关时若环境就绪且服务未在运行,立即生效,无需重启应用。
    if enabled && compute_env_status(&layout) == EnvStatus::Ready && state.lock().unwrap().is_none()
    {
        let app = app.clone();
        let auth_state = auth_state.inner().clone();
        let shared = state.inner().clone();
        let service_port = service_port.inner().clone();
        let runtime_operation = runtime_operation.inner().clone();
        tauri::async_runtime::spawn(async move {
            match start_service_inner(
                &app,
                &shared,
                &service_port,
                &auth_state,
                &runtime_operation,
            )
            .await
            {
                Ok(port) => {
                    let _ = app.emit("service://started", port);
                }
                Err(error) => {
                    crate::sidecar::log_vip_runtime_event(
                        &layout.logs_dir,
                        &format!("autostart-on-toggle failed; service not started ({error})"),
                    );
                }
            }
        });
    }
    Ok(())
}

/// 保存桌面端主题模式。主题设置只影响本地桌面壳,不触碰后端运行配置。
/// Windows 下同时把原生窗口(背景 + 标题栏)重着色为新主题,切换即时生效。
#[tauri::command]
pub fn console_set_theme_mode(app: AppHandle, mode: String) -> Result<(), String> {
    if !matches!(mode.as_str(), "system" | "light" | "dark") {
        return Err("主题模式无效".to_string());
    }
    let layout = Layout::from_home()?;
    let mut settings = crate::settings::load(&layout.root);
    settings.theme_mode = mode.clone();
    crate::settings::save(&layout.root, &settings)?;
    #[cfg(target_os = "windows")]
    if let Some(win) = app.get_webview_window("main") {
        let system_dark = matches!(win.theme(), Ok(tauri::Theme::Dark));
        let dark = crate::window_style::effective_dark(&mode, system_dark);
        if let Err(error) = crate::window_style::apply_window_theme(&win, dark) {
            eprintln!("failed to apply Windows window theme: {error}");
        }
    }
    #[cfg(not(target_os = "windows"))]
    let _ = app;
    Ok(())
}

/// 保存桌面端主题色。
#[tauri::command]
pub fn console_set_theme_color(color: String) -> Result<(), String> {
    if !matches!(
        color.as_str(),
        "teal" | "blue" | "purple" | "pink" | "orange" | "green"
    ) {
        return Err("主题色无效".to_string());
    }
    let layout = Layout::from_home()?;
    let mut settings = crate::settings::load(&layout.root);
    settings.theme_color = color;
    crate::settings::save(&layout.root, &settings)
}

/// 保存服务器 API 密钥(API_AUTH_KEY)。打开 WebUI 时随 URL 传入并落入其
/// localStorage;留空即清除。自 WebUI 设置页的「本地 API 访问」迁移而来。
#[tauri::command]
pub fn console_set_api_auth_key(key: String) -> Result<(), String> {
    let layout = Layout::from_home()?;
    let mut settings = crate::settings::load(&layout.root);
    settings.api_auth_key = key.trim().to_string();
    crate::settings::save(&layout.root, &settings)
}

#[tauri::command]
pub async fn console_login_captcha() -> Result<Captcha, AuthError> {
    run_blocking(auth::fetch_captcha)
        .await
        .map_err(|message| AuthError::Network { message })?
}

#[tauri::command]
pub async fn console_login_send_sms(
    phone: String,
    captcha_id: String,
    code: String,
) -> Result<CommandMessage, AuthError> {
    run_blocking(move || auth::send_sms(&phone, &captcha_id, &code))
        .await
        .map_err(|message| AuthError::Network { message })?
        .map(|message| CommandMessage { message })
}

/// 登录通用收尾：仅写 token 段；会员 LLM 凭据只在 VIP 模式启动时获取。
fn finalize_login(
    raw: LoginRaw,
    has_password: bool,
    remember: bool,
    layout: &Layout,
    auth_state: &AuthState,
    message: String,
) -> Result<LoginResultView, AuthError> {
    let info = fetch_user_info_or_default(&raw.token);
    let mut sess = auth::session_from_login(raw, Some(info.clone()));
    sess.remember_until = remember_until(remember, auth::now_secs());
    auth::write_env_token_section(layout, &sess)?;
    sess.user_info = Some(info.clone());
    *auth_state.0.lock().unwrap() = Some(sess.clone());
    crate::sidecar::log_vip_runtime_event(
        &layout.logs_dir,
        "login succeeded; persisted session tokens only and deferred VIP credential retrieval",
    );
    upload_member_public_key_best_effort(&sess.token, layout);
    Ok(LoginResultView {
        user_info: info,
        has_password,
        expire_at: sess.expire_at,
        message,
    })
}

/// 公钥同步属于登录后的附加流程，在后台执行，避免网络超时阻塞登录返回。
/// 日志只记录阶段，不记录密钥或错误详情。
fn upload_member_public_key_best_effort(token: &str, layout: &Layout) {
    crate::sidecar::log_vip_runtime_event(
        &layout.logs_dir,
        "member-public-key-upload stage=scheduled",
    );
    let token = token.to_string();
    let layout = layout.clone();
    let log_layout = layout.clone();
    let upload = std::thread::Builder::new()
        .name("member-public-key-upload".into())
        .spawn(move || {
            crate::sidecar::log_vip_runtime_event(
                &layout.logs_dir,
                "member-public-key-upload stage=started",
            );
            match auth::upload_member_public_key(&token, &layout) {
                Ok(()) => crate::sidecar::log_vip_runtime_event(
                    &layout.logs_dir,
                    "member-public-key-upload stage=succeeded",
                ),
                Err(_) => crate::sidecar::log_vip_runtime_event(
                    &layout.logs_dir,
                    "member-public-key-upload stage=failed login-continues",
                ),
            }
        });
    if upload.is_err() {
        crate::sidecar::log_vip_runtime_event(
            &log_layout.logs_dir,
            "member-public-key-upload stage=failed thread-start login-continues",
        );
    }
}

/// userInfo 拉取失败时用占位（不阻塞登录主流程，与原 Login.tsx 容错一致）。
fn fetch_user_info_or_default(token: &str) -> UserInfo {
    auth::fetch_user_info(token).unwrap_or(UserInfo {
        id: 0,
        unionid: None,
        avatar_url: None,
        nick_name: None,
        phone: None,
        gender: 0,
        status: 1,
        login_type: 2,
        description: None,
        member_level: None,
    })
}

#[tauri::command]
pub async fn console_login_by_phone(
    phone: String,
    sms_code: String,
    remember: bool,
    auth_state: State<'_, AuthState>,
) -> Result<LoginResultView, AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    crate::sidecar::log_vip_runtime_event(&layout.logs_dir, "login requested (method=phone)");
    let auth_state = auth_state.inner().clone();
    run_blocking(move || {
        let response = auth::login_by_phone(&phone, &sms_code)?;
        let has_password = response.data.has_password;
        finalize_login(
            response.data,
            has_password,
            remember,
            &layout,
            &auth_state,
            response.message,
        )
    })
    .await
    .map_err(|message| AuthError::Network { message })?
}

#[tauri::command]
pub async fn console_login_by_password(
    phone: String,
    password: String,
    remember: bool,
    auth_state: State<'_, AuthState>,
) -> Result<LoginResultView, AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    crate::sidecar::log_vip_runtime_event(&layout.logs_dir, "login requested (method=password)");
    let auth_state = auth_state.inner().clone();
    run_blocking(move || {
        let response = auth::login_by_password(&phone, &password)?;
        let has_password = response.data.has_password;
        finalize_login(
            response.data,
            has_password,
            remember,
            &layout,
            &auth_state,
            response.message,
        )
    })
    .await
    .map_err(|message| AuthError::Network { message })?
}

#[tauri::command]
pub async fn console_login_register(
    phone: String,
    sms_code: String,
    password: String,
    auth_state: State<'_, AuthState>,
) -> Result<LoginResultView, AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    crate::sidecar::log_vip_runtime_event(&layout.logs_dir, "login requested (method=register)");
    let auth_state = auth_state.inner().clone();
    run_blocking(move || {
        let response = auth::register(&phone, &sms_code, &password)?;
        let has_password = response.data.has_password;
        finalize_login(
            response.data,
            has_password,
            false,
            &layout,
            &auth_state,
            response.message,
        )
    })
    .await
    .map_err(|message| AuthError::Network { message })?
}

#[tauri::command]
pub async fn console_login_set_password(
    password: String,
    auth_state: State<'_, AuthState>,
) -> Result<(), AuthError> {
    let token = auth_state
        .0
        .lock()
        .unwrap()
        .as_ref()
        .map(|s| s.token.clone())
        .ok_or(AuthError::NotAuthenticated)?;
    run_blocking(move || auth::set_password(&token, &password))
        .await
        .map_err(|message| AuthError::Network { message })?
}

#[tauri::command]
pub fn console_logout(auth_state: State<'_, AuthState>) -> Result<(), AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    invalidate_authentication(auth_state.inner(), || {
        auth::clear_env_token_section(&layout)
    })
}

#[tauri::command]
pub async fn console_auth_status(
    auth_state: State<'_, AuthState>,
) -> Result<AuthStatusView, AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    let auth_state = auth_state.inner().clone();
    run_blocking(move || {
        // Remember which session began the request so a late failure cannot clear a newer login.
        let requested_token = auth_state
            .0
            .lock()
            .unwrap()
            .as_ref()
            .map(|session| session.token.clone())
            .or_else(|| auth::read_env_token_section(&layout).map(|session| session.token));
        let mut session = match auth::ensure_session_valid(&auth_state, &layout) {
            Ok(session) => session,
            Err(error) if is_authentication_error(&error) => {
                if let Some(token) = requested_token.as_deref() {
                    clear_invalid_authentication_for_token(&auth_state, &layout, token)?;
                }
                return Ok(unauthenticated_status());
            }
            // 网络暂时不可用时仍保留有效期限内的本地记住登录。
            Err(AuthError::Network { .. }) => match auth::read_env_token_section(&layout) {
                Some(session) => session,
                None => return Ok(unauthenticated_status()),
            },
            Err(error) => return Err(error),
        };

        if !session_is_still_current(&auth_state, &session) {
            // `ensure_session_valid` installs a disk-restored session itself. A missing state
            // here therefore means logout happened while validation was in flight.
            return Ok(unauthenticated_status());
        }

        let previous_member_level = session
            .user_info
            .as_ref()
            .and_then(|info| info.member_level.clone());
        let mut membership_changed = false;
        match auth::refresh_user_info(&mut session, auth::fetch_user_info) {
            Ok(()) => {
                membership_changed = membership_level_changed(
                    previous_member_level.as_ref(),
                    session
                        .user_info
                        .as_ref()
                        .and_then(|info| info.member_level.as_ref()),
                );
                let mut guard = auth_state.0.lock().unwrap();
                match guard.as_mut() {
                    Some(current) if current.token == session.token => {
                        current.user_info = session.user_info.clone();
                        if membership_changed {
                            current.vip = None;
                        }
                    }
                    _ => {
                        return Ok(unauthenticated_status());
                    }
                }
            }
            // 资料接口临时不可用时，回退到本次会话已有的缓存资料。
            Err(AuthError::Network { .. }) => {
                let guard = auth_state.0.lock().unwrap();
                match guard.as_ref() {
                    Some(current) if current.token == session.token => {
                        session.user_info = current.user_info.clone();
                    }
                    _ => {
                        return Ok(unauthenticated_status());
                    }
                }
            }
            Err(error) if is_authentication_error(&error) => {
                clear_invalid_authentication_for_token(&auth_state, &layout, &session.token)?;
                return Ok(unauthenticated_status());
            }
            // Business and server errors do not prove a valid token has been revoked.
            Err(AuthError::Api { .. }) => {
                let guard = auth_state.0.lock().unwrap();
                match guard.as_ref() {
                    Some(current) if current.token == session.token => {
                        session.user_info = current.user_info.clone();
                    }
                    _ => return Ok(unauthenticated_status()),
                }
            }
            Err(error) => return Err(error),
        }

        Ok(AuthStatusView {
            authenticated: true,
            user_info: session.user_info,
            expire_at: Some(session.expire_at),
            membership_changed,
        })
    })
    .await
    .map_err(|message| AuthError::Network { message })?
}

/// 获取当前会员运行时凭据对应的安全用量计数。
/// provider URL 和 API key 只保留在阻塞线程的进程内存中，绝不透传 IPC。
#[tauri::command]
pub async fn console_member_usage(
    auth_state: State<'_, AuthState>,
) -> Result<auth::MemberUsageView, AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    let auth_state = auth_state.inner().clone();
    run_blocking(move || {
        let session = auth::ensure_vip_credential(&auth_state, &layout)?;
        auth::with_current_vip_credential(&auth_state, &session, auth::fetch_member_usage)
    })
    .await
    .map_err(|message| AuthError::Network { message })?
}

/// 获取当前登录用户的可展示会员权益，不读取或缓存模型供应商凭据。
#[tauri::command]
pub async fn console_member_benefits(
    auth_state: State<'_, AuthState>,
) -> Result<auth::MemberBenefitsView, AuthError> {
    let layout = Layout::from_home().map_err(|e| AuthError::EnvWrite { message: e })?;
    let auth_state = auth_state.inner().clone();
    run_blocking(move || {
        let session = auth::ensure_session_valid(&auth_state, &layout)?;
        auth::fetch_member_benefits(&session.token)
    })
    .await
    .map_err(|message| AuthError::Network { message })?
}

/// 停止服务:干净回收 sidecar 进程组。
fn stop_service_blocking(state: &SharedChild, service_port: &SharedPort) {
    // 取走 child 后再等待回收，避免锁跨越可能阻塞的进程终止操作。
    if let Some(mut child) = state.lock().unwrap().take() {
        crate::sidecar::terminate(&mut child);
    }
    *service_port.lock().unwrap() = None;
}

#[tauri::command]
pub async fn console_stop_service(
    app: AppHandle,
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<(), String> {
    let operation = runtime_operation
        .try_acquire()
        .ok_or("运行环境正在维护，请等待当前操作完成")?;
    let shared = state.inner().clone();
    let service_port = service_port.inner().clone();
    // terminate 内部 child.wait() 同步等子进程退出;甩到阻塞线程池避免卡 main thread。
    tauri::async_runtime::spawn_blocking(move || {
        let _operation = operation;
        stop_service_blocking(&shared, &service_port);
    })
    .await
    .map_err(|e| format!("stop service task join: {e}"))?;
    // 正常路径 stop 由控制台页发起(未嵌入,no-op);竞态下内嵌页还挂着时带回控制台,
    // 避免用户停服后面对一个已死的 WebUI 页面。
    crate::webui_embed::return_to_console(&app);
    Ok(())
}

/// Open WebUI inside the console's retained iframe. The main webview is never
/// navigated, so the shell rail and the console state remain mounted.
#[tauri::command]
pub fn console_open_webui(app: AppHandle, port: u16) -> Result<bool, String> {
    match crate::webui_embed::prepare_frame(&app, port) {
        Ok(url) => {
            app.emit("webui://open", url)
                .map_err(|e| format!("emit webui open: {e}"))?;
            Ok(true)
        }
        Err(e) => {
            eprintln!("warn: prepare embedded webui failed: {e}; fallback to system browser");
            let url = format!("http://127.0.0.1:{port}/");
            crate::validate_external_url(&url)?;
            crate::open_url_with_system(&url)?;
            Ok(false)
        }
    }
}

/// Consume a WebUI request emitted before the Vue shell registered listeners
/// (for example, the ready-environment auto-start path during native boot).
#[tauri::command]
pub fn console_take_pending_webui(app: AppHandle) -> Option<String> {
    app.state::<crate::webui_embed::WebuiEmbedState>()
        .take_frame_url()
}

/// Close the retained research frame when the persistent rail enters a console page.
#[tauri::command]
pub fn console_close_webui(app: AppHandle) {
    crate::webui_embed::return_to_console(&app);
}

/// 在系统默认浏览器打开 WebUI(次要入口;主入口为主窗口内嵌导航)。
#[tauri::command]
pub fn console_open_webui_external(port: u16) -> Result<(), String> {
    // API 密钥始终随 URL 传入(空=清除),WebUI 首载同步进 localStorage。
    let layout = Layout::from_home()?;
    let settings = crate::settings::load(&layout.root);
    let key = settings.api_auth_key.trim();
    let mut url = tauri::Url::parse(&format!("http://127.0.0.1:{port}/"))
        .map_err(|e| format!("invalid webui url: {e}"))?;
    url.query_pairs_mut().append_pair("api_key", key);
    let url = url.to_string();
    crate::validate_external_url(&url)?;
    crate::open_url_with_system(&url)
}

/// 构造本地 backend 的消息渠道启动 URL。
pub fn channels_start_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/channels/start")
}

/// 启动消息渠道:转发 POST /channels/start 到正在运行的 backend。
/// 等价于 `vibe-trading channels start`。backend 对 loopback 免 auth,无需鉴权头。
///
/// 用 async fn + spawn_blocking：reqwest::blocking 若在同步 Tauri command 中调用，
/// 会占住 Tauri 异步运行时线程（最长 30 s），导致 webview 整体假死（根因 B）。
#[tauri::command]
pub async fn console_start_channels(port: u16) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let resp = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|e| format!("构建 HTTP 客户端: {e}"))?
            .post(channels_start_url(port))
            .send()
            .map_err(|e| format!("调用 /channels/start 失败: {e}"))?;
        let status = resp.status();
        let body = resp.text().map_err(|e| format!("读取响应: {e}"))?;
        if !status.is_success() {
            return Err(format!("后端返回 {status}: {body}"));
        }
        Ok(body)
    })
    .await
    .map_err(|e| format!("spawn_blocking join: {e}"))
    .and_then(|r| r)
}

/// 构造本地 backend 的消息渠道状态 URL。
pub fn channels_status_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/channels/status")
}

// ── LLM / 数据源设置代理(自 WebUI 设置页迁移) ─────────────────────
// loopback 免 auth,无需鉴权头;服务未运行时由调用方决定不触发。

/// 转发本地 backend 的设置读写请求,返回原始 JSON 文本。
/// async + spawn_blocking:同 console_start_channels,避免 reqwest::blocking
/// 占住 Tauri 异步运行时线程导致 webview 假死。
async fn proxy_settings(
    port: u16,
    method: reqwest::Method,
    path: &str,
    body: Option<String>,
) -> Result<String, String> {
    let url = format!("http://127.0.0.1:{port}{path}");
    let path = path.to_string();
    tauri::async_runtime::spawn_blocking(move || {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|e| format!("构建 HTTP 客户端: {e}"))?;
        let mut req = client.request(method, &url);
        if let Some(b) = body.as_deref() {
            req = req
                .header("Content-Type", "application/json")
                .body(b.to_string());
        }
        let resp = req.send().map_err(|e| format!("调用 {path} 失败: {e}"))?;
        let status = resp.status();
        let text = resp.text().map_err(|e| format!("读取响应: {e}"))?;
        if !status.is_success() {
            return Err(format!("后端返回 {status}: {text}"));
        }
        Ok(text)
    })
    .await
    .map_err(|e| format!("spawn_blocking join: {e}"))
    .and_then(|r| r)
}

/// 读取 LLM 设置(GET /settings/llm)。
#[tauri::command]
pub async fn console_get_llm_settings(port: u16) -> Result<String, String> {
    proxy_settings(port, reqwest::Method::GET, "/settings/llm", None).await
}

/// 保存 LLM 设置(PUT /settings/llm,body 为前端构造的 JSON,含 vip/custom 模式)。
#[tauri::command]
pub async fn console_set_llm_settings(port: u16, body: String) -> Result<String, String> {
    proxy_settings(port, reqwest::Method::PUT, "/settings/llm", Some(body)).await
}

/// 读取数据源设置(GET /settings/data-sources)。
#[tauri::command]
pub async fn console_get_data_source_settings(port: u16) -> Result<String, String> {
    proxy_settings(port, reqwest::Method::GET, "/settings/data-sources", None).await
}

/// 保存数据源设置(PUT /settings/data-sources)。
#[tauri::command]
pub async fn console_set_data_source_settings(port: u16, body: String) -> Result<String, String> {
    proxy_settings(
        port,
        reqwest::Method::PUT,
        "/settings/data-sources",
        Some(body),
    )
    .await
}

/// 消息渠道状态:转发 GET /channels/status,供控制台展示运行/未登录/失效。
/// backend 对 loopback 免 auth,无需鉴权头。服务未运行时由调用方决定不触发。
///
/// 用 async fn + spawn_blocking：前端通常定期轮询此接口，若在同步 Tauri command
/// 中用 reqwest::blocking，每次轮询都会占住 Tauri 工作线程最长 10 s（根因 B）。
#[tauri::command]
pub async fn console_channels_status(port: u16) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let resp = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| format!("构建 HTTP 客户端: {e}"))?
            .get(channels_status_url(port))
            .send()
            .map_err(|e| format!("调用 /channels/status 失败: {e}"))?;
        let status = resp.status();
        let body = resp.text().map_err(|e| format!("读取响应: {e}"))?;
        if !status.is_success() {
            return Err(format!("后端返回 {status}: {body}"));
        }
        Ok(body)
    })
    .await
    .map_err(|e| format!("spawn_blocking join: {e}"))
    .and_then(|r| r)
}

/// 停止消息渠道:转发 POST /channels/stop(自 WebUI 设置页迁移)。
#[tauri::command]
pub async fn console_stop_channels(port: u16) -> Result<String, String> {
    proxy_settings(port, reqwest::Method::POST, "/channels/stop", None).await
}

/// 运行配对命令(审批等):转发 POST /channels/pairing/command,
/// body 为前端构造的 JSON({channel, command})。
#[tauri::command]
pub async fn console_run_pairing_command(port: u16, body: String) -> Result<String, String> {
    proxy_settings(
        port,
        reqwest::Method::POST,
        "/channels/pairing/command",
        Some(body),
    )
    .await
}

/// 发起微信扫码登录:转发 POST /channels/weixin/login/start。
#[tauri::command]
pub async fn console_weixin_login_start(port: u16) -> Result<String, String> {
    proxy_settings(
        port,
        reqwest::Method::POST,
        "/channels/weixin/login/start",
        None,
    )
    .await
}

/// 查询微信扫码登录状态:转发 GET /channels/weixin/login/status?login_id=…。
/// login_id 仅做最小百分号编码(login_id 由后端生成,含字母数字与连字符)。
#[tauri::command]
pub async fn console_weixin_login_status(port: u16, login_id: String) -> Result<String, String> {
    let encoded: String = login_id
        .bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect();
    let path = format!("/channels/weixin/login/status?login_id={encoded}");
    proxy_settings(port, reqwest::Method::GET, &path, None).await
}

/// 安装单个消息渠道的可选依赖:用 venv 解释器 spawn
/// `pip install --no-input 'vibe-trading-ai[<channel>]'`,逐行 emit "channeldep://progress"。
/// pip 进度几乎全走 stderr,故 stdout/stderr 各开一线程转发,避免日志空白。
#[tauri::command]
pub async fn console_install_channel_dep(
    app: AppHandle,
    channel: String,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<(), String> {
    validate_channel(&channel)?;
    let operation = runtime_operation
        .try_acquire()
        .ok_or("运行环境正在维护，请等待当前操作完成")?;
    let layout = Layout::from_home()?;
    if !layout.venv_python.exists() {
        return Err("环境未就绪,请先完成依赖安装".into());
    }
    let mut child = build_channel_dep_cmd(&layout.venv_python, &channel)
        .spawn()
        .map_err(|e| format!("spawn pip: {e}"))?;
    let stdout = child.stdout.take().ok_or("no pip stdout")?;
    let stderr = child.stderr.take().ok_or("no pip stderr")?;
    let app_out = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let _ = app_out.emit("channeldep://progress", line);
        }
    });
    let app_err = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = app_err.emit("channeldep://progress", line);
        }
    });
    std::thread::spawn(move || {
        let _operation = operation;
        let code = child.wait().ok().and_then(|s| s.code());
        let _ = app.emit("channeldep://exit", code);
    });
    Ok(())
}

/// 退出应用时是否需要二次确认:服务运行中或依赖安装中都需要提醒用户
/// (退出会终止 sidecar / 中断安装)。纯函数,便于单测覆盖「运行中/安装中/空闲」三态。
/// 注意:窗口关闭按钮 X 不经此判断——它一律静默收纳后台,只有托盘「退出」才走这里。
pub fn needs_quit_confirmation(service_running: bool, installing: bool) -> bool {
    service_running || installing
}

/// 安装器启动前的门禁:bootstrap 子进程结束并释放资源后才能开始更新。
pub fn can_install_update(installing: bool) -> bool {
    !installing
}

/// 用户在退出二次确认框点「确认退出」后调用:真正退出应用。
/// `app.exit(0)` 会触发 RunEvent::ExitRequested,由 main.rs 在那里回收 sidecar 进程组。
#[tauri::command]
pub fn console_quit(app: AppHandle) {
    app.exit(0);
}

/// 强制清理虚拟环境:删除 ~/.vibe-trading/venv,便于用户从零重新安装依赖。
///
/// 仅删除 venv 目录本身(包含解释器与已装依赖);不动 runtime/、.env、
/// sessions/ 等用户数据。删除前应停止服务,否则正在运行的 sidecar 进程
/// 会持有该目录(Windows 下会删除失败)。纯函数便于单测覆盖「存在/不存在」。
pub fn clear_venv_dir(layout: &Layout) -> Result<(), String> {
    if !layout.venv_dir.exists() {
        return Ok(()); // 幂等:目录本就不存在,视为已清理。
    }
    fs::remove_dir_all(&layout.venv_dir)
        .map_err(|e| format!("清理 venv 失败 {}: {e}", layout.venv_dir.display()))
}

/// 强制清理虚拟环境：先停止服务并同步 bundle 代码，再删除
/// ~/.vibe-trading/venv，便于用户从零重新安装依赖。
#[tauri::command]
pub async fn console_clear_venv(
    app: AppHandle,
    state: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    runtime_operation: State<'_, RuntimeOperationLock>,
) -> Result<(), String> {
    let operation = runtime_operation
        .try_acquire()
        .ok_or("运行环境正在维护，请等待当前操作完成")?;
    let layout = Layout::from_home()?;
    let resources =
        crate::resources::Resources::resolve(&app).map_err(|e| format!("resources: {e}"))?;
    let shared = state.inner().clone();
    let service_port = service_port.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _operation = operation;
        stop_service_blocking(&shared, &service_port);
        crate::runtime_dir::refresh_from_bundle(
            &resources.agent_template,
            &resources.env_seed,
            &resources.version_file,
            Some(&resources.frontend_dist),
            &layout,
        )?;
        clear_venv_dir(&layout)
    })
    .await
    .map_err(|e| format!("clear environment task join: {e}"))??;
    crate::webui_embed::return_to_console(&app);
    Ok(())
}

#[allow(dead_code)]
fn legacy_windows_uninstaller_path(local_app_data: &Path) -> PathBuf {
    local_app_data.join("Vibe Trading").join("uninstall.exe")
}

#[allow(dead_code)]
fn legacy_macos_app_paths(home: &Path) -> [PathBuf; 2] {
    [
        home.join("Applications").join("Vibe Trading.app"),
        PathBuf::from("/Applications/Vibe Trading.app"),
    ]
}

#[allow(dead_code)]
fn find_existing_legacy_path<I>(paths: I) -> Option<PathBuf>
where
    I: IntoIterator<Item = PathBuf>,
{
    paths.into_iter().find(|path| path.exists())
}

#[cfg(windows)]
/// 注册表中"添加或删除程序"记录的旧版安装信息。
/// NSIS 安装器(含 Tauri 默认)无论装到哪个目录，都会写入 Uninstall 键。
#[derive(Clone, Debug, PartialEq)]
struct RegistryUninstallInfo {
    /// 卸载程序可执行文件(UninstallString 解析出的首个程序)。
    executable: PathBuf,
    /// UninstallString 中除程序外的参数，随程序一起传给卸载器。
    args: Vec<String>,
    /// 注册表子键名(DisplayName 所在子键)，仅用于错误信息。
    subkey: String,
}

#[cfg(windows)]
fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

#[cfg(windows)]
/// Windows 卸载字符串解析: `"C:\path\uninstall.exe" /S` → (exe, ["/S"])。
/// 遵循 CommandLineToArgvW 规则(双引号可含空格，内部 `""` 转义为字面引号)。
fn parse_uninstall_string(s: &str) -> Option<(PathBuf, Vec<String>)> {
    let bytes = s.as_bytes();
    let mut args: Vec<String> = Vec::new();
    let mut i = 0;
    let n = bytes.len();
    while i < n {
        while i < n && bytes[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= n {
            break;
        }
        let mut cur = String::new();
        let mut in_quotes = false;
        while i < n {
            let c = bytes[i];
            if c == b'"' {
                if in_quotes && i + 1 < n && bytes[i + 1] == b'"' {
                    cur.push('"');
                    i += 2;
                    continue;
                }
                in_quotes = !in_quotes;
                i += 1;
            } else if c.is_ascii_whitespace() && !in_quotes {
                i += 1;
                break;
            } else {
                cur.push(c as char);
                i += 1;
            }
        }
        if !cur.is_empty() {
            args.push(cur);
        }
    }
    let mut it = args.into_iter();
    let exe = it.next()?;
    Some((PathBuf::from(exe), it.collect()))
}

#[cfg(windows)]
/// 读取注册表字符串值。仅处理 REG_SZ/REG_EXPAND_SZ(按字符串读，不做环境变量展开)。
fn read_registry_string(hkey: HKEY, value_name: &str) -> Option<String> {
    let name = to_wide(value_name);
    let mut size: u32 = 0;
    let mut kind: u32 = 0;
    let status = unsafe {
        RegQueryValueExW(
            hkey,
            name.as_ptr(),
            std::ptr::null(),
            &mut kind,
            std::ptr::null_mut(),
            &mut size,
        )
    };
    if status != 0 && status != ERROR_MORE_DATA {
        return None;
    }
    let mut buf = vec![0u8; size as usize];
    let status = unsafe {
        RegQueryValueExW(
            hkey,
            name.as_ptr(),
            std::ptr::null(),
            &mut kind,
            buf.as_mut_ptr(),
            &mut size,
        )
    };
    if status != 0 || (kind != REG_SZ && kind != REG_EXPAND_SZ) {
        return None;
    }
    let nul = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    let bytes = &buf[..nul];
    if !bytes.len().is_multiple_of(2) {
        return None;
    }
    let wide: Vec<u16> = bytes
        .chunks_exact(2)
        .map(|c| u16::from_le_bytes([c[0], c[1]]))
        .collect();
    String::from_utf16(&wide).ok()
}

#[cfg(windows)]
/// 在指定根键(含视图位)下枚举 Uninstall 子键，按 DisplayName 精确匹配旧版应用名。
fn find_registry_legacy_entry(
    root: HKEY,
    view: REG_SAM_FLAGS,
    display_name: &str,
) -> Option<RegistryUninstallInfo> {
    const UNINSTALL_KEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall";
    let subkey = to_wide(UNINSTALL_KEY);
    let mut handle: HKEY = std::ptr::null_mut();
    let result = unsafe { RegOpenKeyExW(root, subkey.as_ptr(), 0, KEY_READ | view, &mut handle) };
    if result != 0 || handle.is_null() {
        return None;
    }
    let mut entry: Option<RegistryUninstallInfo> = None;
    let mut index: u32 = 0;
    loop {
        let mut name_buf = [0u16; 512];
        let mut name_len = name_buf.len() as u32;
        let status = unsafe {
            RegEnumKeyExW(
                handle,
                index,
                name_buf.as_mut_ptr(),
                &mut name_len,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        if status != 0 {
            break;
        }
        let key_name = String::from_utf16_lossy(&name_buf[..name_len as usize]);
        let child_path = to_wide(&format!("{}\\{}", UNINSTALL_KEY, key_name));
        let mut child: HKEY = std::ptr::null_mut();
        if unsafe { RegOpenKeyExW(root, child_path.as_ptr(), 0, KEY_READ | view, &mut child) } == 0
            && !child.is_null()
        {
            let display = read_registry_string(child, "DisplayName");
            if display.as_deref() == Some(display_name) {
                let uninstall_string = read_registry_string(child, "UninstallString");
                if let Some((executable, args)) =
                    uninstall_string.as_deref().and_then(parse_uninstall_string)
                {
                    entry = Some(RegistryUninstallInfo {
                        executable,
                        args,
                        subkey: key_name.clone(),
                    });
                }
            }
            unsafe { RegCloseKey(child) };
        }
        if entry.is_some() {
            break;
        }
        index += 1;
    }
    unsafe { RegCloseKey(handle) };
    entry
}

#[cfg(windows)]
/// 遍历 HKLM/HKCU × 32/64 视图，在注册表中查找旧版应用的卸载信息。
fn find_registry_legacy_uninstaller(display_name: &str) -> Option<RegistryUninstallInfo> {
    let views = [KEY_WOW64_64KEY, KEY_WOW64_32KEY];
    views.iter().find_map(|&view| {
        find_registry_legacy_entry(HKEY_LOCAL_MACHINE, view, display_name)
            .or_else(|| find_registry_legacy_entry(HKEY_CURRENT_USER, view, display_name))
    })
}

#[cfg(windows)]
fn uninstall_legacy_windows() -> Result<(), String> {
    let registry = find_registry_legacy_uninstaller("Vibe Trading");
    uninstall_legacy_windows_inner(registry, spawn_uninstaller)
}

#[cfg(windows)]
/// 启动一个卸载程序(以所在目录为工作目录)。
fn spawn_uninstaller(executable: &Path, args: &[String]) -> Result<(), String> {
    let install_dir = executable
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    std::process::Command::new(executable)
        .args(args)
        .current_dir(install_dir)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("启动旧版 Vibe Trading 卸载程序失败: {e}"))
}

#[cfg(windows)]
/// 按注册表优先、固定路径回退的顺序发现并启动旧版卸载程序。
/// `registry_entry` 为注册表查询结果、`launcher` 为启动函数，测试时可注入。
fn uninstall_legacy_windows_inner(
    registry_entry: Option<RegistryUninstallInfo>,
    launcher: impl Fn(&Path, &[String]) -> Result<(), String>,
) -> Result<(), String> {
    if let Some(entry) = registry_entry {
        if entry.executable.is_file() {
            return launcher(&entry.executable, &entry.args);
        }
    }

    // 回退:老安装器在固定位置留下的 uninstall.exe
    let local_app_data = dirs::data_local_dir().ok_or("local app data directory unavailable")?;
    let uninstaller = legacy_windows_uninstaller_path(&local_app_data);
    if !uninstaller.is_file() {
        return Err(format!(
            "未找到旧版 Vibe Trading: {}",
            uninstaller.display()
        ));
    }
    launcher(&uninstaller, &[])
}

#[cfg(target_os = "macos")]
fn uninstall_legacy_macos() -> Result<(), String> {
    let home = dirs::home_dir().ok_or("home directory unavailable")?;
    let app = find_existing_legacy_path(legacy_macos_app_paths(&home))
        .filter(|path| path.is_dir())
        .ok_or("未找到旧版 Vibe Trading 应用")?;

    if app == Path::new("/Applications/Vibe Trading.app") {
        let app_path = app.to_str().ok_or("legacy app path is not valid UTF-8")?;
        let script = r#"on run argv
  set targetPath to quoted form of (item 1 of argv)
  do shell script "/bin/rm -rf -- " & targetPath with administrator privileges
end run"#;
        return std::process::Command::new("/usr/bin/osascript")
            .args(["-e", script, app_path])
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("启动旧版 Vibe Trading 管理员卸载失败: {e}"));
    }

    fs::remove_dir_all(&app).map_err(|e| format!("删除旧版 Vibe Trading 失败: {e}"))
}

/// 启动或移除旧版 Vibe Trading 应用本体，保留 ~/.vibe-trading 用户数据。
#[tauri::command]
pub fn console_uninstall_legacy_app() -> Result<(), String> {
    #[cfg(windows)]
    {
        uninstall_legacy_windows()
    }
    #[cfg(target_os = "macos")]
    {
        uninstall_legacy_macos()
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        Err("旧版 Vibe Trading 卸载仅支持 Windows 和 macOS".into())
    }
}

/// 在文件管理器打开 ~/.vibe-trading/logs/。
#[tauri::command]
pub fn console_open_logs() -> Result<(), String> {
    let layout = Layout::from_home()?;
    std::fs::create_dir_all(&layout.logs_dir).map_err(|e| format!("mkdir logs: {e}"))?;
    open_path_in_file_manager(&layout.logs_dir)
}

/// 清理 ~/.vibe-trading/logs/ 下所有日志文件（sidecar-YYYY-MM-DD.log 及遗留 sidecar.log）。
/// 保留目录本身;服务运行中当天文件被进程占用时(macOS/Linux 仍可 unlink,Windows 可能
/// 拒绝)删除会失败,自动跳过。返回实际删除的文件数。
#[tauri::command]
pub fn console_clear_logs() -> Result<usize, String> {
    let layout = Layout::from_home()?;
    std::fs::create_dir_all(&layout.logs_dir).map_err(|e| format!("mkdir logs: {e}"))?;
    clear_logs_in(&layout.logs_dir)
}

/// pip 镜像配置文件路径 —— 与 Python 端 `src/optional_deps/mirror.py` 的
/// `default_config_path()` 读写同一路径,bootstrap 启动时读取。
fn mirror_config_path(layout: &Layout) -> std::path::PathBuf {
    layout.root.join("runtime").join("optional_deps_mirror.json")
}

/// 读取镜像名。文件缺失/损坏/非国内镜像名(official|custom|off)时返回默认
/// "tsinghua",与 Python 端 load_mirror_config 的缺省语义一致。
fn read_mirror_name(path: &Path) -> String {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(&raw).ok())
        .and_then(|v| {
            v.get("name")
                .and_then(|n| n.as_str())
                .map(str::to_owned)
        })
        .filter(|n| n == "tsinghua" || n == "aliyun")
        .unwrap_or_else(|| "tsinghua".to_owned())
}

fn write_mirror_name(path: &Path, name: &str) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("mkdir runtime: {e}"))?;
    }
    let body = serde_json::json!({"name": name, "custom_index_url": ""});
    std::fs::write(
        path,
        serde_json::to_string(&body).map_err(|e| format!("serialize mirror config: {e}"))?,
    )
    .map_err(|e| format!("write mirror config: {e}"))
}

/// 在清华/阿里云之间切换并持久化,返回切换后的镜像名。
fn toggle_mirror_in(path: &Path) -> Result<String, String> {
    let next = if read_mirror_name(path) == "aliyun" {
        "tsinghua"
    } else {
        "aliyun"
    };
    write_mirror_name(path, next)?;
    Ok(next.to_owned())
}

/// 读取当前 pip 镜像名(onboarding 页展示用)。
#[tauri::command]
pub fn console_get_mirror() -> Result<String, String> {
    let layout = Layout::from_home()?;
    Ok(read_mirror_name(&mirror_config_path(&layout)))
}

/// 切换 pip 镜像(清华 ↔ 阿里云)并持久化,返回切换后的镜像名。
#[tauri::command]
pub fn console_toggle_mirror() -> Result<String, String> {
    let layout = Layout::from_home()?;
    toggle_mirror_in(&mirror_config_path(&layout))
}

/// 删除目录下所有 `.log` 文件,返回删除数。非 `.log` 文件(会话、配置等)不动。
fn clear_logs_in(dir: &Path) -> Result<usize, String> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Ok(0);
    };
    let mut removed = 0usize;
    for ent in entries.flatten() {
        let path = ent.path();
        if path.extension().and_then(|e| e.to_str()) == Some("log")
            && std::fs::remove_file(&path).is_ok()
        {
            removed += 1;
        }
    }
    Ok(removed)
}

#[cfg(test)]
mod mirror_tests {
    use super::*;

    #[test]
    fn mirror_toggle_roundtrips_and_persists() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("runtime").join("optional_deps_mirror.json");
        // 无配置文件 → 默认 tsinghua → 切到 aliyun
        assert_eq!(read_mirror_name(&path), "tsinghua");
        assert_eq!(toggle_mirror_in(&path).unwrap(), "aliyun");
        assert_eq!(read_mirror_name(&path), "aliyun");
        // 再切回 tsinghua
        assert_eq!(toggle_mirror_in(&path).unwrap(), "tsinghua");
        // 文件内容与 Python 端格式一致
        let raw: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(raw["name"], "tsinghua");
        assert_eq!(raw["custom_index_url"], "");
    }

    #[test]
    fn mirror_read_tolerates_corrupt_or_foreign_entries() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("mirror.json");
        std::fs::write(&path, "not json").unwrap();
        assert_eq!(read_mirror_name(&path), "tsinghua");
        std::fs::write(&path, r#"{"name":"official","custom_index_url":""}"#).unwrap();
        // official/custom/off 不在切换范围内,读取按默认处理
        assert_eq!(read_mirror_name(&path), "tsinghua");
        // 切换把 foreign 名重置回国内镜像
        assert_eq!(toggle_mirror_in(&path).unwrap(), "aliyun");
    }
}

#[cfg(target_os = "macos")]
fn open_path_in_file_manager(p: &Path) -> Result<(), String> {
    std::process::Command::new("open")
        .arg(p)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("open logs: {e}"))
}

#[cfg(target_os = "windows")]
fn open_path_in_file_manager(p: &Path) -> Result<(), String> {
    std::process::Command::new("explorer")
        .arg(p)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("open logs: {e}"))
}

#[cfg(target_os = "linux")]
fn open_path_in_file_manager(p: &Path) -> Result<(), String> {
    std::process::Command::new("xdg-open")
        .arg(p)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("open logs: {e}"))
}

// ── 公共配置 ──

/// 服务端公共配置（镜像 cool-admin /app/base/comm/publicConfig）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PublicConfig {
    #[serde(default)]
    pub official_url: String,
    #[serde(default = "default_true")]
    pub enable_login: bool,
    /// 是否启用广告（服务端 comm 统一管理；服务端缺字段时默认开启）
    #[serde(default = "default_true")]
    pub enable_ad: bool,
    #[serde(default)]
    pub check_update: bool,
    #[serde(default)]
    pub enable_service: bool,
    #[serde(default)]
    pub service_qr_code: String,
    /// 客服微信二维码（登录用户「联系客服」弹窗展示）
    #[serde(default)]
    pub kefu_qr_code: String,
    /// 支持作者二维码（登录用户「支持作者领中级会员」弹窗展示）
    #[serde(default)]
    pub reward_qr_code: String,
    /// 静态资源 baseURL（imgBase）：取自 auth::user_api_url() 当前生效地址；
    /// 本地开发默认 127.0.0.1:8001，生产构建由编译期 VIBE_USER_API_URL 注入。
    #[serde(default)]
    pub img_base: String,
}

fn default_true() -> bool {
    true
}

/// 拉取服务端公共配置，替代前端硬编码的 prod.ts。
/// GET /app/base/comm/publicConfig（IGNORE_TOKEN）。
/// 失败时静默降级为默认值，不阻塞启动流程。
#[tauri::command]
pub async fn console_get_public_config() -> Result<PublicConfig, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let url = format!("{}/app/base/comm/publicConfig", auth::user_api_url());
        let text = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .map_err(|e| format!("build client: {e}"))?
            .get(&url)
            .send()
            .map_err(|e| format!("public config request: {e}"))?
            .text()
            .map_err(|e| format!("public config body: {e}"))?;
        let mut cfg: PublicConfig = auth::parse_cool_response(&text).map_err(|e| e.to_string())?;
        cfg.img_base = auth::user_api_url();
        Ok(cfg)
    })
    .await
    .map_err(|e| format!("spawn_blocking join: {e}"))?
}

// ── 广告 ──

/// 广告项（镜像 cool-admin MarketingAdEntity select 字段）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdItem {
    pub id: i64,
    pub title: String,
    /// 1=纯图片 2=纯文本
    #[serde(rename = "type")]
    pub ad_type: i64,
    pub position: String,
    #[serde(default)]
    pub images: Option<Vec<AdImage>>,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub link: Option<String>,
    pub sort: i64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AdImage {
    pub url: String,
    #[serde(default)]
    pub link: Option<String>,
}

/// 调用 cool-admin 公开接口拉取广告列表。
/// POST /app/marketing/ad/list → { type?, position? } → { code, data: AdItem[], message }
/// 无鉴权（IGNORE_TOKEN），静默失败：接口挂了或没广告时返回空数组。
///
/// 注意：reqwest::blocking 会阻塞调用线程。若在 #[tauri::command] 同步函数中调用，
/// 会占住 Tauri 异步运行时线程，导致 webview 整体假死（与 start/stop 服务同类问题）。
/// 解法与 console_start_service 一致：改为 async fn + spawn_blocking 甩到线程池。
#[tauri::command]
pub async fn console_fetch_ads(position: String) -> Result<Vec<AdItem>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let url = format!("{}/app/marketing/ad/list", auth::user_api_url());
        let body = serde_json::json!({ "position": position });
        let text = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .build()
            .map_err(|e| format!("build client: {e}"))?
            .post(&url)
            .json(&body)
            .send()
            .map_err(|e| format!("ad list request: {e}"))?
            .text()
            .map_err(|e| format!("ad list body: {e}"))?;
        auth::parse_cool_response::<Vec<AdItem>>(&text).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("spawn_blocking join: {e}"))?
}

// ── 版本检查与自动更新命令 ──────────────────────────────────────────

/// 检查 GitHub 最新版本，与当前版本比较。
/// 如果有新版本，返回 UpdateInfo（含下载链接、release notes）；
/// 无新版本时 has_update = false。
#[tauri::command]
pub async fn console_check_update(app: AppHandle) -> Result<crate::updater::UpdateInfo, String> {
    let current = app.package_info().version.to_string();
    tauri::async_runtime::spawn_blocking(move || crate::updater::check_update(&current))
        .await
        .map_err(|e| format!("spawn_blocking join: {e}"))?
}

/// 下载最新安装包到 ~/.vibe-trading/updates/，流式推送 update://progress 事件。
/// 前端应先调 console_check_update 拿到 UpdateInfo 再调此命令。
#[tauri::command]
pub async fn console_download_update(
    app: AppHandle,
    info: crate::updater::UpdateInfo,
) -> Result<String, String> {
    let app2 = app.clone();
    tauri::async_runtime::spawn_blocking(move || crate::updater::download_update(&info, &app2))
        .await
        .map_err(|e| format!("spawn_blocking join: {e}"))?
        .map(|p| p.to_string_lossy().to_string())
}

/// 用系统命令打开已下载的安装包（macOS 打开 DMG，Windows 启动安装程序），
/// 然后退出当前进程。下次启动必定运行 boot/prepare，从新版 bundle 刷新 runtime 代码。
#[tauri::command]
pub async fn console_install_update(
    app: AppHandle,
    installing: State<'_, InstallingFlag>,
    service: State<'_, SharedChild>,
    service_port: State<'_, SharedPort>,
    path: String,
) -> Result<(), String> {
    if !can_install_update(installing.0.load(Ordering::SeqCst)) {
        return Err("依赖安装尚未完成，请等待后再安装更新".to_string());
    }
    let package = PathBuf::from(path);
    if !package.exists() {
        let path = package.display();
        return Err(format!("安装包不存在: {path}"));
    }
    let shared = service.inner().clone();
    let service_port = service_port.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        crate::updater::install_update_then(
            &package,
            move || stop_service_blocking(&shared, &service_port),
            crate::updater::install_update,
            move || app.exit(0),
        )
    })
    .await
    .map_err(|e| format!("install update task join: {e}"))?
}

// ── 测试 ────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::sync::mpsc;
    use std::sync::{Arc, Mutex};

    #[test]
    fn login_without_remembering_has_no_deadline() {
        assert_eq!(remember_until(false, 100), None);
    }

    #[test]
    fn public_config_roundtrips_enable_ad() {
        // 服务端返回 enableAd 时,应保留并序列化回 camelCase 字段
        let cfg: PublicConfig = serde_json::from_str(
            r#"{"officialUrl":"https://example.com","enableLogin":true,"enableAd":true}"#,
        )
        .unwrap();
        let value = serde_json::to_value(&cfg).unwrap();
        assert_eq!(value["enableAd"], true);
    }

    #[test]
    fn public_config_defaults_enable_ad_true_when_server_omits_it() {
        // 服务端缺 enableAd 字段时,默认开启(需 serde default)
        let cfg: PublicConfig = serde_json::from_str(r#"{"enableLogin":true}"#).unwrap();
        let value = serde_json::to_value(&cfg).unwrap();
        assert_eq!(value["enableAd"], true);
    }
    #[test]
    fn remembered_login_has_a_fourteen_day_deadline() {
        assert_eq!(
            remember_until(true, 100),
            Some(100 + auth::REMEMBER_LOGIN_SECS)
        );
    }

    #[test]
    fn membership_level_change_invalidates_cached_credential() {
        let previous = auth::MemberLevel {
            id: 1,
            name: "普通会员".into(),
            code: Some("normal".into()),
            level_value: 1,
            expire_time: None,
        };
        let current = auth::MemberLevel {
            id: 2,
            name: "Pro".into(),
            code: Some("pro".into()),
            level_value: 20,
            expire_time: Some("2026-12-31 23:59:59".into()),
        };

        assert!(membership_level_changed(Some(&previous), Some(&current)));
        assert!(!membership_level_changed(Some(&previous), Some(&previous)));
        assert!(!membership_level_changed(None, Some(&current)));
    }

    #[test]
    fn member_public_key_upload_failure_does_not_block_finalize_login() {
        let _api_url_lock = auth::USER_API_URL_TEST_LOCK
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let api_url = format!("http://{}", listener.local_addr().unwrap());
        let (upload_finished_sender, upload_finished_receiver) = mpsc::channel();
        std::thread::spawn(move || {
            for response in [
                b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    .as_slice(),
                b"HTTP/1.1 200 OK\r\nContent-Length: 62\r\nConnection: close\r\n\r\n{\"code\":1001,\"message\":\"upstream-detail-should-not-propagate\"}"
                    .as_slice(),
            ] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = [0_u8; 1024];
                stream.read(&mut request).unwrap();
                stream.write_all(response).unwrap();
            }
            upload_finished_sender.send(()).unwrap();
        });
        let previous_api_url = std::env::var("VIBE_USER_API_URL").ok();
        std::env::set_var("VIBE_USER_API_URL", api_url);

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let auth_state = AuthState(Arc::new(Mutex::new(None)));
        let raw = LoginRaw {
            token: "test-token".into(),
            refresh_token: "test-refresh-token".into(),
            expire: 60,
            refresh_expire: 120,
            has_password: true,
        };
        let result = finalize_login(raw, true, false, &layout, &auth_state, String::new());
        upload_finished_receiver
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();

        match previous_api_url {
            Some(value) => std::env::set_var("VIBE_USER_API_URL", value),
            None => std::env::remove_var("VIBE_USER_API_URL"),
        }

        assert!(result.is_ok());
        assert!(auth_state.0.lock().unwrap().is_some());
    }

    #[test]
    fn invalid_authentication_clears_memory_and_persisted_tokens() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let session = auth::UserSession {
            token: "access-token".into(),
            refresh_token: "refresh-token".into(),
            expire_at: 1_800_000_000,
            refresh_expire_at: 1_900_000_000,
            remember_until: Some(1_900_000_000),
            user_info: None,
            vip: None,
        };
        auth::write_env_token_section(&layout, &session).unwrap();
        let state = AuthState(Arc::new(Mutex::new(Some(session))));

        assert!(clear_invalid_authentication_for_token(&state, &layout, "access-token").unwrap());

        assert!(state.0.lock().unwrap().is_none());
        assert!(auth::read_env_token_section_at(&layout, 1_700_000_000).is_none());
    }

    #[test]
    fn stale_profile_failure_does_not_clear_a_newer_login() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let old_session = auth::UserSession {
            token: "old-token".into(),
            refresh_token: "old-refresh".into(),
            expire_at: 1_800_000_000,
            refresh_expire_at: 1_900_000_000,
            remember_until: Some(1_900_000_000),
            user_info: None,
            vip: None,
        };
        let new_session = auth::UserSession {
            token: "new-token".into(),
            refresh_token: "new-refresh".into(),
            ..old_session.clone()
        };
        auth::write_env_token_section(&layout, &new_session).unwrap();
        let state = AuthState(Arc::new(Mutex::new(Some(new_session.clone()))));

        let cleared =
            clear_invalid_authentication_for_token(&state, &layout, &old_session.token).unwrap();

        assert!(!cleared);
        assert_eq!(state.0.lock().unwrap().as_ref().unwrap().token, "new-token");
        assert_eq!(
            auth::read_env_token_section_at(&layout, 1_700_000_000)
                .unwrap()
                .token,
            "new-token"
        );
    }

    #[test]
    fn only_unauthorized_api_errors_invalidate_authentication() {
        assert!(is_authentication_error(&AuthError::Api {
            code: 401,
            message: "未授权".into(),
        }));
        assert!(is_authentication_error(&AuthError::Api {
            code: 1001,
            message: "登录失效~".into(),
        }));
        assert!(!is_authentication_error(&AuthError::Api {
            code: 1001,
            message: "数据库暂时不可用".into(),
        }));
        assert!(!is_authentication_error(&AuthError::Api {
            code: 500,
            message: "服务繁忙".into(),
        }));
    }

    #[test]
    fn absent_session_after_validation_is_not_restored() {
        let session = auth::UserSession {
            token: "old-token".into(),
            refresh_token: "old-refresh".into(),
            expire_at: 1_800_000_000,
            refresh_expire_at: 1_900_000_000,
            remember_until: Some(1_900_000_000),
            user_info: None,
            vip: None,
        };
        let state = AuthState(Arc::new(Mutex::new(None)));

        assert!(!session_is_still_current(&state, &session));
    }

    #[test]
    fn invalidation_waits_for_the_auth_lock_before_clearing_persistence() {
        let state = AuthState(Arc::new(Mutex::new(None)));
        let held_lock = state.0.lock().unwrap();
        let (cleared, observed_clear) = std::sync::mpsc::channel();
        let worker_state = state.clone();
        let worker = std::thread::spawn(move || {
            invalidate_authentication(&worker_state, || {
                cleared.send(()).unwrap();
                Ok(())
            })
            .unwrap();
        });

        assert!(observed_clear
            .recv_timeout(std::time::Duration::from_millis(50))
            .is_err());
        drop(held_lock);
        observed_clear
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap();
        worker.join().unwrap();
    }

    fn authenticated_state(layout: &Layout) -> AuthState {
        let session = auth::UserSession {
            token: "access-token".into(),
            refresh_token: "refresh-token".into(),
            expire_at: 1_800_000_000,
            refresh_expire_at: 1_900_000_000,
            remember_until: Some(1_900_000_000),
            user_info: None,
            vip: None,
        };
        auth::write_env_token_section(layout, &session).unwrap();
        AuthState(Arc::new(Mutex::new(Some(session))))
    }

    fn spawn_exit_vip_server(status: &str, body: &str) -> (u16, mpsc::Receiver<String>) {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let status = status.to_string();
        let body = body.to_string();
        let (request_sender, request_receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0_u8; 2048];
            let read = stream.read(&mut request).unwrap();
            request_sender
                .send(String::from_utf8_lossy(&request[..read]).into_owned())
                .unwrap();
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });
        (port, request_receiver)
    }

    #[test]
    fn logout_to_custom_running_service_clears_auth_only_after_successful_post() {
        tauri::async_runtime::block_on(async {
            let tmp = tempfile::tempdir().unwrap();
            let layout = Layout::new(&tmp.path().join(".vibe-trading"));
            let auth_state = authenticated_state(&layout);
            let (port, request) = spawn_exit_vip_server("200 OK", r#"{"custom_configured":true}"#);
            let service_port = Arc::new(Mutex::new(Some(port)));

            let readiness = logout_to_custom_inner(
                &service_port,
                &auth_state,
                &RuntimeOperationLock::new(),
                &layout,
            )
            .await
            .unwrap();

            assert!(readiness.custom_configured);
            assert!(auth_state.0.lock().unwrap().is_none());
            assert_eq!(auth::read_llm_mode(&layout), auth::DesktopLlmMode::Custom);
            assert!(auth::read_env_token_section_at(&layout, 1_700_000_000).is_none());
            let request = request.recv_timeout(Duration::from_secs(1)).unwrap();
            assert!(request.starts_with("POST /settings/llm/desktop-exit-vip HTTP/1.1"));
        });
    }

    #[test]
    fn logout_to_custom_running_service_preserves_auth_when_post_fails() {
        tauri::async_runtime::block_on(async {
            let tmp = tempfile::tempdir().unwrap();
            let layout = Layout::new(&tmp.path().join(".vibe-trading"));
            let auth_state = authenticated_state(&layout);
            let (port, _) = spawn_exit_vip_server("500 Internal Server Error", "failed");
            let service_port = Arc::new(Mutex::new(Some(port)));

            let result = logout_to_custom_inner(
                &service_port,
                &auth_state,
                &RuntimeOperationLock::new(),
                &layout,
            )
            .await;

            assert!(result.is_err());
            assert!(auth_state.0.lock().unwrap().is_some());
            assert_eq!(auth::read_llm_mode(&layout), auth::DesktopLlmMode::Vip);
            assert!(auth::read_env_token_section_at(&layout, 1_700_000_000).is_some());
        });
    }

    #[test]
    fn logout_to_custom_rejects_busy_runtime_without_clearing_auth() {
        tauri::async_runtime::block_on(async {
            let tmp = tempfile::tempdir().unwrap();
            let layout = Layout::new(&tmp.path().join(".vibe-trading"));
            let auth_state = authenticated_state(&layout);
            let runtime_operation = RuntimeOperationLock::new();
            let _held = runtime_operation.try_acquire().unwrap();

            let result = logout_to_custom_inner(
                &Arc::new(Mutex::new(None)),
                &auth_state,
                &runtime_operation,
                &layout,
            )
            .await;

            assert_eq!(result.unwrap_err(), "运行环境正在维护，请等待当前操作完成");
            assert!(auth_state.0.lock().unwrap().is_some());
            assert_eq!(auth::read_llm_mode(&layout), auth::DesktopLlmMode::Vip);
        });
    }

    #[test]
    fn logout_to_custom_preserves_auth_when_atomic_persistence_fails() {
        tauri::async_runtime::block_on(async {
            let tmp = tempfile::tempdir().unwrap();
            let layout = Layout::new(&tmp.path().join("not-a-directory"));
            fs::write(&layout.root, "blocks mkdir").unwrap();
            let auth_state = AuthState(Arc::new(Mutex::new(Some(auth::UserSession {
                token: "access-token".into(),
                refresh_token: "refresh-token".into(),
                expire_at: 1_800_000_000,
                refresh_expire_at: 1_900_000_000,
                remember_until: Some(1_900_000_000),
                user_info: None,
                vip: None,
            }))));

            let result = logout_to_custom_inner(
                &Arc::new(Mutex::new(None)),
                &auth_state,
                &RuntimeOperationLock::new(),
                &layout,
            )
            .await;

            assert!(result.is_err());
            assert!(auth_state.0.lock().unwrap().is_some());
        });
    }

    #[test]
    fn logout_to_custom_stopped_service_persists_custom_without_vip_environment() {
        tauri::async_runtime::block_on(async {
            let tmp = tempfile::tempdir().unwrap();
            let layout = Layout::new(&tmp.path().join(".vibe-trading"));
            let auth_state = authenticated_state(&layout);
            let service_port = Arc::new(Mutex::new(None));

            logout_to_custom_inner(
                &service_port,
                &auth_state,
                &RuntimeOperationLock::new(),
                &layout,
            )
            .await
            .unwrap();

            assert_eq!(auth::read_llm_mode(&layout), auth::DesktopLlmMode::Custom);
            let cmd = crate::sidecar::build_cmd_with_vip(
                Path::new("/fake/python"),
                Path::new("/fake/agent"),
                8899,
                Path::new("/fake/libs"),
                Path::new("/fake/sessions"),
                None,
            );
            for key in [
                "VIBE_DESKTOP_VIP_PROVISIONED",
                "VIBE_DESKTOP_VIP_API_KEY",
                "VIBE_DESKTOP_VIP_BASE_URL",
                "VIBE_DESKTOP_VIP_MODELS_JSON",
            ] {
                assert!(cmd
                    .get_envs()
                    .any(|(name, value)| name == key && value.is_none()));
            }
        });
    }

    #[test]
    fn stopped_custom_readiness_uses_staged_provider_defaults_and_rejects_placeholders() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let providers = layout.runtime_agent.join("src/providers");
        fs::create_dir_all(&providers).unwrap();
        fs::write(
            providers.join("llm_providers.json"),
            r#"[{"name":"openai","api_key_env":"OPENAI_API_KEY","base_url_env":"OPENAI_BASE_URL","default_model":"gpt-test","default_base_url":"https://api.example/v1","api_key_required":true},{"name":"openai-codex","api_key_env":null,"base_url_env":"CODEX_BASE_URL","default_model":"codex","default_base_url":"https://oauth.example","api_key_required":false,"auth_type":"oauth"},{"name":"ollama","api_key_env":null,"base_url_env":"OLLAMA_BASE_URL","default_model":"qwen","default_base_url":"http://localhost:11434","api_key_required":false}]"#,
        )
        .unwrap();
        fs::create_dir_all(&layout.root).unwrap();

        fs::write(
            &layout.user_env,
            "LANGCHAIN_PROVIDER=openai\nLANGCHAIN_MODEL_NAME=gpt-test\nOPENAI_BASE_URL=https://api.example/v1\nOPENAI_API_KEY=real-key\n",
        )
        .unwrap();
        assert!(stopped_custom_readiness(&layout).custom_configured);

        fs::write(
            &layout.user_env,
            "LANGCHAIN_PROVIDER=openai\nOPENAI_API_KEY=sk-xxx\n",
        )
        .unwrap();
        assert!(!stopped_custom_readiness(&layout).custom_configured);

        fs::write(&layout.user_env, "LANGCHAIN_PROVIDER=openai-codex\n").unwrap();
        assert!(!stopped_custom_readiness(&layout).custom_configured);

        fs::write(
            &layout.user_env,
            "LANGCHAIN_PROVIDER=ollama\nLANGCHAIN_MODEL_NAME=llama3\nOLLAMA_BASE_URL=http://localhost:11434\n",
        )
        .unwrap();
        assert!(stopped_custom_readiness(&layout).custom_configured);

        fs::write(providers.join("llm_providers.json"), "not-json").unwrap();
        assert!(!stopped_custom_readiness(&layout).custom_configured);
    }

    #[test]
    fn custom_readiness_maps_python_snake_case_to_tauri_camel_case() {
        let python: PythonCustomReadiness =
            serde_json::from_str(r#"{"custom_configured":true}"#).unwrap();
        let tauri = serde_json::to_value(map_custom_readiness(python)).unwrap();

        assert_eq!(tauri, serde_json::json!({ "customConfigured": true }));
    }
    use std::fs;

    #[test]
    fn run_blocking_drops_reqwest_client_outside_tokio_task() {
        tauri::async_runtime::block_on(async {
            let result = run_blocking(|| {
                let client = reqwest::blocking::Client::builder().build().unwrap();
                drop(client);
                7
            })
            .await;

            assert_eq!(result, Ok(7));
        });
    }

    #[test]
    fn clear_logs_removes_log_files_keeps_others() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        fs::write(dir.join("sidecar-2026-07-09.log"), "a").unwrap();
        fs::write(dir.join("sidecar.log"), "b").unwrap();
        fs::write(dir.join("notes.txt"), "c").unwrap();
        let removed = clear_logs_in(dir).unwrap();
        assert_eq!(removed, 2);
        assert!(!dir.join("sidecar-2026-07-09.log").exists());
        assert!(!dir.join("sidecar.log").exists());
        assert!(dir.join("notes.txt").exists(), "非日志文件应保留");
    }

    #[test]
    fn env_status_not_installed_when_no_venv() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        let layout = crate::runtime_dir::Layout::new(&home);
        assert_eq!(compute_env_status(&layout), EnvStatus::NotInstalled);
    }

    #[test]
    fn env_status_incomplete_when_venv_without_marker() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        let layout = crate::runtime_dir::Layout::new(&home);
        fs::create_dir_all(layout.venv_python.parent().unwrap()).unwrap();
        fs::write(&layout.venv_python, "#!/bin/sh\n").unwrap();
        assert_eq!(compute_env_status(&layout), EnvStatus::Incomplete);
    }

    #[test]
    fn env_status_ready_when_venv_and_marker_present() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        let layout = crate::runtime_dir::Layout::new(&home);
        fs::create_dir_all(layout.venv_python.parent().unwrap()).unwrap();
        fs::write(&layout.venv_python, "#!/bin/sh\n").unwrap();
        fs::write(layout.venv_dir.join(".requirements_hash"), "deadbeef").unwrap();
        assert_eq!(compute_env_status(&layout), EnvStatus::Ready);
    }

    #[test]
    fn channels_start_url_targets_local_backend() {
        assert_eq!(
            channels_start_url(8899),
            "http://127.0.0.1:8899/channels/start"
        );
    }

    #[test]
    fn bootstrap_cmd_runs_cli_bootstrap_sse() {
        let cmd = build_bootstrap_cmd(Path::new("/rt/bin/python3"), Path::new("/rt/agent"));
        let args: Vec<&str> = cmd.get_args().map(|a| a.to_str().unwrap()).collect();
        let joined = args.join(" ");
        assert!(joined.contains("bootstrap"), "args: {joined}");
        assert!(joined.contains("--sse"), "args: {joined}");
        let mut has_pythonpath = false;
        for (k, v) in cmd.get_envs() {
            if k.to_str() == Some("PYTHONPATH") && v.and_then(|x| x.to_str()) == Some("/rt/agent") {
                has_pythonpath = true;
            }
        }
        assert!(
            has_pythonpath,
            "bootstrap 子进程须设 PYTHONPATH 指向 runtime agent"
        );
    }

    #[test]
    fn validate_channel_accepts_known_channels() {
        for ok in [
            "telegram", "slack", "discord", "weixin", "wecom", "qq", "napcat", "feishu", "dingtalk",
        ] {
            assert!(validate_channel(ok).is_ok(), "{ok} 应合法");
        }
    }

    #[test]
    fn validate_channel_rejects_injection_and_garbage() {
        for bad in [
            "",
            "tel egram",
            "tel;egram",
            "../etc",
            "Telegram",
            "a$b",
            "with space",
        ] {
            assert!(validate_channel(bad).is_err(), "{bad:?} 应被拒");
        }
    }

    #[test]
    fn channel_dep_cmd_installs_extra_with_no_input() {
        let cmd = build_channel_dep_cmd(Path::new("/v/bin/python3"), "telegram");
        let args: Vec<&str> = cmd.get_args().map(|a| a.to_str().unwrap()).collect();
        let joined = args.join(" ");
        assert!(joined.contains("--no-input"), "args: {joined}");
        assert!(
            joined.contains("vibe-trading-ai[telegram]"),
            "args: {joined}"
        );
    }

    #[test]
    fn parse_sse_progress_frame_extracts_stage_and_message() {
        let ev = parse_sse_data(
            "progress",
            r#"{"stage": "installing", "message": "Collecting pandas"}"#,
        )
        .expect("progress 帧应解析成功");
        assert_eq!(ev.stage, "installing");
        assert_eq!(ev.message, "Collecting pandas");
        assert!(ev.ok, "progress 帧默认 ok=true");
    }

    #[test]
    fn parse_sse_done_frame_falls_back_to_event_name_for_stage() {
        // done/failed 帧的 data 不带 stage,应退回 event 名。
        let ev = parse_sse_data("done", r#"{"ok": true, "message": "environment ready"}"#)
            .expect("done 帧应解析成功");
        assert_eq!(ev.stage, "done");
        assert_eq!(ev.message, "environment ready");
        assert!(ev.ok);
    }

    #[test]
    fn parse_sse_failed_frame_preserves_ok_false() {
        let ev = parse_sse_data(
            "failed",
            r#"{"ok": false, "message": "deps incomplete: numpy"}"#,
        )
        .expect("failed 帧应解析成功");
        assert_eq!(ev.stage, "failed");
        assert!(!ev.ok, "failed 帧须保留 ok=false 供前端标红");
    }

    #[test]
    fn parse_sse_rejects_non_json_data() {
        assert!(parse_sse_data("progress", "not json").is_none());
    }

    #[test]
    fn clear_venv_dir_removes_existing_venv() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        let layout = crate::runtime_dir::Layout::new(&home);
        fs::create_dir_all(layout.venv_dir.join("lib")).unwrap();
        fs::write(layout.venv_dir.join("marker"), "x").unwrap();
        assert!(layout.venv_dir.exists());
        clear_venv_dir(&layout).expect("清理应成功");
        assert!(!layout.venv_dir.exists(), "venv 应被删除");
    }

    #[test]
    fn clear_venv_dir_idempotent_when_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        let layout = crate::runtime_dir::Layout::new(&home);
        assert!(!layout.venv_dir.exists());
        clear_venv_dir(&layout).expect("缺失时应幂等成功");
        assert!(!layout.venv_dir.exists());
    }

    #[cfg(windows)]
    #[test]
    fn legacy_uninstall_prefers_registry_discovered_path() {
        let tmp = tempfile::tempdir().unwrap();
        // 模拟用户装到 D 盘:uninstall.exe 不在 %LOCALAPPDATA%\Vibe Trading
        let install_dir = tmp.path().join("Custom Install Dir");
        fs::create_dir_all(&install_dir).unwrap();
        let uninstaller = install_dir.join("uninstall.exe");
        fs::write(&uninstaller, b"MZ").unwrap();

        let entry = RegistryUninstallInfo {
            executable: uninstaller.clone(),
            args: vec!["/S".into()],
            subkey: "{GUID}".into(),
        };
        let launched = Arc::new(Mutex::new(None));
        let capture = Arc::clone(&launched);
        // 注册表入口存在 → 应启动注册表发现的卸载器(非固定路径、非"未找到")
        let result = uninstall_legacy_windows_inner(Some(entry), move |exe, args| {
            *capture.lock().unwrap() = Some((exe.to_path_buf(), args.to_vec()));
            Ok(())
        });
        assert!(result.is_ok(), "注册表发现的卸载器应被启动: {result:?}");
        let (exe, args) = launched.lock().unwrap().clone().expect("launcher 应被调用");
        assert_eq!(exe, uninstaller, "应启动注册表发现的卸载器");
        assert_eq!(args, vec!["/S".to_string()], "参数应原样传递");
    }

    #[cfg(windows)]
    #[test]
    fn legacy_uninstall_falls_back_to_fixed_path_when_registry_empty() {
        let tmp = tempfile::tempdir().unwrap();
        // 注册表无记录 → 回退到固定路径;固定路径也不存在 → 报"未找到"
        let result = uninstall_legacy_windows_inner(None, |_, _| Ok(()));
        assert!(result.is_err(), "注册表与固定路径都缺失时不应静默成功");
        let err = result.unwrap_err();
        assert!(
            err.contains("未找到旧版 Vibe Trading"),
            "错误应提示未找到: {err}"
        );
    }

    #[cfg(windows)]
    #[test]
    fn legacy_uninstall_skips_registry_entry_whose_exe_is_missing() {
        let tmp = tempfile::tempdir().unwrap();
        // 注册表指向的 exe 已不存在(残留注册表项) → 应继续回退固定路径,而不是尝试启动
        let ghost = tmp.path().join("ghost").join("uninstall.exe"); // 不创建文件
        let entry = RegistryUninstallInfo {
            executable: ghost.clone(),
            args: vec![],
            subkey: "{GHOST}".into(),
        };
        let result = uninstall_legacy_windows_inner(Some(entry), |_, _| {
            panic!("launcher 不应在 exe 缺失时被调用");
        });
        assert!(result.is_err(), "exe 缺失时不应成功");
        let err = result.unwrap_err();
        assert!(err.contains("未找到旧版 Vibe Trading"), "应报未找到: {err}");
    }

    #[cfg(windows)]
    #[test]
    fn parse_uninstall_string_handles_quoted_paths_and_args() {
        assert_eq!(
            parse_uninstall_string(r#""C:\Program Files\Vibe Trading\uninstall.exe" /S"#),
            Some((
                PathBuf::from(r"C:\Program Files\Vibe Trading\uninstall.exe"),
                vec!["/S".to_string()]
            ))
        );
        assert_eq!(
            parse_uninstall_string(r#"C:\Tools\uninst.exe"#),
            Some((PathBuf::from(r"C:\Tools\uninst.exe"), vec![]))
        );
        // 空字符串 → None
        assert_eq!(parse_uninstall_string(""), None);
        assert_eq!(parse_uninstall_string("   "), None);
    }

    #[test]
    fn legacy_uninstall_uses_fixed_platform_install_paths() {
        let home = Path::new("C:/Users/tester");
        assert_eq!(
            legacy_windows_uninstaller_path(Path::new("C:/Users/tester/AppData/Local")),
            Path::new("C:/Users/tester/AppData/Local/Vibe Trading/uninstall.exe")
        );

        let mac_candidates = legacy_macos_app_paths(home);
        assert_eq!(
            mac_candidates[0],
            Path::new("C:/Users/tester/Applications/Vibe Trading.app")
        );
        assert_eq!(
            mac_candidates[1],
            Path::new("/Applications/Vibe Trading.app")
        );
    }

    #[test]
    fn legacy_uninstall_reports_missing_installation_without_touching_user_data() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("home");
        let user_data = home.join(".vibe-trading");
        fs::create_dir_all(&user_data).unwrap();
        fs::write(user_data.join("sessions.json"), "keep").unwrap();

        assert!(find_existing_legacy_path([
            home.join("AppData/Local/Vibe Trading/uninstall.exe"),
            home.join("Applications/Vibe Trading.app"),
        ])
        .is_none());
        assert!(user_data.join("sessions.json").exists());
    }

    #[test]
    fn quit_confirmation_required_when_running_or_installing() {
        assert!(needs_quit_confirmation(true, false), "运行中应确认");
        assert!(needs_quit_confirmation(false, true), "安装中应确认");
        assert!(needs_quit_confirmation(true, true), "同时进行更应确认");
        assert!(!needs_quit_confirmation(false, false), "空闲直接退出");
    }

    #[test]
    fn update_install_is_blocked_while_bootstrap_is_running() {
        assert!(can_install_update(false));
        assert!(!can_install_update(true));
    }

    #[test]
    fn runtime_operation_lock_rejects_concurrent_operations() {
        let lock = RuntimeOperationLock::new();
        let operation = lock.try_acquire().expect("first operation acquires lock");
        assert!(
            lock.try_acquire().is_none(),
            "second operation must be rejected"
        );
        drop(operation);
        assert!(
            lock.try_acquire().is_some(),
            "lock releases when operation completes"
        );
    }

    #[test]
    fn environment_decide_reports_deps_and_runtime_status() {
        // 依赖缺失 + runtime 全新（从未安装 → 需修复：首次安装后才有版本可言）
        let (deps, runtime) = decide_environment(EnvStatus::NotInstalled, true, None, "1.0.0");
        assert!(!deps);
        assert!(!runtime, "从未安装视为运行时代码不全，触发安装");
        // 依赖就绪 + 代码最新
        let (deps, runtime) = decide_environment(EnvStatus::Ready, true, Some("1.0.0"), "1.0.0");
        assert!(deps && runtime);
        // 依赖就绪 + 版本落后
        let (deps, runtime) = decide_environment(EnvStatus::Ready, true, Some("1.0.0"), "1.1.0");
        assert!(deps && !runtime);
        // marker 残留但代码目录缺失 → 判旧，需要修复
        let (deps, runtime) = decide_environment(EnvStatus::Ready, false, Some("1.0.0"), "1.0.0");
        assert!(deps && !runtime, "runtime/agent 缺失视为运行时代码不全");
        // 依赖未完成 + 版本落后
        let (deps, runtime) =
            decide_environment(EnvStatus::Incomplete, true, Some("0.9.0"), "1.0.0");
        assert!(!deps && !runtime);
    }

    #[test]
    fn environment_report_serializes_with_frontend_field_names() {
        let report = EnvironmentReport {
            env: EnvStatus::Ready,
            installed_version: Some("1.0.0".into()),
            bundle_version: "1.0.0".into(),
            deps_ok: true,
            runtime_ok: true,
        };

        let value = serde_json::to_value(report).expect("serialize environment report");
        assert_eq!(value["installedVersion"], "1.0.0");
        assert_eq!(value["bundleVersion"], "1.0.0");
        assert_eq!(value["depsOk"], true);
        assert_eq!(value["runtimeOk"], true);
    }

    #[test]
    fn status_report_preserves_running_service_port() {
        let report = build_status_report(EnvStatus::Ready, true, Some(8899));
        assert!(report.service_running);
        assert_eq!(report.port, Some(8899));
    }

    #[test]
    fn status_report_hides_port_when_service_is_stopped() {
        let report = build_status_report(EnvStatus::Ready, false, Some(8899));
        assert!(!report.service_running);
        assert_eq!(report.port, None);
    }

    #[test]
    fn unauthenticated_start_can_fall_back_to_env_configuration() {
        assert!(can_start_without_vip(&AuthError::NotAuthenticated));
        assert!(can_start_without_vip(&AuthError::LoginExpired));
        assert!(!can_start_without_vip(&AuthError::Network {
            message: "offline".into(),
        }));
        assert!(!can_start_without_vip(&AuthError::Credential {
            message: "membership unavailable".into(),
        }));
    }
}
