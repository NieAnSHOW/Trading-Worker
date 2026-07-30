//! 桌面控制台 IPC —— 环境/服务状态、启停服务、bootstrap 转发、打开 WebUI/日志。
//! 逻辑尽量做成纯函数(可 cargo 测);Tauri command 是薄壳(设计 D3)。

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::Path;
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{AppHandle, Emitter, State};

use crate::auth::{self, AuthError, AuthState, Captcha, LoginRaw, UserInfo};
use crate::runtime_dir::Layout;

pub type SharedChild = Arc<Mutex<Option<Child>>>;

/// 依赖安装(bootstrap)进行中标志。托盘「退出」据此判断是否需要二次确认。
pub struct InstallingFlag(pub Arc<AtomicBool>);

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

/// 导入桌面服务配置后返回给前端的安全视图。
/// 不暴露原始 JSON 或本地配置文件路径。
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopConfigImportView {
    pub user_api_url: String,
}

/// console_auth_status 返回。
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AuthStatusView {
    pub authenticated: bool,
    pub user_info: Option<UserInfo>,
    pub expire_at: Option<i64>,
}

fn unauthenticated_status() -> AuthStatusView {
    AuthStatusView {
        authenticated: false,
        user_info: None,
        expire_at: None,
    }
}

#[tauri::command]
pub fn console_import_user_api_config(content: String) -> Result<DesktopConfigImportView, String> {
    let layout = Layout::from_home()?;
    let user_api_url =
        crate::desktop_config::import_user_api_config(&layout.desktop_config, &content)?;
    Ok(DesktopConfigImportView { user_api_url })
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
pub fn console_status(state: State<'_, SharedChild>) -> Result<StatusReport, String> {
    let layout = Layout::from_home()?;
    let running = state.lock().unwrap().is_some();
    Ok(StatusReport {
        env: compute_env_status(&layout),
        service_running: running,
        port: None,
    })
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
) -> Result<(), String> {
    let layout = Layout::from_home()?;
    prepare_runtime_from_bundle(&app, &layout)?;
    let res = crate::resources::Resources::resolve(&app).map_err(|e| format!("resources: {e}"))?;
    let mut child = build_bootstrap_cmd(&res.runtime_python, &layout.runtime_agent)
        .spawn()
        .map_err(|e| format!("spawn bootstrap: {e}"))?;
    let stdout = child.stdout.take().ok_or("no bootstrap stdout")?;
    // 安装期间置位:关闭窗口时据此弹「安装未完成」确认。
    let flag = installing.0.clone();
    flag.store(true, Ordering::SeqCst);
    let app2 = app.clone();
    std::thread::spawn(move || {
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
    auth_state: State<'_, AuthState>,
) -> Result<u16, ServiceStartError> {
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
                let auth_state = auth_state.inner().clone();
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
    let shared = state.inner().clone();
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
            shared.lock().unwrap().replace(child);
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

        match auth::refresh_user_info(&mut session, auth::fetch_user_info) {
            Ok(()) => {
                let mut guard = auth_state.0.lock().unwrap();
                match guard.as_mut() {
                    Some(current) if current.token == session.token => {
                        current.user_info = session.user_info.clone();
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
#[tauri::command]
pub async fn console_stop_service(state: State<'_, SharedChild>) -> Result<(), String> {
    // 单独语句取走 child,确保 MutexGuard 不跨 await(Future 须 Send)。
    let child = state.lock().unwrap().take();
    if let Some(mut child) = child {
        // terminate 内部 child.wait() 同步等子进程退出;甩到阻塞线程池避免卡 main thread。
        let _ = tauri::async_runtime::spawn_blocking(move || crate::sidecar::terminate(&mut child))
            .await;
    }
    Ok(())
}

/// 在系统默认浏览器打开 WebUI。
#[tauri::command]
pub fn console_open_webui(port: u16) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{port}/");
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

/// 安装单个消息渠道的可选依赖:用 venv 解释器 spawn
/// `pip install --no-input 'vibe-trading-ai[<channel>]'`,逐行 emit "channeldep://progress"。
/// pip 进度几乎全走 stderr,故 stdout/stderr 各开一线程转发,避免日志空白。
#[tauri::command]
pub async fn console_install_channel_dep(app: AppHandle, channel: String) -> Result<(), String> {
    validate_channel(&channel)?;
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

/// 强制清理虚拟环境:删除 ~/.vibe-trading/venv,便于用户从零重新安装依赖。
#[tauri::command]
pub fn console_clear_venv() -> Result<(), String> {
    let layout = Layout::from_home()?;
    clear_venv_dir(&layout)
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

/// 用系统命令打开已下载的安装包（macOS 打开 DMG，Windows 启动安装程序）。
/// 打开后 app 继续运行，用户手动完成安装。
#[tauri::command]
pub fn console_install_update(path: String) -> Result<(), String> {
    let p = std::path::Path::new(&path);
    if !p.exists() {
        return Err(format!("安装包不存在: {path}"));
    }
    crate::updater::install_update(p)
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
    fn remembered_login_has_a_fourteen_day_deadline() {
        assert_eq!(
            remember_until(true, 100),
            Some(100 + auth::REMEMBER_LOGIN_SECS)
        );
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
        auth::set_user_api_url_for_test(Some(api_url));

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

        auth::set_user_api_url_for_test(None);

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

    #[test]
    fn quit_confirmation_required_when_running_or_installing() {
        assert!(needs_quit_confirmation(true, false), "运行中应确认");
        assert!(needs_quit_confirmation(false, true), "安装中应确认");
        assert!(needs_quit_confirmation(true, true), "同时进行更应确认");
        assert!(!needs_quit_confirmation(false, false), "空闲直接退出");
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
