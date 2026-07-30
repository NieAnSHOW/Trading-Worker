//! 桌面 console 用户登录：cool-admin 客户端 + ~/.vibe-trading/.env token 段读写。
//! 设计见 docs/superpowers/specs/2026-07-05-desktop-console-login-env-design.md
//! 全部同步 + reqwest::blocking，与 console.rs 现有命令风格一致。

use std::collections::HashMap;
use std::fs;
use std::io::Read;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use aes_gcm::aead::{AeadInPlace, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce, Tag};
use base64::Engine;
use hkdf::Hkdf;
use rand_core::{OsRng, RngCore};
use sha2::Sha256;
use x25519_dalek::{PublicKey, StaticSecret};

use crate::runtime_dir::Layout;

const DEFAULT_USER_API_URL: &str = "http://127.0.0.1:8001";

// ── 可覆盖配置 ──
// 业务接口（captcha/sms/login/person...），独立于大模型 MaaS 接口。
// 默认值与 frontend/src/pages/auth/Login.tsx 对齐。 https://trading-server.nieanshow.cn
pub fn user_api_url_for_config(path: &Path) -> String {
    crate::desktop_config::read_user_api_url(path)
        .unwrap_or_else(|| DEFAULT_USER_API_URL.to_string())
}

pub fn user_api_url() -> String {
    #[cfg(test)]
    if let Some(url) = USER_API_URL_TEST_OVERRIDE.lock().unwrap().clone() {
        return url;
    }

    Layout::from_home()
        .map(|layout| user_api_url_for_config(&layout.desktop_config))
        .unwrap_or_else(|_| DEFAULT_USER_API_URL.to_string())
}

// ── .env 中由本模块管辖的 key（其余 key 不动）──
pub const ENV_KEY_ACCESS: &str = "USER_ACCESS_TOKEN";
pub const ENV_KEY_REFRESH: &str = "USER_REFRESH_TOKEN";
pub const ENV_KEY_EXPIRE: &str = "USER_TOKEN_EXPIRE";
pub const ENV_KEY_REFRESH_EXPIRE: &str = "USER_REFRESH_EXPIRE";
pub const ENV_KEY_REMEMBER_UNTIL: &str = "USER_REMEMBER_UNTIL";
pub const ENV_KEY_LLM_MODE: &str = "DESKTOP_LLM_MODE";
pub const REMEMBER_LOGIN_SECS: i64 = 14 * 24 * 60 * 60;

const MEMBER_CREDENTIAL_INFO: &[u8] = b"vibe-trading/member-credential/v1";
const MAX_MEMBER_CIPHERTEXT_BYTES: usize = 4096;
const MAX_MEMBER_CREDENTIAL_RESPONSE_BYTES: usize = 16 * 1024;
const X25519_SPKI_PREFIX: [u8; 12] = [
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x03, 0x21, 0x00,
];

// ── 错误类型：serde 序列化后由 Tauri 透传给 Vue，按 variant 分流 ──
#[derive(Debug, serde::Serialize)]
#[serde(tag = "variant")]
pub enum AuthError {
    Network { message: String },
    Api { code: i64, message: String },
    LoginExpired,
    EnvWrite { message: String },
    NotAuthenticated,
    Credential { message: String },
}

impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Network { message } => write!(f, "网络错误: {message}"),
            Self::Api { code, message } => write!(f, "API 错误({code}): {message}"),
            Self::LoginExpired => write!(f, "登录已过期"),
            Self::EnvWrite { message } => write!(f, "写入 .env 失败: {message}"),
            Self::NotAuthenticated => write!(f, "未登录"),
            Self::Credential { message } => write!(f, "会员凭据错误: {message}"),
        }
    }
}

// ── 类型：与 cool-admin JSON 对齐（camelCase）──
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MemberLevel {
    pub id: i64,
    pub name: String,
    #[serde(default)]
    pub code: Option<String>,
    #[serde(default)]
    pub level_value: i64,
    #[serde(default)]
    pub expire_time: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UserInfo {
    pub id: i64,
    #[serde(default)]
    pub unionid: Option<String>,
    #[serde(default)]
    pub avatar_url: Option<String>,
    #[serde(default)]
    pub nick_name: Option<String>,
    #[serde(default)]
    pub phone: Option<String>,
    #[serde(default)]
    pub gender: i64,
    #[serde(default)]
    pub status: i64,
    #[serde(default)]
    pub login_type: i64,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub member_level: Option<MemberLevel>,
}

#[derive(Debug, Clone, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoginRaw {
    pub token: String,
    pub refresh_token: String,
    pub expire: i64,         // 相对秒
    pub refresh_expire: i64, // 相对秒
    pub has_password: bool,
}

#[derive(Debug, Clone, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct VipRuntimeCredential {
    #[serde(rename = "baseURL")]
    pub base_url: String,
    pub api_key: String,
    pub models: Vec<String>,
}

/// 会员 API 可安全展示的用量计数；不包含 provider 地址或 API key。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct MemberUsageView {
    pub total_available: i64,
    pub total_granted: i64,
    pub total_used: i64,
    #[serde(default)]
    pub unlimited_quota: bool,
}

/// 当前会员可安全展示的权益；不包含模型、供应商或密钥信息。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct MemberBenefit {
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub description: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct MemberBenefitsView {
    pub benefits: Vec<MemberBenefit>,
}

#[derive(Debug, Clone, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct EncryptedMemberEnvelope {
    pub version: u8,
    pub server_public_key: String,
    pub salt: String,
    pub iv: String,
    pub ciphertext: String,
    pub tag: String,
}

/// v2 响应只对 API Key 使用密封信封，供应商地址和模型列表保持明文业务字段。
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct MemberCipherCredentials {
    version: u8,
    #[serde(rename = "baseURL")]
    base_url: String,
    models: Vec<String>,
    api_key_seal: MemberApiKeySeal,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct MemberApiKeySeal {
    version: u8,
    ephemeral_public_key: String,
    salt: String,
    iv: String,
    ciphertext: String,
    tag: String,
}

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Captcha {
    pub captcha_id: String,
    pub data: String,
}

/// 内存缓存的完整 session（不序列化给前端；vip 和 user_info 都不落盘）。
#[derive(Debug, Clone)]
pub struct UserSession {
    pub token: String,
    pub refresh_token: String,
    pub expire_at: i64,         // 绝对 epoch 秒
    pub refresh_expire_at: i64, // 绝对 epoch 秒
    pub remember_until: Option<i64>,
    pub user_info: Option<UserInfo>,
    pub vip: Option<VipRuntimeCredential>,
}

#[derive(Clone)]
pub struct AuthState(pub Arc<Mutex<Option<UserSession>>>);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DesktopLlmMode {
    Vip,
    Custom,
}

impl DesktopLlmMode {
    fn as_env_value(self) -> &'static str {
        match self {
            Self::Vip => "vip",
            Self::Custom => "custom",
        }
    }
}

fn normalize_llm_mode(value: Option<&str>) -> DesktopLlmMode {
    match value.map(str::trim) {
        Some("custom") => DesktopLlmMode::Custom,
        Some("vip") | None | Some(_) => DesktopLlmMode::Vip,
    }
}

pub fn read_llm_mode(layout: &Layout) -> DesktopLlmMode {
    let content = fs::read_to_string(&layout.user_env).unwrap_or_default();
    let values = parse_env_to_map(&content);
    normalize_llm_mode(values.get(ENV_KEY_LLM_MODE).map(String::as_str))
}

// ── 纯函数：.env key 替换 ──

/// 把 `updates` 的每个 (key, value) 替换进 `content`：
/// 已存在 `key=` 行则替换该行；不存在则在末尾追加。其余行原样保留。
pub fn rewrite_env_keys(content: &str, updates: &[(String, String)]) -> String {
    let mut lines: Vec<String> = content.split('\n').map(str::to_string).collect();
    for (key, value) in updates {
        let prefix = format!("{key}=");
        let mut found = false;
        for line in lines.iter_mut() {
            // 仅匹配行首（允许前导空白），避免误匹配 KEY_OTHER= 之类
            let trimmed = line.trim_start();
            if trimmed.starts_with(&prefix) {
                *line = format!("{key}={value}");
                found = true;
                break;
            }
        }
        if !found {
            // 追加：若末行非空，先补一个空行分隔
            let need_sep = lines.last().map(|s| !s.trim().is_empty()).unwrap_or(false);
            if need_sep {
                lines.push(String::new());
            }
            lines.push(format!("{key}={value}"));
        }
    }
    lines.join("\n")
}

/// 把 .env 文本解析成 key->value map（取每个 `key=value` 行首匹配；忽略注释与空行）。
pub fn parse_env_to_map(content: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for line in content.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with('#') || trimmed.is_empty() {
            continue;
        }
        if let Some(eq) = trimmed.find('=') {
            let key = trimmed[..eq].trim().to_string();
            let value = trimmed[eq + 1..].trim().to_string();
            if !key.is_empty() {
                map.insert(key, value);
            }
        }
    }
    map
}

/// 当前 epoch 秒（单独函数便于测试时不依赖系统时钟副作用）。
pub fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

// ── .env 原子写（tmp→fsync→rename；unix 权限 0600）──

pub fn write_env_atomic(path: &Path, content: &str) -> Result<(), AuthError> {
    let parent = path.parent().ok_or_else(|| AuthError::EnvWrite {
        message: "no parent dir".into(),
    })?;
    fs::create_dir_all(parent).map_err(|e| AuthError::EnvWrite {
        message: format!("mkdir {:?}: {e}", parent),
    })?;
    let file_name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "env".into());
    let tmp = parent.join(format!(".{file_name}.tmp"));

    #[cfg(unix)]
    {
        use std::io::Write;
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&tmp)
            .map_err(|e| AuthError::EnvWrite {
                message: format!("open tmp: {e}"),
            })?;
        f.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|e| AuthError::EnvWrite {
                message: format!("chmod tmp: {e}"),
            })?;
        f.write_all(content.as_bytes())
            .map_err(|e| AuthError::EnvWrite {
                message: format!("write tmp: {e}"),
            })?;
        f.sync_all().map_err(|e| AuthError::EnvWrite {
            message: format!("fsync tmp: {e}"),
        })?;
    }
    #[cfg(not(unix))]
    {
        fs::write(&tmp, content).map_err(|e| AuthError::EnvWrite {
            message: format!("write tmp: {e}"),
        })?;
    }

    fs::rename(&tmp, path).map_err(|e| AuthError::EnvWrite {
        message: format!("rename tmp: {e}"),
    })?;
    Ok(())
}

/// 把登录 token 和规范化的 LLM 模式写进 layout.user_env，其余 key 不动。
pub fn write_env_token_section(layout: &Layout, sess: &UserSession) -> Result<(), AuthError> {
    let Some(remember_until) = sess.remember_until else {
        return clear_env_token_section(layout);
    };
    let path = &layout.user_env;
    let content = fs::read_to_string(path).unwrap_or_default();
    let values = parse_env_to_map(&content);
    let mode = normalize_llm_mode(values.get(ENV_KEY_LLM_MODE).map(String::as_str));
    let updates = vec![
        (ENV_KEY_ACCESS.to_string(), sess.token.clone()),
        (ENV_KEY_REFRESH.to_string(), sess.refresh_token.clone()),
        (ENV_KEY_EXPIRE.to_string(), sess.expire_at.to_string()),
        (
            ENV_KEY_REFRESH_EXPIRE.to_string(),
            sess.refresh_expire_at.to_string(),
        ),
        (
            ENV_KEY_REMEMBER_UNTIL.to_string(),
            remember_until.to_string(),
        ),
        (
            ENV_KEY_LLM_MODE.to_string(),
            mode.as_env_value().to_string(),
        ),
    ];
    let new_content = rewrite_env_keys(&content, &updates);
    write_env_atomic(path, &new_content)
}

/// 清掉登录写入 .env 的 key（置空，复用 rewrite_env_keys）；其余 key 不动。
/// token 为空后 read_env_token_section 返回 None，等价于未登录。
pub fn clear_env_token_section(layout: &Layout) -> Result<(), AuthError> {
    let path = &layout.user_env;
    let content = fs::read_to_string(path).unwrap_or_default();
    let values = parse_env_to_map(&content);
    let keys = [
        ENV_KEY_ACCESS,
        ENV_KEY_REFRESH,
        ENV_KEY_EXPIRE,
        ENV_KEY_REFRESH_EXPIRE,
        ENV_KEY_REMEMBER_UNTIL,
    ];
    let updates: Vec<(String, String)> = keys
        .iter()
        .map(|k| (k.to_string(), String::new()))
        .collect();
    let mut updates = updates;
    updates.push((
        ENV_KEY_LLM_MODE.to_string(),
        normalize_llm_mode(values.get(ENV_KEY_LLM_MODE).map(String::as_str))
            .as_env_value()
            .to_string(),
    ));
    let new_content = rewrite_env_keys(&content, &updates);
    write_env_atomic(path, &new_content)
}

/// 从 layout.user_env 读回 session（重启恢复用）。机密 VIP 凭据绝不从磁盘读取。
pub fn read_env_token_section(layout: &Layout) -> Option<UserSession> {
    read_env_token_section_at(layout, now_secs())
}

/// 从 layout.user_env 读回尚未超过持久化期限的 session。
pub fn read_env_token_section_at(layout: &Layout, now: i64) -> Option<UserSession> {
    let content = fs::read_to_string(&layout.user_env).ok()?;
    let map = parse_env_to_map(&content);
    let remember_until = match map
        .get(ENV_KEY_REMEMBER_UNTIL)
        .and_then(|value| value.trim().parse::<i64>().ok())
    {
        Some(deadline) if deadline > now => deadline,
        _ => {
            let _ = clear_env_token_section(layout);
            return None;
        }
    };
    let access_token = map.get(ENV_KEY_ACCESS)?.trim();
    if access_token.is_empty() {
        return None;
    }
    let refresh_token = map.get(ENV_KEY_REFRESH)?.trim().to_string();
    let expire_at = map.get(ENV_KEY_EXPIRE)?.trim().parse::<i64>().ok()?;
    let refresh_expire_at = map
        .get(ENV_KEY_REFRESH_EXPIRE)?
        .trim()
        .parse::<i64>()
        .ok()?;
    Some(UserSession {
        token: access_token.to_string(),
        refresh_token,
        expire_at,
        refresh_expire_at,
        remember_until: Some(remember_until),
        user_info: None,
        vip: None,
    })
}

// ── cool-admin 响应解析 ──
/// cool-admin 统一包装 {code, data, message}，code==1000 成功。
#[derive(Debug, serde::Deserialize)]
struct CoolResponse {
    pub code: i64,
    #[serde(default)]
    pub data: Option<serde_json::Value>,
    #[serde(default)]
    pub message: Option<String>,
}

/// cool-admin 成功响应：保留 data 和服务端的用户可见消息。
pub struct CoolSuccess<T> {
    pub data: T,
    pub message: String,
}

/// 把 cool-admin 响应体解析为 data 和 message；code!=1000 或解析失败转 AuthError。
pub fn parse_cool_response_with_message<T: serde::de::DeserializeOwned>(
    text: &str,
) -> Result<CoolSuccess<T>, AuthError> {
    let resp: CoolResponse = serde_json::from_str(text).map_err(|e| AuthError::Network {
        message: format!("解析响应失败: {e}"),
    })?;
    if resp.code != 1000 {
        return Err(AuthError::Api {
            code: resp.code,
            message: resp
                .message
                .unwrap_or_else(|| format!("code={}", resp.code)),
        });
    }
    let data =
        serde_json::from_value(resp.data.unwrap_or(serde_json::Value::Null)).map_err(|e| {
            AuthError::Network {
                message: format!("解析 data 字段失败: {e}"),
            }
        })?;
    Ok(CoolSuccess {
        data,
        message: resp.message.unwrap_or_default(),
    })
}

/// 仅需要业务数据的调用保持原有接口；需要显示服务端消息时使用上面的解析器。
pub fn parse_cool_response<T: serde::de::DeserializeOwned>(text: &str) -> Result<T, AuthError> {
    parse_cool_response_with_message(text).map(|response| response.data)
}

// ── cool-admin 客户端（同步 reqwest::blocking）──
// 端点与 frontend/src/lib/apiUser.ts 完全对齐；Authorization 裸 token（无 Bearer）。

const HTTP_TIMEOUT_SECS: u64 = 30;
const MEMBER_PUBLIC_KEY_UPLOAD_FAILED: &str = "会员公钥上报失败";

#[cfg(test)]
pub(crate) static USER_API_URL_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[cfg(test)]
static USER_API_URL_TEST_OVERRIDE: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

/// Test-only URL override. Callers must hold `USER_API_URL_TEST_LOCK` while setting or clearing it.
#[cfg(test)]
pub(crate) fn set_user_api_url_for_test(url: Option<String>) {
    *USER_API_URL_TEST_OVERRIDE.lock().unwrap() = url;
}

fn http_client() -> Result<reqwest::blocking::Client, AuthError> {
    reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
        .build()
        .map_err(|e| AuthError::Network {
            message: format!("build client: {e}"),
        })
}

#[derive(serde::Deserialize)]
struct MemberUsageResponse {
    code: bool,
    #[serde(default)]
    data: Option<MemberUsageView>,
}

pub fn parse_member_usage(text: &str) -> Result<MemberUsageView, AuthError> {
    let response: MemberUsageResponse =
        serde_json::from_str(text).map_err(|_| AuthError::Network {
            message: "会员用量响应无效".into(),
        })?;
    if !response.code {
        return Err(AuthError::Api {
            code: 0,
            message: "会员用量请求失败".into(),
        });
    }
    response.data.ok_or_else(|| AuthError::Network {
        message: "会员用量响应无效".into(),
    })
}

fn member_usage_url(base_url: &str) -> String {
    let base_url = base_url.trim_end_matches('/');
    let provider_origin = base_url.strip_suffix("/v1").unwrap_or(base_url);
    format!("{provider_origin}/api/usage/token")
}

pub fn fetch_member_usage(credential: &VipRuntimeCredential) -> Result<MemberUsageView, AuthError> {
    let url = member_usage_url(&credential.base_url);
    let text = http_client()
        .map_err(|_| AuthError::Network {
            message: "会员用量请求失败".into(),
        })?
        .get(url)
        .bearer_auth(&credential.api_key)
        .send()
        .map_err(|_| AuthError::Network {
            message: "会员用量请求失败".into(),
        })?
        .text()
        .map_err(|_| AuthError::Network {
            message: "会员用量响应读取失败".into(),
        })?;
    parse_member_usage(&text)
}

pub fn parse_member_benefits(text: &str) -> Result<MemberBenefitsView, AuthError> {
    parse_cool_response(text)
}

pub fn fetch_member_benefits(token: &str) -> Result<MemberBenefitsView, AuthError> {
    let url = endpoint("/app/ai/member/benefits");
    let response = http_client()?
        .get(&url)
        .header("Authorization", token)
        .send()
        .map_err(|e| AuthError::Network {
            message: format!("member benefits: {e}"),
        })?;
    if response.status().as_u16() == 401 {
        return Err(AuthError::NotAuthenticated);
    }
    let text = response.text().map_err(|e| AuthError::Network {
        message: format!("member benefits body: {e}"),
    })?;
    parse_member_benefits(&text)
}

fn endpoint(path: &str) -> String {
    format!("{}{}", user_api_url(), path)
}

fn credential_error(message: impl Into<String>) -> AuthError {
    AuthError::Credential {
        message: message.into(),
    }
}

fn log_vip_runtime_event(layout: &Layout, event: &str) {
    crate::sidecar::log_vip_runtime_event(&layout.logs_dir, event);
}

fn new_vip_credential_trace_id() -> String {
    let mut bytes = [0_u8; 12];
    OsRng.fill_bytes(&mut bytes);
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn log_vip_credential_event(layout: &Layout, trace_id: &str, event: &str) {
    log_vip_runtime_event(layout, &format!("trace_id={trace_id} {event}"));
}

/// 用于本地诊断的安全摘要；不得把凭据字段本身写入日志。
fn vip_runtime_log_summary(credential: &VipRuntimeCredential) -> String {
    format!(
        "credential accepted (models_count={}); retained in process memory only",
        credential.models.len()
    )
}

pub fn public_key_base64(private_key: &StaticSecret) -> String {
    let public_key = PublicKey::from(private_key);
    let mut der = X25519_SPKI_PREFIX.to_vec();
    der.extend_from_slice(public_key.as_bytes());
    base64::engine::general_purpose::STANDARD.encode(der)
}

/// 上报本机持久会员公钥。请求体只能包含公钥，私钥始终保留在本机安全目录。
pub fn upload_member_public_key(token: &str, layout: &Layout) -> Result<(), AuthError> {
    let private_key = load_or_create_member_key(layout)?;
    let response = http_client()?
        .post(endpoint("/app/ai/member"))
        .header("Authorization", token)
        .json(&serde_json::json!({
            "clientPublicKey": public_key_base64(&private_key),
        }))
        .send()
        .map_err(|_| AuthError::Network {
            message: MEMBER_PUBLIC_KEY_UPLOAD_FAILED.into(),
        })?
        .error_for_status()
        .map_err(|_| AuthError::Network {
            message: MEMBER_PUBLIC_KEY_UPLOAD_FAILED.into(),
        })?;
    let text = response.text().map_err(|_| AuthError::Network {
        message: MEMBER_PUBLIC_KEY_UPLOAD_FAILED.into(),
    })?;
    parse_cool_response::<()>(&text)
        .map(|_| ())
        .map_err(|_| AuthError::Network {
            message: MEMBER_PUBLIC_KEY_UPLOAD_FAILED.into(),
        })
}

/// 读取已有的会员 X25519 私钥；首次使用时以原子方式生成并保存到本地。
/// 私钥仅用于本机解密会员凭据，绝不能写入日志或发送到服务端。
pub fn load_or_create_member_key(layout: &Layout) -> Result<StaticSecret, AuthError> {
    match fs::metadata(&layout.member_key) {
        Ok(_) => read_member_key(&layout.member_key),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let private_key = StaticSecret::random_from_rng(OsRng);
            write_member_key_atomic(&layout.member_key, &private_key.to_bytes())?;
            // 发布竞争失败时，读取已由其他调用持久化的密钥，保证所有调用返回同一私钥。
            read_member_key(&layout.member_key)
        }
        Err(_) => Err(credential_error("会员私钥读取失败")),
    }
}

fn read_member_key(path: &Path) -> Result<StaticSecret, AuthError> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        let metadata = fs::metadata(path).map_err(|_| credential_error("会员私钥读取失败"))?;
        if metadata.permissions().mode() & 0o777 != 0o600 {
            fs::set_permissions(path, fs::Permissions::from_mode(0o600))
                .map_err(|_| credential_error("会员私钥权限设置失败"))?;
        }
    }

    let bytes = fs::read(path).map_err(|_| credential_error("会员私钥读取失败"))?;
    let bytes: [u8; 32] = bytes
        .try_into()
        .map_err(|_| credential_error("会员私钥长度无效"))?;
    Ok(StaticSecret::from(bytes))
}

fn write_member_key_atomic(path: &Path, key: &[u8; 32]) -> Result<(), AuthError> {
    let parent = path
        .parent()
        .ok_or_else(|| credential_error("会员私钥路径无效"))?;
    fs::create_dir_all(parent).map_err(|_| credential_error("会员私钥目录创建失败"))?;

    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("member.key");
    let mut suffix = [0_u8; 12];
    OsRng.fill_bytes(&mut suffix);
    let suffix: String = suffix.iter().map(|byte| format!("{byte:02x}")).collect();
    let tmp = parent.join(format!(
        ".{file_name}.{}.{}.tmp",
        std::process::id(),
        suffix
    ));

    let result = (|| {
        #[cfg(unix)]
        {
            use std::io::Write;
            use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&tmp)
                .map_err(|_| credential_error("会员私钥临时文件创建失败"))?;
            file.set_permissions(fs::Permissions::from_mode(0o600))
                .map_err(|_| credential_error("会员私钥权限设置失败"))?;
            file.write_all(key)
                .map_err(|_| credential_error("会员私钥写入失败"))?;
            file.sync_all()
                .map_err(|_| credential_error("会员私钥保存失败"))?;
        }
        #[cfg(not(unix))]
        {
            use std::io::Write;

            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&tmp)
                .map_err(|_| credential_error("会员私钥临时文件创建失败"))?;
            file.write_all(key)
                .map_err(|_| credential_error("会员私钥写入失败"))?;
            file.sync_all()
                .map_err(|_| credential_error("会员私钥保存失败"))?;
        }

        match fs::hard_link(&tmp, path) {
            Ok(()) => {
                #[cfg(unix)]
                sync_member_key_parent(parent)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(_) => return Err(credential_error("会员私钥保存失败")),
        }
        Ok(())
    })();

    let cleanup = fs::remove_file(&tmp);
    if result.is_err() || cleanup.is_err() {
        return Err(credential_error("会员私钥保存失败"));
    }

    #[cfg(unix)]
    sync_member_key_parent(parent)?;

    Ok(())
}

#[cfg(unix)]
fn sync_member_key_parent(parent: &Path) -> Result<(), AuthError> {
    fs::File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| credential_error("会员私钥保存失败"))
}

fn decode_server_public_key(value: &str) -> Result<PublicKey, AuthError> {
    let der = decode_member_field(value, "serverPublicKey", X25519_SPKI_PREFIX.len() + 32)?;
    if der.len() != X25519_SPKI_PREFIX.len() + 32 || !der.starts_with(&X25519_SPKI_PREFIX) {
        return Err(credential_error(
            "serverPublicKey 不是 X25519 DER-SPKI 密钥",
        ));
    }
    let bytes: [u8; 32] = der[X25519_SPKI_PREFIX.len()..]
        .try_into()
        .map_err(|_| credential_error("serverPublicKey 长度无效"))?;
    Ok(PublicKey::from(bytes))
}

fn decode_member_field(value: &str, name: &str, max_bytes: usize) -> Result<Vec<u8>, AuthError> {
    // 先按编码上限拒绝，避免畸形服务端响应触发无界内存分配。
    let max_base64_len = max_bytes.div_ceil(3) * 4;
    if value.len() > max_base64_len {
        return Err(credential_error(format!("{name} 长度无效")));
    }
    let decoded = base64::engine::general_purpose::STANDARD
        .decode(value)
        .map_err(|_| credential_error(format!("{name} 不是有效 Base64")))?;
    if decoded.len() > max_bytes
        || base64::engine::general_purpose::STANDARD.encode(&decoded) != value
    {
        return Err(credential_error(format!("{name} 不是有效 Base64")));
    }
    Ok(decoded)
}

fn decode_member_fixed_field(
    value: &str,
    name: &str,
    expected_len: usize,
) -> Result<Vec<u8>, AuthError> {
    let decoded = decode_member_field(value, name, expected_len)?;
    if decoded.len() != expected_len {
        return Err(credential_error("凭据加密字段长度无效"));
    }
    Ok(decoded)
}

fn decrypt_member_ciphertext(
    client_private_key: &StaticSecret,
    peer_public_key: PublicKey,
    salt: &[u8],
    iv: &[u8],
    ciphertext: &mut [u8],
    tag: &[u8],
) -> Result<(), AuthError> {
    if salt.len() != 32 || iv.len() != 12 || tag.len() != 16 || ciphertext.is_empty() {
        return Err(credential_error("凭据加密字段长度无效"));
    }

    let shared_secret = client_private_key.diffie_hellman(&peer_public_key);
    let mut key = [0_u8; 32];
    Hkdf::<Sha256>::new(Some(salt), shared_secret.as_bytes())
        .expand(MEMBER_CREDENTIAL_INFO, &mut key)
        .map_err(|_| credential_error("凭据密钥派生失败"))?;
    let cipher = Aes256Gcm::new_from_slice(&key).map_err(|_| credential_error("凭据密钥无效"))?;
    cipher
        .decrypt_in_place_detached(Nonce::from_slice(iv), b"", ciphertext, Tag::from_slice(tag))
        .map_err(|_| credential_error("凭据认证失败"))
}

fn validate_vip_credential(credential: &VipRuntimeCredential) -> Result<(), AuthError> {
    if credential.base_url.trim().is_empty()
        || credential.api_key.trim().is_empty()
        || credential.models.is_empty()
        || credential
            .models
            .iter()
            .any(|model| model.trim().is_empty())
    {
        return Err(credential_error("凭据内容不完整"));
    }
    Ok(())
}

pub fn decrypt_member_envelope(
    client_private_key: &StaticSecret,
    envelope: &EncryptedMemberEnvelope,
) -> Result<VipRuntimeCredential, AuthError> {
    if envelope.version != 1 {
        return Err(credential_error("不支持的凭据版本"));
    }
    let server_public_key = decode_server_public_key(&envelope.server_public_key)?;
    let salt = decode_member_fixed_field(&envelope.salt, "salt", 32)?;
    let iv = decode_member_fixed_field(&envelope.iv, "iv", 12)?;
    let mut ciphertext = decode_member_field(
        &envelope.ciphertext,
        "ciphertext",
        MAX_MEMBER_CIPHERTEXT_BYTES,
    )?;
    let tag = decode_member_fixed_field(&envelope.tag, "tag", 16)?;
    decrypt_member_ciphertext(
        client_private_key,
        server_public_key,
        &salt,
        &iv,
        &mut ciphertext,
        &tag,
    )?;
    let credential: VipRuntimeCredential =
        serde_json::from_slice(&ciphertext).map_err(|_| credential_error("凭据内容无效"))?;
    validate_vip_credential(&credential)?;
    Ok(credential)
}

fn decrypt_v2_member_credential(
    client_private_key: &StaticSecret,
    response: MemberCipherCredentials,
) -> Result<VipRuntimeCredential, AuthError> {
    if response.version != 2 || response.api_key_seal.version != 2 {
        return Err(credential_error("不支持的凭据版本"));
    }

    let peer_public_key = decode_server_public_key(&response.api_key_seal.ephemeral_public_key)
        .map_err(|_| credential_error("ephemeralPublicKey 无效"))?;
    let salt = decode_member_fixed_field(&response.api_key_seal.salt, "salt", 32)?;
    let iv = decode_member_fixed_field(&response.api_key_seal.iv, "iv", 12)?;
    let mut ciphertext = decode_member_field(
        &response.api_key_seal.ciphertext,
        "ciphertext",
        MAX_MEMBER_CIPHERTEXT_BYTES,
    )?;
    let tag = decode_member_fixed_field(&response.api_key_seal.tag, "tag", 16)?;
    decrypt_member_ciphertext(
        client_private_key,
        peer_public_key,
        &salt,
        &iv,
        &mut ciphertext,
        &tag,
    )?;
    let api_key =
        String::from_utf8(ciphertext).map_err(|_| credential_error("apiKeySeal 内容无效"))?;
    let credential = VipRuntimeCredential {
        base_url: response.base_url,
        api_key,
        models: response.models,
    };
    validate_vip_credential(&credential)?;
    Ok(credential)
}

fn decrypt_member_credential_response(
    client_private_key: &StaticSecret,
    response: serde_json::Value,
) -> Result<VipRuntimeCredential, AuthError> {
    let version = response
        .get("version")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| credential_error("凭据版本无效"))?;
    match version {
        1 => serde_json::from_value::<EncryptedMemberEnvelope>(response)
            .map_err(|_| credential_error("凭据响应无效"))
            .and_then(|envelope| decrypt_member_envelope(client_private_key, &envelope)),
        2 => serde_json::from_value::<MemberCipherCredentials>(response)
            .map_err(|_| credential_error("凭据响应无效"))
            .and_then(|response| decrypt_v2_member_credential(client_private_key, response)),
        _ => Err(credential_error("不支持的凭据版本")),
    }
}

pub fn fetch_vip_credential(
    token: &str,
    layout: &Layout,
) -> Result<VipRuntimeCredential, AuthError> {
    let trace_id = new_vip_credential_trace_id();
    log_vip_credential_event(
        layout,
        &trace_id,
        "requesting encrypted member credential from server",
    );
    // v1 协议沿用此持久私钥完成 ECDH；请求中仅携带由它导出的公钥。
    let member_private_key = load_or_create_member_key(layout)?;
    let response = http_client()?
        .post(endpoint("/app/ai/member/credentials"))
        .header("Authorization", token)
        .header("X-Vibe-Trace-Id", &trace_id)
        .json(&serde_json::json!({
            "clientPublicKey": public_key_base64(&member_private_key),
        }))
        .send()
        .map_err(|e| {
            log_vip_credential_event(
                layout,
                &trace_id,
                "encrypted member credential request failed (network)",
            );
            AuthError::Network {
                message: format!("member credentials: {e}"),
            }
        })?;
    if response
        .content_length()
        .is_some_and(|length| length > MAX_MEMBER_CREDENTIAL_RESPONSE_BYTES as u64)
    {
        return Err(credential_error("会员凭据响应过大"));
    }
    let mut body = response.take(MAX_MEMBER_CREDENTIAL_RESPONSE_BYTES as u64 + 1);
    let mut bytes = Vec::with_capacity(MAX_MEMBER_CREDENTIAL_RESPONSE_BYTES);
    body.read_to_end(&mut bytes).map_err(|e| {
        log_vip_credential_event(
            layout,
            &trace_id,
            "encrypted member credential response could not be read",
        );
        AuthError::Network {
            message: format!("member credentials body: {e}"),
        }
    })?;
    if bytes.len() > MAX_MEMBER_CREDENTIAL_RESPONSE_BYTES {
        return Err(credential_error("会员凭据响应过大"));
    }
    let text = String::from_utf8(bytes).map_err(|_| credential_error("会员凭据响应无效"))?;
    let response: serde_json::Value = parse_cool_response(&text).map_err(|error| {
        log_vip_credential_event(
            layout,
            &trace_id,
            "encrypted member credential response was rejected",
        );
        error
    })?;
    let version = response
        .get("version")
        .and_then(serde_json::Value::as_u64)
        .unwrap_or(0);
    log_vip_credential_event(
        layout,
        &trace_id,
        "encrypted member credential response received",
    );
    log_vip_credential_event(
        layout,
        &trace_id,
        &format!("decrypting encrypted member credential response (version={version})"),
    );
    decrypt_member_credential_response(&member_private_key, response)
        .map(|credential| {
            log_vip_credential_event(
                layout,
                &trace_id,
                &format!("decrypted {}", vip_runtime_log_summary(&credential)),
            );
            credential
        })
        .map_err(|error| {
            log_vip_credential_event(
                layout,
                &trace_id,
                "encrypted member credential decryption failed",
            );
            error
        })
}

pub fn fetch_captcha() -> Result<Captcha, AuthError> {
    let url = endpoint("/app/user/login/captcha?width=120&height=40");
    let text = http_client()?
        .get(&url)
        .send()
        .map_err(|e| AuthError::Network {
            message: format!("captcha: {e}"),
        })?
        .text()
        .map_err(|e| AuthError::Network {
            message: format!("captcha body: {e}"),
        })?;
    parse_cool_response(&text)
}

pub fn send_sms(phone: &str, captcha_id: &str, code: &str) -> Result<String, AuthError> {
    let url = endpoint("/app/user/login/smsCode");
    let body = serde_json::json!({ "phone": phone, "captchaId": captcha_id, "code": code });
    let text = http_client()?
        .post(&url)
        .json(&body)
        .send()
        .map_err(|e| AuthError::Network {
            message: format!("sms: {e}"),
        })?
        .text()
        .map_err(|e| AuthError::Network {
            message: format!("sms body: {e}"),
        })?;
    parse_cool_response_with_message::<serde_json::Value>(&text).map(|response| response.message)
}

/// 把 cool-admin 返回的相对秒 expire 转成绝对 epoch 秒。
pub fn session_from_login(raw: LoginRaw, user_info: Option<UserInfo>) -> UserSession {
    let now = now_secs();
    UserSession {
        token: raw.token,
        refresh_token: raw.refresh_token,
        expire_at: now + raw.expire,
        refresh_expire_at: now + raw.refresh_expire,
        remember_until: None,
        user_info,
        vip: None,
    }
}

/// refresh 后的 token 必须重新获取会员凭据，不能复用旧 token 的内存缓存。
fn session_from_refresh(raw: LoginRaw, previous: &UserSession) -> UserSession {
    let mut refreshed = session_from_login(raw, previous.user_info.clone());
    refreshed.remember_until = previous.remember_until;
    refreshed
}

fn post_login(path: &str, body: serde_json::Value) -> Result<CoolSuccess<LoginRaw>, AuthError> {
    let text = http_client()?
        .post(endpoint(path))
        .json(&body)
        .send()
        .map_err(|e| AuthError::Network {
            message: format!("login: {e}"),
        })?
        .text()
        .map_err(|e| AuthError::Network {
            message: format!("login body: {e}"),
        })?;
    parse_cool_response_with_message(&text)
}

pub fn login_by_phone(phone: &str, sms_code: &str) -> Result<CoolSuccess<LoginRaw>, AuthError> {
    post_login(
        "/app/user/login/phone",
        serde_json::json!({ "phone": phone, "smsCode": sms_code }),
    )
}

pub fn login_by_password(phone: &str, password: &str) -> Result<CoolSuccess<LoginRaw>, AuthError> {
    post_login(
        "/app/user/login/password",
        serde_json::json!({ "phone": phone, "password": password }),
    )
}

pub fn register(
    phone: &str,
    sms_code: &str,
    password: &str,
) -> Result<CoolSuccess<LoginRaw>, AuthError> {
    post_login(
        "/app/user/login/register",
        serde_json::json!({ "phone": phone, "smsCode": sms_code, "password": password }),
    )
}

pub fn refresh_token(rt: &str) -> Result<LoginRaw, AuthError> {
    post_login(
        "/app/user/login/refreshToken",
        serde_json::json!({ "refreshToken": rt }),
    )
    .map(|response| response.data)
    // This endpoint uses Cool Admin's generic COMMFAIL (1001) for invalid or expired
    // refresh tokens. It is not a generic server error in this endpoint's contract.
    .map_err(normalize_refresh_token_error)
}

fn normalize_refresh_token_error(error: AuthError) -> AuthError {
    match error {
        AuthError::Api { code: 1001, .. } => AuthError::LoginExpired,
        error => error,
    }
}

pub fn set_password(token: &str, password: &str) -> Result<(), AuthError> {
    let url = endpoint("/app/user/info/setPassword");
    let body = serde_json::json!({ "password": password });
    let text = http_client()?
        .post(&url)
        .header("Authorization", token)
        .json(&body)
        .send()
        .map_err(|e| AuthError::Network {
            message: format!("set pwd: {e}"),
        })?
        .text()
        .map_err(|e| AuthError::Network {
            message: format!("set pwd body: {e}"),
        })?;
    parse_cool_response::<serde_json::Value>(&text).map(|_| ())
}

pub fn fetch_user_info(token: &str) -> Result<UserInfo, AuthError> {
    let url = endpoint("/app/user/info/person");
    let response = http_client()?
        .get(&url)
        .header("Authorization", token)
        .send()
        .map_err(|e| AuthError::Network {
            message: format!("userinfo: {e}"),
        })?;
    if response.status().as_u16() == 401 {
        return Err(AuthError::NotAuthenticated);
    }
    let text = response.text().map_err(|e| AuthError::Network {
        message: format!("userinfo body: {e}"),
    })?;
    parse_cool_response(&text)
}

/// 从服务端刷新已认证会话的展示资料；用户资料不写入磁盘。
pub fn refresh_user_info<F>(session: &mut UserSession, fetch: F) -> Result<(), AuthError>
where
    F: FnOnce(&str) -> Result<UserInfo, AuthError>,
{
    session.user_info = Some(fetch(&session.token)?);
    Ok(())
}

// ── 会话校验：纯决策 + IO 包装 ──

pub enum SessionAction {
    Valid,
    NeedsRefresh,
    Expired,
}

/// 纯函数：依据当前时间戳决定动作（便于单测，不依赖系统时钟副作用）。
pub fn decide_session_action(now: i64, sess: &UserSession) -> SessionAction {
    if now < sess.expire_at {
        SessionAction::Valid
    } else if now < sess.refresh_expire_at {
        SessionAction::NeedsRefresh
    } else {
        SessionAction::Expired
    }
}

/// 启动服务前调：校验内存 session；过期则尝试 refresh（成功后重写 .env）。
/// refresh 失败或 refreshExpire 已到则返回 LoginExpired。
pub fn ensure_session_valid(state: &AuthState, layout: &Layout) -> Result<UserSession, AuthError> {
    let sess = {
        let mut guard = state.0.lock().unwrap();
        match guard.clone() {
            Some(session) => {
                log_vip_runtime_event(layout, "validating authenticated session from memory");
                session
            }
            None => match read_env_token_section(layout) {
                Some(session) => {
                    log_vip_runtime_event(
                        layout,
                        "restored authenticated session from local token configuration",
                    );
                    *guard = Some(session.clone());
                    session
                }
                None => {
                    log_vip_runtime_event(
                        layout,
                        "no authenticated session available for VIP service",
                    );
                    return Err(AuthError::NotAuthenticated);
                }
            },
        }
    };
    match decide_session_action(now_secs(), &sess) {
        SessionAction::Valid => {
            log_vip_runtime_event(layout, "authenticated session is valid; no refresh needed");
            Ok(sess)
        }
        SessionAction::NeedsRefresh => {
            log_vip_runtime_event(layout, "authenticated session expired; refreshing token");
            let raw = refresh_token(&sess.refresh_token).map_err(|error| {
                log_vip_runtime_event(layout, "session refresh failed");
                error
            })?;
            // refresh 未必返回新 userInfo，沿用旧 session 的 userInfo（若有）
            let mut new_sess = session_from_refresh(raw, &sess);
            // userInfo 缺失则尝试补全（旧 session 恢复时 user_info 为 None 时仍可工作）
            if new_sess.user_info.is_none() {
                if let Ok(info) = fetch_user_info(&new_sess.token) {
                    new_sess.user_info = Some(info);
                }
            }
            let new_sess = commit_refreshed_session(state, layout, &sess, new_sess)?;
            log_vip_runtime_event(
                layout,
                "session refresh succeeded; persisted tokens and cleared cached VIP credential",
            );
            Ok(new_sess)
        }
        SessionAction::Expired => {
            log_vip_runtime_event(
                layout,
                "authenticated session and refresh window are expired",
            );
            Err(AuthError::LoginExpired)
        }
    }
}

fn commit_refreshed_session(
    state: &AuthState,
    layout: &Layout,
    previous: &UserSession,
    refreshed: UserSession,
) -> Result<UserSession, AuthError> {
    let mut guard = state.0.lock().unwrap();
    match guard.as_ref() {
        Some(active) if active.token == previous.token => {
            // 持锁覆盖短暂的原子文件写，避免 logout 在检查后重新写入旧会话。
            write_env_token_section(layout, &refreshed)?;
            *guard = Some(refreshed.clone());
            Ok(refreshed)
        }
        _ => Err(AuthError::NotAuthenticated),
    }
}

fn commit_vip_credential(
    current: &mut Option<UserSession>,
    previous: &UserSession,
    credential: VipRuntimeCredential,
) -> Result<UserSession, AuthError> {
    match current.as_mut() {
        Some(active) if active.token == previous.token => {
            if active.vip.is_none() {
                active.vip = Some(credential);
            }
            Ok(active.clone())
        }
        _ => Err(AuthError::NotAuthenticated),
    }
}

/// Runs provider work without holding the authentication lock, then verifies the
/// expected session remains current before returning its result.
pub fn with_current_vip_credential<T>(
    state: &AuthState,
    session: &UserSession,
    operation: impl FnOnce(&VipRuntimeCredential) -> Result<T, AuthError>,
) -> Result<T, AuthError> {
    let credential = match state.0.lock().unwrap().as_ref() {
        Some(current) if current.token == session.token => current.vip.clone(),
        _ => None,
    }
    .ok_or(AuthError::NotAuthenticated)?;
    let result = operation(&credential);

    if matches!(state.0.lock().unwrap().as_ref(), Some(current) if current.token == session.token) {
        result
    } else {
        Err(AuthError::NotAuthenticated)
    }
}

/// 获取内存中已解密的会员凭据；进程重启后按需重新获取，但绝不写进 .env。
pub fn ensure_vip_credential(state: &AuthState, layout: &Layout) -> Result<UserSession, AuthError> {
    let sess = ensure_session_valid(state, layout)?;
    if let Some(cached) = {
        let guard = state.0.lock().unwrap();
        match guard.as_ref() {
            Some(active) if active.token == sess.token => {
                active.vip.as_ref().map(|_| active.clone())
            }
            _ => return Err(AuthError::NotAuthenticated),
        }
    } {
        log_vip_runtime_event(layout, "using cached VIP runtime credential from memory");
        return Ok(cached);
    }

    log_vip_runtime_event(
        layout,
        "no cached VIP credential; fetching a new runtime credential",
    );
    let credential = fetch_vip_credential(&sess.token, layout)?;
    let sess = commit_vip_credential(&mut state.0.lock().unwrap(), &sess, credential)?;
    log_vip_runtime_event(
        layout,
        "VIP runtime credential cached in memory for this desktop session",
    );
    Ok(sess)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;

    fn mock_member_public_key_upload(response: &'static [u8]) -> (String, mpsc::Receiver<String>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let (request_sender, request_receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            let header_end = loop {
                let bytes_read = stream.read(&mut buffer).unwrap();
                assert!(bytes_read > 0);
                request.extend_from_slice(&buffer[..bytes_read]);
                if let Some(position) = request.windows(4).position(|window| window == b"\r\n\r\n")
                {
                    break position + 4;
                }
            };
            let headers = std::str::from_utf8(&request[..header_end]).unwrap();
            let content_length = headers
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then_some(value.trim())
                })
                .unwrap()
                .parse::<usize>()
                .unwrap();
            while request.len() - header_end < content_length {
                let bytes_read = stream.read(&mut buffer).unwrap();
                assert!(bytes_read > 0);
                request.extend_from_slice(&buffer[..bytes_read]);
            }
            request_sender
                .send(String::from_utf8(request).unwrap())
                .unwrap();
            stream.write_all(response).unwrap();
        });
        (format!("http://{address}"), request_receiver)
    }

    fn mock_vip_credential_fetch(request_count: usize) -> (String, mpsc::Receiver<String>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let (request_sender, request_receiver) = mpsc::channel();
        std::thread::spawn(move || {
            for _ in 0..request_count {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                let mut buffer = [0_u8; 1024];
                let header_end = loop {
                    let bytes_read = stream.read(&mut buffer).unwrap();
                    assert!(bytes_read > 0);
                    request.extend_from_slice(&buffer[..bytes_read]);
                    if let Some(position) =
                        request.windows(4).position(|window| window == b"\r\n\r\n")
                    {
                        break position + 4;
                    }
                };
                let headers = std::str::from_utf8(&request[..header_end]).unwrap();
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then_some(value.trim())
                    })
                    .unwrap()
                    .parse::<usize>()
                    .unwrap();
                while request.len() - header_end < content_length {
                    let bytes_read = stream.read(&mut buffer).unwrap();
                    assert!(bytes_read > 0);
                    request.extend_from_slice(&buffer[..bytes_read]);
                }

                let request = String::from_utf8(request).unwrap();
                let (_, body) = request.split_once("\r\n\r\n").unwrap();
                let body: serde_json::Value = serde_json::from_str(body).unwrap();
                let client_public_key = body["clientPublicKey"].as_str().unwrap();
                let envelope =
                    encrypted_fixture_for(&decode_server_public_key(client_public_key).unwrap());
                let response_body = serde_json::json!({
                    "code": 1000,
                    "data": {
                        "version": envelope.version,
                        "serverPublicKey": envelope.server_public_key,
                        "salt": envelope.salt,
                        "iv": envelope.iv,
                        "ciphertext": envelope.ciphertext,
                        "tag": envelope.tag,
                    }
                })
                .to_string();
                request_sender.send(request).unwrap();
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body,
                )
                .unwrap();
            }
        });
        (format!("http://{address}"), request_receiver)
    }

    #[test]
    fn parses_successful_member_usage_response() {
        let usage = parse_member_usage(
            r#"{"code":true,"data":{"total_available":8,"total_granted":10,"total_used":2,"unlimited_quota":true}}"#,
        )
        .unwrap();
        assert_eq!(
            (usage.total_available, usage.total_granted, usage.total_used),
            (8, 10, 2)
        );
        assert_eq!(
            serde_json::to_value(usage).unwrap()["unlimited_quota"],
            true
        );
    }

    #[test]
    fn parses_member_benefits_response() {
        let view = parse_member_benefits(
            r#"{"code":1000,"data":{"benefits":[{"id":"models","title":"VIP 模型"},{"id":"priority","title":"优先响应","description":"高峰期优先排队"}]}}"#,
        )
        .unwrap();
        assert_eq!(view.benefits.len(), 2);
        assert_eq!(
            view.benefits[1].description.as_deref(),
            Some("高峰期优先排队")
        );
    }

    #[test]
    fn member_usage_url_uses_provider_origin_not_openai_v1_prefix() {
        assert_eq!(
            member_usage_url("https://provider.example/v1"),
            "https://provider.example/api/usage/token"
        );
    }

    #[test]
    fn member_usage_failure_does_not_expose_provider_message() {
        let err = parse_member_usage(
            r#"{"code":false,"message":"https://provider.example/token?api_key=secret"}"#,
        )
        .unwrap_err();

        assert!(matches!(
            err,
            AuthError::Api {
                code: 0,
                message
            } if message == "会员用量请求失败"
        ));
    }

    fn client_keypair() -> (x25519_dalek::StaticSecret, x25519_dalek::PublicKey) {
        let private_key = x25519_dalek::StaticSecret::from([7_u8; 32]);
        let public_key = x25519_dalek::PublicKey::from(&private_key);
        (private_key, public_key)
    }

    #[test]
    fn public_key_upload_sends_only_client_public_key() {
        let _api_url_lock = USER_API_URL_TEST_LOCK.lock().unwrap();
        let (api_url, request_receiver) = mock_member_public_key_upload(
            b"HTTP/1.1 200 OK\r\nContent-Length: 25\r\nConnection: close\r\n\r\n{\"code\":1000,\"data\":null}",
        );
        set_user_api_url_for_test(Some(api_url));

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let result = upload_member_public_key("test-access-token", &layout);

        set_user_api_url_for_test(None);
        result.unwrap();

        let request = request_receiver
            .recv_timeout(std::time::Duration::from_secs(3))
            .unwrap();
        let (headers, body) = request.split_once("\r\n\r\n").unwrap();
        assert!(headers.starts_with("POST /app/ai/member HTTP/1.1\r\n"));
        assert!(headers.lines().any(|line| {
            line.split_once(':').is_some_and(|(name, value)| {
                name.eq_ignore_ascii_case("authorization") && value.trim() == "test-access-token"
            })
        }));
        let body: serde_json::Value = serde_json::from_str(body).unwrap();
        let body = body.as_object().unwrap();
        assert_eq!(body.len(), 1);
        assert!(body.get("clientPublicKey").is_some());
        assert!(body.get("privateKey").is_none());
    }

    #[test]
    fn public_key_upload_uses_the_persisted_key_without_sending_private_key_bytes() {
        use base64::Engine;

        let _api_url_lock = USER_API_URL_TEST_LOCK.lock().unwrap();
        let (api_url, request_receiver) = mock_member_public_key_upload(
            b"HTTP/1.1 200 OK\r\nContent-Length: 25\r\nConnection: close\r\n\r\n{\"code\":1000,\"data\":null}",
        );
        set_user_api_url_for_test(Some(api_url));

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let persisted_private_key = [42_u8; 32];
        let persisted_private_key_base64 =
            base64::engine::general_purpose::STANDARD.encode(persisted_private_key);
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(&layout.member_key, persisted_private_key).unwrap();
        let result = upload_member_public_key("test-access-token", &layout);

        set_user_api_url_for_test(None);
        result.unwrap();

        let request = request_receiver
            .recv_timeout(std::time::Duration::from_secs(3))
            .unwrap();
        assert!(
            !request.contains(&persisted_private_key_base64),
            "完整 HTTP 请求不得包含持久私钥的 Base64 表示"
        );
        let (_, body) = request.split_once("\r\n\r\n").unwrap();
        let body: serde_json::Value = serde_json::from_str(body).unwrap();
        let body = body.as_object().unwrap();
        let client_public_key = body["clientPublicKey"].as_str().unwrap();
        let persisted_private_key = StaticSecret::from(persisted_private_key);

        assert_eq!(body.len(), 1);
        assert_eq!(client_public_key, public_key_base64(&persisted_private_key));
        assert_ne!(client_public_key, persisted_private_key_base64);
    }

    #[test]
    fn public_key_upload_maps_non_success_http_status_to_fixed_error() {
        let _api_url_lock = USER_API_URL_TEST_LOCK.lock().unwrap();
        let (api_url, request_receiver) = mock_member_public_key_upload(
            b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 50\r\nConnection: close\r\n\r\n{\"message\":\"upstream-detail-should-not-propagate\"}",
        );
        set_user_api_url_for_test(Some(api_url));

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let error = upload_member_public_key("test-token", &layout).unwrap_err();

        set_user_api_url_for_test(None);
        request_receiver
            .recv_timeout(std::time::Duration::from_secs(3))
            .unwrap();

        assert!(matches!(error, AuthError::Network { message } if message == "会员公钥上报失败"));
    }

    #[test]
    fn public_key_upload_maps_cool_failure_to_fixed_error() {
        let _api_url_lock = USER_API_URL_TEST_LOCK.lock().unwrap();
        let (api_url, request_receiver) = mock_member_public_key_upload(
            b"HTTP/1.1 200 OK\r\nContent-Length: 62\r\nConnection: close\r\n\r\n{\"code\":1001,\"message\":\"upstream-detail-should-not-propagate\"}",
        );
        set_user_api_url_for_test(Some(api_url));

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let error = upload_member_public_key("test-token", &layout).unwrap_err();

        set_user_api_url_for_test(None);
        request_receiver
            .recv_timeout(std::time::Duration::from_secs(3))
            .unwrap();

        assert!(matches!(error, AuthError::Network { message } if message == "会员公钥上报失败"));
    }

    fn der_spki_base64(public_key: &x25519_dalek::PublicKey) -> String {
        use base64::Engine;

        const X25519_SPKI_PREFIX: [u8; 12] = [
            0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x6e, 0x03, 0x21, 0x00,
        ];
        let mut der = X25519_SPKI_PREFIX.to_vec();
        der.extend_from_slice(public_key.as_bytes());
        base64::engine::general_purpose::STANDARD.encode(der)
    }

    fn encrypted_fixture_for(
        client_public_key: &x25519_dalek::PublicKey,
    ) -> EncryptedMemberEnvelope {
        use aes_gcm::aead::{AeadInPlace, KeyInit};
        use aes_gcm::Aes256Gcm;
        use base64::Engine;
        use hkdf::Hkdf;
        use sha2::Sha256;

        let server_private_key = x25519_dalek::StaticSecret::from([8_u8; 32]);
        let server_public_key = x25519_dalek::PublicKey::from(&server_private_key);
        let shared_secret = server_private_key.diffie_hellman(client_public_key);
        let salt = [9_u8; 32];
        let iv = [10_u8; 12];
        let mut key = [0_u8; 32];
        Hkdf::<Sha256>::new(Some(&salt), shared_secret.as_bytes())
            .expand(b"vibe-trading/member-credential/v1", &mut key)
            .unwrap();
        let mut plaintext =
            br#"{"baseURL":"https://api.example/v1","apiKey":"member-key","models":["model-a"]}"#
                .to_vec();
        let tag = Aes256Gcm::new_from_slice(&key)
            .unwrap()
            .encrypt_in_place_detached((&iv).into(), b"", &mut plaintext)
            .unwrap();
        EncryptedMemberEnvelope {
            version: 1,
            server_public_key: der_spki_base64(&server_public_key),
            salt: base64::engine::general_purpose::STANDARD.encode(salt),
            iv: base64::engine::general_purpose::STANDARD.encode(iv),
            ciphertext: base64::engine::general_purpose::STANDARD.encode(plaintext),
            tag: base64::engine::general_purpose::STANDARD.encode(tag),
        }
    }

    fn encrypted_v2_fixture_for(
        client_public_key: &x25519_dalek::PublicKey,
    ) -> MemberCipherCredentials {
        use aes_gcm::aead::{AeadInPlace, KeyInit};
        use aes_gcm::Aes256Gcm;
        use base64::Engine;
        use hkdf::Hkdf;
        use sha2::Sha256;

        let ephemeral_private_key = x25519_dalek::StaticSecret::from([11_u8; 32]);
        let ephemeral_public_key = x25519_dalek::PublicKey::from(&ephemeral_private_key);
        let shared_secret = ephemeral_private_key.diffie_hellman(client_public_key);
        let salt = [12_u8; 32];
        let iv = [13_u8; 12];
        let mut key = [0_u8; 32];
        Hkdf::<Sha256>::new(Some(&salt), shared_secret.as_bytes())
            .expand(MEMBER_CREDENTIAL_INFO, &mut key)
            .unwrap();
        let mut ciphertext = b"member-key".to_vec();
        let tag = Aes256Gcm::new_from_slice(&key)
            .unwrap()
            .encrypt_in_place_detached((&iv).into(), b"", &mut ciphertext)
            .unwrap();
        MemberCipherCredentials {
            version: 2,
            base_url: "https://api.example/v1".into(),
            models: vec!["model-a".into()],
            api_key_seal: MemberApiKeySeal {
                version: 2,
                ephemeral_public_key: der_spki_base64(&ephemeral_public_key),
                salt: base64::engine::general_purpose::STANDARD.encode(salt),
                iv: base64::engine::general_purpose::STANDARD.encode(iv),
                ciphertext: base64::engine::general_purpose::STANDARD.encode(ciphertext),
                tag: base64::engine::general_purpose::STANDARD.encode(tag),
            },
        }
    }

    #[test]
    fn decrypt_member_envelope_decodes_server_format_fixture() {
        let (client_private_key, client_public_key) = client_keypair();
        let credential = decrypt_member_envelope(
            &client_private_key,
            &encrypted_fixture_for(&client_public_key),
        )
        .unwrap();
        assert_eq!(credential.base_url, "https://api.example/v1");
        assert_eq!(credential.api_key, "member-key");
        assert_eq!(credential.models, vec!["model-a"]);
    }

    #[test]
    fn decrypts_v2_object_member_credentials() {
        let (client_private_key, client_public_key) = client_keypair();
        let response = serde_json::to_value(encrypted_v2_fixture_for(&client_public_key)).unwrap();

        assert!(response["apiKeySeal"].is_object());
        let credential = decrypt_member_credential_response(&client_private_key, response).unwrap();

        assert_eq!(credential.api_key, "member-key");
    }

    #[test]
    fn rejects_string_api_key_seal_and_non_v2_internal_version() {
        let (client_private_key, client_public_key) = client_keypair();
        let response = encrypted_v2_fixture_for(&client_public_key);

        let mut string_seal_response = serde_json::to_value(&response).unwrap();
        string_seal_response["apiKeySeal"] = serde_json::json!("not-an-object");
        assert!(
            decrypt_member_credential_response(&client_private_key, string_seal_response).is_err()
        );

        let mut wrong_version_response = response;
        wrong_version_response.api_key_seal.version = 1;
        assert!(decrypt_member_credential_response(
            &client_private_key,
            serde_json::to_value(wrong_version_response).unwrap(),
        )
        .is_err());
    }

    #[test]
    fn decrypts_v2_member_credentials_and_rejects_tampering() {
        use base64::Engine;

        let (client_private_key, client_public_key) = client_keypair();
        let response = encrypted_v2_fixture_for(&client_public_key);
        let credential = decrypt_member_credential_response(
            &client_private_key,
            serde_json::to_value(&response).unwrap(),
        )
        .unwrap();
        assert_eq!(credential.base_url, "https://api.example/v1");
        assert_eq!(credential.api_key, "member-key");
        assert_eq!(credential.models, vec!["model-a"]);

        let mut malformed_base64 = response.clone();
        malformed_base64.api_key_seal.ciphertext = "%%%".into();
        assert!(matches!(
            decrypt_member_credential_response(
                &client_private_key,
                serde_json::to_value(malformed_base64).unwrap(),
            ),
            Err(AuthError::Credential { message }) if message == "ciphertext 不是有效 Base64"
        ));

        let mut invalid_salt_length = response.clone();
        invalid_salt_length.api_key_seal.salt =
            base64::engine::general_purpose::STANDARD.encode([0_u8; 31]);
        assert!(matches!(
            decrypt_member_credential_response(
                &client_private_key,
                serde_json::to_value(invalid_salt_length).unwrap(),
            ),
            Err(AuthError::Credential { message }) if message == "凭据加密字段长度无效"
        ));

        let mut modified_tag = response.clone();
        let mut tag = base64::engine::general_purpose::STANDARD
            .decode(&modified_tag.api_key_seal.tag)
            .unwrap();
        tag[0] ^= 1;
        modified_tag.api_key_seal.tag = base64::engine::general_purpose::STANDARD.encode(tag);
        assert!(matches!(
            decrypt_member_credential_response(
                &client_private_key,
                serde_json::to_value(modified_tag).unwrap(),
            ),
            Err(AuthError::Credential { message }) if message == "凭据认证失败"
        ));

        let mut modified_ciphertext = response;
        let mut ciphertext = base64::engine::general_purpose::STANDARD
            .decode(&modified_ciphertext.api_key_seal.ciphertext)
            .unwrap();
        ciphertext[0] ^= 1;
        modified_ciphertext.api_key_seal.ciphertext =
            base64::engine::general_purpose::STANDARD.encode(ciphertext);
        assert!(matches!(
            decrypt_member_credential_response(
                &client_private_key,
                serde_json::to_value(modified_ciphertext).unwrap(),
            ),
            Err(AuthError::Credential { message }) if message == "凭据认证失败"
        ));
    }

    #[test]
    fn fetch_vip_credential_reuses_persisted_public_key_and_decrypts_v1_envelopes() {
        let _api_url_lock = USER_API_URL_TEST_LOCK.lock().unwrap();
        let (api_url, request_receiver) = mock_vip_credential_fetch(2);
        set_user_api_url_for_test(Some(api_url));

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let first = fetch_vip_credential("test-access-token", &layout);
        let second = fetch_vip_credential("test-access-token", &layout);

        set_user_api_url_for_test(None);

        let first = first.unwrap();
        let second = second.unwrap();
        assert_eq!(first.base_url, "https://api.example/v1");
        assert_eq!(first.api_key, "member-key");
        assert_eq!(first.models, vec!["model-a"]);
        assert_eq!(second.base_url, first.base_url);
        assert_eq!(second.api_key, first.api_key);
        assert_eq!(second.models, first.models);

        let requests: Vec<String> = (0..2)
            .map(|_| {
                request_receiver
                    .recv_timeout(std::time::Duration::from_secs(3))
                    .unwrap()
            })
            .collect();
        let public_keys: Vec<String> = requests
            .iter()
            .map(|request| {
                let (headers, body) = request.split_once("\r\n\r\n").unwrap();
                assert!(headers.starts_with("POST /app/ai/member/credentials HTTP/1.1\r\n"));
                let body: serde_json::Value = serde_json::from_str(body).unwrap();
                let body = body.as_object().unwrap();
                assert_eq!(body.len(), 1);
                assert!(body.get("privateKey").is_none());
                body["clientPublicKey"].as_str().unwrap().to_owned()
            })
            .collect();
        assert_eq!(public_keys[0], public_keys[1]);
        assert_eq!(
            public_keys[0],
            public_key_base64(&load_or_create_member_key(&layout).unwrap())
        );
    }

    #[test]
    fn fetch_vip_credential_rejects_malformed_persisted_private_key_before_request() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(&layout.member_key, [0_u8; 31]).unwrap();

        assert!(matches!(
            fetch_vip_credential("test-access-token", &layout),
            Err(AuthError::Credential { message }) if message == "会员私钥长度无效"
        ));
    }

    #[test]
    fn user_profile_preserves_the_server_membership_level() {
        let info: UserInfo = parse_cool_response(
            r#"{"code":1000,"data":{"id":7,"nickName":"Trader","gender":0,"status":1,"loginType":2,"memberLevel":{"id":3,"name":"Pro","code":"pro","levelValue":20}}}"#,
        )
        .unwrap();

        let serialized = serde_json::to_value(info).unwrap();
        assert_eq!(serialized["memberLevel"]["name"], "Pro");
        assert_eq!(serialized["memberLevel"]["levelValue"], 20);
    }

    #[test]
    fn vip_runtime_log_summary_never_discloses_credential_values() {
        let credential = VipRuntimeCredential {
            base_url: "https://api.example/v1".into(),
            api_key: "member-key".into(),
            models: vec!["model-a".into(), "model-b".into()],
        };

        let summary = vip_runtime_log_summary(&credential);

        assert!(summary.contains("models_count=2"));
        assert!(!summary.contains(&credential.base_url));
        assert!(!summary.contains(&credential.api_key));
        assert!(!summary.contains("model-a"));
    }

    #[test]
    fn vip_credential_trace_id_is_safe_for_logs_and_request_header() {
        let trace_id = new_vip_credential_trace_id();

        assert_eq!(trace_id.len(), 24);
        assert!(trace_id
            .chars()
            .all(|character| character.is_ascii_hexdigit()));
    }

    #[test]
    fn decrypt_member_envelope_rejects_modified_ciphertext() {
        let (client_private_key, client_public_key) = client_keypair();
        let mut envelope = encrypted_fixture_for(&client_public_key);
        envelope.ciphertext.push('A');
        assert!(decrypt_member_envelope(&client_private_key, &envelope).is_err());
    }

    #[test]
    fn load_or_create_member_key_reuses_32_byte_private_key() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));

        let first = load_or_create_member_key(&layout).unwrap();
        let stored = fs::read(&layout.member_key).unwrap();
        let second = load_or_create_member_key(&layout).unwrap();

        assert_eq!(stored.len(), 32);
        assert_eq!(first.to_bytes(), stored.as_slice());
        assert_eq!(second.to_bytes(), stored.as_slice());
    }

    #[test]
    fn load_or_create_member_key_concurrent_initialization_reuses_persisted_key() {
        use std::sync::Barrier;

        let tmp = tempfile::tempdir().unwrap();
        let layout = Arc::new(Layout::new(&tmp.path().join(".vibe-trading")));
        let barrier = Arc::new(Barrier::new(16));
        let mut handles = Vec::new();

        for _ in 0..16 {
            let layout = Arc::clone(&layout);
            let barrier = Arc::clone(&barrier);
            handles.push(std::thread::spawn(move || {
                barrier.wait();
                load_or_create_member_key(&layout).unwrap().to_bytes()
            }));
        }

        let keys: Vec<[u8; 32]> = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect();
        let stored: [u8; 32] = fs::read(&layout.member_key).unwrap().try_into().unwrap();
        for key in keys {
            assert_eq!(key, stored);
        }
    }

    #[test]
    fn load_or_create_member_key_rejects_non_32_byte_file() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(&layout.member_key, [0_u8; 31]).unwrap();

        assert!(matches!(
            load_or_create_member_key(&layout),
            Err(AuthError::Credential { message }) if message == "会员私钥长度无效"
        ));
    }

    #[cfg(unix)]
    #[test]
    fn load_or_create_member_key_sets_0600_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));

        load_or_create_member_key(&layout).unwrap();

        assert_eq!(
            fs::metadata(&layout.member_key)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }

    #[cfg(unix)]
    #[test]
    fn load_or_create_member_key_tightens_existing_file_permissions_to_0600() {
        use std::os::unix::fs::PermissionsExt;

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(&layout.member_key, [3_u8; 32]).unwrap();
        fs::set_permissions(&layout.member_key, fs::Permissions::from_mode(0o644)).unwrap();

        let key = load_or_create_member_key(&layout).unwrap();

        assert_eq!(key.to_bytes(), [3_u8; 32]);
        assert_eq!(
            fs::metadata(&layout.member_key)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }

    #[test]
    fn token_env_persistence_excludes_vip_credentials() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let session = UserSession {
            token: "token".into(),
            refresh_token: "refresh".into(),
            expire_at: 1_700_000_000,
            refresh_expire_at: 1_700_000_100,
            remember_until: Some(1_800_000_000),
            user_info: None,
            vip: None,
        };
        write_env_token_section(&layout, &session).unwrap();
        let text = fs::read_to_string(&layout.user_env).unwrap();
        assert!(!text.contains("VIP_API_KEY"));
        assert!(!text.contains("VIP_BASE_URL"));
        assert!(!text.contains("member-key"));
        assert!(!text.contains("https://api.example/v1"));
    }

    #[test]
    fn login_and_logout_preserve_legacy_openai_configuration_without_mode() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(
            &layout.user_env,
            "LANGCHAIN_PROVIDER=openai\nLANGCHAIN_MODEL_NAME=model-a\nOPENAI_BASE_URL=https://api.example/v1\nOPENAI_API_KEY=member-key\nUSER_ACCESS_TOKEN=legacy-token\nCUSTOM_KEEP=1\n",
        )
        .unwrap();
        let session = UserSession {
            token: "token".into(),
            refresh_token: "refresh".into(),
            expire_at: 1_700_000_000,
            refresh_expire_at: 1_700_000_100,
            remember_until: Some(1_800_000_000),
            user_info: None,
            vip: None,
        };

        write_env_token_section(&layout, &session).unwrap();
        let after_login = fs::read_to_string(&layout.user_env).unwrap();
        assert!(after_login.contains("member-key"));
        assert!(after_login.contains("https://api.example/v1"));
        assert!(after_login.contains("LANGCHAIN_PROVIDER=openai"));
        assert!(after_login.contains("OPENAI_API_KEY=member-key"));
        assert!(after_login.contains("DESKTOP_LLM_MODE=vip"));
        assert!(after_login.contains("CUSTOM_KEEP=1"));

        clear_env_token_section(&layout).unwrap();
        let after_logout = fs::read_to_string(&layout.user_env).unwrap();
        assert!(after_logout.contains("member-key"));
        assert!(after_logout.contains("OPENAI_API_KEY=member-key"));
        assert!(after_logout.contains("CUSTOM_KEEP=1"));
    }

    #[test]
    fn token_updates_preserve_current_custom_provider_settings() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(
            &layout.user_env,
            "DESKTOP_LLM_MODE=custom\nLANGCHAIN_PROVIDER=openai\nLANGCHAIN_MODEL_NAME=custom-model\nOPENAI_BASE_URL=https://custom.example/v1\nOPENAI_API_KEY=custom-key\n",
        )
        .unwrap();
        let session = UserSession {
            token: "token".into(),
            refresh_token: "refresh".into(),
            expire_at: 1_700_000_000,
            refresh_expire_at: 1_700_000_100,
            remember_until: Some(1_800_000_000),
            user_info: None,
            vip: None,
        };

        write_env_token_section(&layout, &session).unwrap();
        let after_login = fs::read_to_string(&layout.user_env).unwrap();
        assert!(after_login.contains("DESKTOP_LLM_MODE=custom"));
        assert!(after_login.contains("OPENAI_API_KEY=custom-key"));
        clear_env_token_section(&layout).unwrap();
        let text = fs::read_to_string(&layout.user_env).unwrap();
        assert!(text.contains("DESKTOP_LLM_MODE=custom"));
        assert!(text.contains("OPENAI_API_KEY=custom-key"));
        assert!(text.contains("OPENAI_BASE_URL=https://custom.example/v1"));
        assert!(text.contains("LANGCHAIN_MODEL_NAME=custom-model"));
    }

    #[test]
    fn token_write_normalizes_invalid_llm_mode_to_vip() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();
        fs::write(&layout.user_env, "DESKTOP_LLM_MODE=unsupported\n").unwrap();
        let session = sample_session(1_700_000_000, 1_700_000_100);

        write_env_token_section(&layout, &session).unwrap();

        assert!(fs::read_to_string(&layout.user_env)
            .unwrap()
            .contains("DESKTOP_LLM_MODE=vip"));
    }

    #[test]
    fn read_llm_mode_allows_only_custom_or_vip() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        fs::create_dir_all(&layout.root).unwrap();

        fs::write(&layout.user_env, "DESKTOP_LLM_MODE=custom\n").unwrap();
        assert_eq!(read_llm_mode(&layout), DesktopLlmMode::Custom);

        fs::write(&layout.user_env, "DESKTOP_LLM_MODE=anything-else\n").unwrap();
        assert_eq!(read_llm_mode(&layout), DesktopLlmMode::Vip);
    }

    #[test]
    fn refreshed_session_drops_cached_vip_credential() {
        let previous = UserSession {
            token: "old-token".into(),
            refresh_token: "old-refresh".into(),
            expire_at: 0,
            refresh_expire_at: 1,
            remember_until: Some(99),
            user_info: Some(UserInfo {
                id: 1,
                unionid: None,
                avatar_url: None,
                nick_name: None,
                phone: None,
                gender: 0,
                status: 1,
                login_type: 0,
                description: None,
                member_level: None,
            }),
            vip: Some(VipRuntimeCredential {
                base_url: "https://old.example/v1".into(),
                api_key: "old-key".into(),
                models: vec!["old-model".into()],
            }),
        };
        let refreshed = session_from_refresh(
            LoginRaw {
                token: "new-token".into(),
                refresh_token: "new-refresh".into(),
                expire: 60,
                refresh_expire: 120,
                has_password: true,
            },
            &previous,
        );

        assert_eq!(refreshed.token, "new-token");
        assert_eq!(refreshed.user_info.as_ref().unwrap().id, 1);
        assert!(refreshed.vip.is_none());
    }

    #[test]
    fn login_payload_ignores_legacy_member_field_and_persists_only_tokens() {
        let raw: LoginRaw = parse_cool_response(r#"{"code":1000,"data":{"token":"t","refreshToken":"r","expire":1,"refreshExpire":2,"hasPassword":true,"member":{"levelCode":"normal","provider":{"baseURL":"https://api.example/v1","apiKey":"member-key"},"models":["model-a"]}}}"#).unwrap();
        assert_eq!(raw.token, "t");
        assert!(raw.has_password);

        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let mut sess = session_from_login(raw, None);
        sess.remember_until = Some(1_800_000_000);
        write_env_token_section(&layout, &sess).unwrap();
        let text = fs::read_to_string(&layout.user_env).unwrap();
        assert!(text.contains("USER_ACCESS_TOKEN=t"));
        assert!(text.contains("DESKTOP_LLM_MODE=vip"));
        assert!(!text.contains("member-key"));
        assert!(!text.contains("https://api.example/v1"));
    }

    #[test]
    fn rewrite_replaces_existing_key() {
        let content = "OPENAI_API_KEY=old\nOTHER=keep\n";
        let out = rewrite_env_keys(content, &[("OPENAI_API_KEY".into(), "new".into())]);
        assert!(out.contains("OPENAI_API_KEY=new"));
        assert!(out.contains("OTHER=keep"));
        assert!(!out.contains("=old"));
    }

    #[test]
    fn rewrite_appends_missing_key_with_separator() {
        let content = "EXISTING=1";
        let out = rewrite_env_keys(content, &[("OPENAI_API_KEY".into(), "tok".into())]);
        // 原行保留，新行在空行之后追加
        assert!(out.starts_with("EXISTING=1\n\nOPENAI_API_KEY=tok"));
    }

    #[test]
    fn rewrite_preserves_unrelated_lines_and_comments() {
        let content = "# comment\nLANGCHAIN_PROVIDER=openrouter\nOPENROUTER_API_KEY=xxx\n";
        let updates = vec![
            ("LANGCHAIN_PROVIDER".into(), "openai".into()),
            ("OPENAI_API_KEY".into(), "tok".into()),
        ];
        let out = rewrite_env_keys(content, &updates);
        assert!(out.contains("# comment"));
        assert!(out.contains("OPENROUTER_API_KEY=xxx")); // 其他 provider 不破坏
        assert!(out.contains("LANGCHAIN_PROVIDER=openai"));
        assert!(out.contains("OPENAI_API_KEY=tok"));
    }

    #[test]
    fn rewrite_does_not_match_prefixed_key_names() {
        // OPENAI_API_KEY 不应误匹配 OPENAI_API_KEY_EXTRA=
        let content = "OPENAI_API_KEY_EXTRA=x\n";
        let out = rewrite_env_keys(content, &[("OPENAI_API_KEY".into(), "tok".into())]);
        assert!(out.contains("OPENAI_API_KEY_EXTRA=x")); // 原行保留
        assert!(out.contains("OPENAI_API_KEY=tok")); // 新行追加
    }

    #[test]
    fn parse_env_to_map_skips_comments_and_blanks() {
        let content = "# header\n\nA=1\n  B = 2 \nC=\n";
        let map = parse_env_to_map(content);
        assert_eq!(map.get("A").map(String::as_str), Some("1"));
        assert_eq!(map.get("B").map(String::as_str), Some("2"));
        assert_eq!(map.get("C").map(String::as_str), Some(""));
        assert!(!map.contains_key("# header"));
    }

    #[test]
    fn user_api_url_defaults_to_local_server() {
        assert_eq!(
            user_api_url_for_config(Path::new("/missing/desktop-config.json")),
            DEFAULT_USER_API_URL
        );
    }

    #[test]
    fn user_api_url_uses_the_persisted_json_configuration() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://configured.example.test"}"#,
        )
        .unwrap();

        assert_eq!(
            user_api_url_for_config(&path),
            "https://configured.example.test"
        );
    }

    #[test]
    fn write_env_atomic_roundtrips_content() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(".env");
        write_env_atomic(&path, "A=1\nB=2\n").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "A=1\nB=2\n");
    }

    #[test]
    fn write_env_atomic_creates_parent_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("nested/deep/.env");
        write_env_atomic(&path, "X=1\n").unwrap();
        assert!(path.exists());
    }

    #[cfg(unix)]
    #[test]
    fn write_env_atomic_forces_0600_when_temp_file_already_exists() {
        use std::os::unix::fs::PermissionsExt;

        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join(".env");
        let stale_tmp = tmp.path().join("..env.tmp");
        fs::write(&stale_tmp, "stale").unwrap();
        fs::set_permissions(&stale_tmp, fs::Permissions::from_mode(0o644)).unwrap();

        write_env_atomic(&path, "SECRET=value\n").unwrap();

        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    #[test]
    fn write_token_section_preserves_other_keys() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        fs::create_dir_all(&home).unwrap();
        let layout = Layout::new(&home);
        // 预置其他 provider 配置
        fs::write(
            &layout.user_env,
            "OPENROUTER_API_KEY=xxx\nTUSHARE_TOKEN=t\n",
        )
        .unwrap();

        let sess = UserSession {
            token: "tok".into(),
            refresh_token: "rt".into(),
            expire_at: 1700000000,
            refresh_expire_at: 1700000100,
            remember_until: Some(1800000000),
            user_info: None,
            vip: None,
        };
        write_env_token_section(&layout, &sess).unwrap();

        let after = fs::read_to_string(&layout.user_env).unwrap();
        assert!(
            after.contains("OPENROUTER_API_KEY=xxx"),
            "其他 provider 须保留"
        );
        assert!(after.contains("TUSHARE_TOKEN=t"));
        assert!(!after.contains("member-key"));
        assert!(after.contains("DESKTOP_LLM_MODE=vip"));
        assert!(after.contains("USER_REFRESH_TOKEN=rt"));
        assert!(after.contains("USER_TOKEN_EXPIRE=1700000000"));
    }

    #[test]
    fn clear_token_section_wipes_login_keys_but_keeps_others() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        fs::create_dir_all(&home).unwrap();
        let layout = Layout::new(&home);
        fs::write(
            &layout.user_env,
            "OPENROUTER_API_KEY=xxx\nTUSHARE_TOKEN=t\n",
        )
        .unwrap();
        let sess = UserSession {
            token: "tok".into(),
            refresh_token: "rt".into(),
            expire_at: 1700000000,
            refresh_expire_at: 1700000100,
            remember_until: Some(1800000000),
            user_info: None,
            vip: None,
        };
        write_env_token_section(&layout, &sess).unwrap();
        clear_env_token_section(&layout).unwrap();

        let after = fs::read_to_string(&layout.user_env).unwrap();
        assert!(!after.contains("tok"), "token 不得残留");
        assert!(!after.contains("USER_REFRESH_TOKEN=rt"));
        assert!(
            after.contains("OPENROUTER_API_KEY=xxx"),
            "其他 provider 须保留"
        );
        assert!(after.contains("TUSHARE_TOKEN=t"));
        assert!(
            read_env_token_section(&layout).is_none(),
            "清理后读不到 session"
        );
    }

    #[test]
    fn read_token_section_roundtrips_after_write() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        fs::create_dir_all(&home).unwrap();
        let layout = Layout::new(&home);
        let sess = UserSession {
            token: "tok".into(),
            refresh_token: "rt".into(),
            expire_at: 1700000000,
            refresh_expire_at: 1700000100,
            remember_until: Some(1800000000),
            user_info: None,
            vip: None,
        };
        write_env_token_section(&layout, &sess).unwrap();

        let got = read_env_token_section(&layout).expect("应读到 session");
        assert_eq!(got.token, "tok");
        assert!(got.vip.is_none(), "恢复时不能带 VIP 凭据");
        assert_eq!(got.refresh_token, "rt");
        assert_eq!(got.expire_at, 1700000000);
        assert_eq!(got.refresh_expire_at, 1700000100);
        assert!(got.user_info.is_none(), "恢复时不带 userInfo");
    }

    #[test]
    fn read_token_section_returns_none_when_no_token() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join(".vibe-trading");
        fs::create_dir_all(&home).unwrap();
        let layout = Layout::new(&home);
        // 无任何 LLM key
        fs::write(&layout.user_env, "OTHER=1\n").unwrap();
        assert!(read_env_token_section(&layout).is_none());
    }

    #[test]
    fn read_token_section_returns_none_when_file_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        assert!(read_env_token_section(&layout).is_none());
    }

    #[test]
    fn parse_cool_response_unwraps_data_on_success() {
        let text = r#"{"code":1000,"data":{"captchaId":"c1","data":"svg"}}"#;
        let c: Captcha = parse_cool_response(text).unwrap();
        assert_eq!(c.captcha_id, "c1");
        assert_eq!(c.data, "svg");
    }

    #[test]
    fn parse_cool_response_accepts_success_without_data() {
        let text = r#"{"code":1000,"message":"验证码已发送"}"#;
        let response = parse_cool_response_with_message::<()>(&text).unwrap();
        assert_eq!(response.message, "验证码已发送");
    }

    #[test]
    fn parse_cool_response_maps_non_1000_to_api_error() {
        let text = r#"{"code":1001,"message":"验证码错误"}"#;
        let err = parse_cool_response::<Captcha>(text).unwrap_err();
        match err {
            AuthError::Api { code, message } => {
                assert_eq!(code, 1001);
                assert_eq!(message, "验证码错误");
            }
            other => panic!("期望 Api，得到 {other:?}"),
        }
    }

    #[test]
    fn refresh_token_commfail_is_login_expired() {
        assert!(matches!(
            normalize_refresh_token_error(AuthError::Api {
                code: 1001,
                message: "刷新token失败，请检查refreshToken是否正确或过期".into(),
            }),
            AuthError::LoginExpired
        ));
    }

    #[test]
    fn parse_cool_response_maps_bad_json_to_network_error() {
        let err = parse_cool_response::<Captcha>("not json").unwrap_err();
        assert!(matches!(err, AuthError::Network { .. }));
    }

    // ── decide_session_action + ensure_session_valid 测试 ──

    fn sample_session(expire_at: i64, refresh_expire_at: i64) -> UserSession {
        UserSession {
            token: "t".into(),
            refresh_token: "r".into(),
            expire_at,
            refresh_expire_at,
            remember_until: Some(2_000_000_000),
            user_info: None,
            vip: None,
        }
    }

    fn remembered_session(remember_until: i64) -> (Layout, UserSession) {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.keep().join(".vibe-trading"));
        let session = UserSession {
            remember_until: Some(remember_until),
            ..sample_session(20, 40)
        };
        (layout, session)
    }

    fn sample_login_raw() -> LoginRaw {
        LoginRaw {
            token: "new-token".into(),
            refresh_token: "new-refresh".into(),
            expire: 60,
            refresh_expire: 120,
            has_password: true,
        }
    }

    #[test]
    fn remembered_token_section_roundtrips_before_deadline() {
        let (layout, session) = remembered_session(1_700_000_000);
        write_env_token_section(&layout, &session).unwrap();
        assert_eq!(
            read_env_token_section_at(&layout, 1_699_999_999)
                .unwrap()
                .remember_until,
            Some(1_700_000_000)
        );
    }

    #[test]
    fn expired_remembered_token_section_is_cleared() {
        let (layout, session) = remembered_session(1_700_000_000);
        write_env_token_section(&layout, &session).unwrap();
        assert!(read_env_token_section_at(&layout, 1_700_000_000).is_none());
        assert!(!fs::read_to_string(&layout.user_env)
            .unwrap()
            .contains("USER_ACCESS_TOKEN=tok"));
    }

    #[test]
    fn refresh_keeps_original_remember_deadline() {
        let previous = UserSession {
            remember_until: Some(99),
            ..sample_session(20, 40)
        };
        assert_eq!(
            session_from_refresh(sample_login_raw(), &previous).remember_until,
            Some(99)
        );
    }

    #[test]
    fn refresh_result_is_not_committed_after_logout() {
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let original = UserSession {
            remember_until: Some(1_900_000_000),
            ..sample_session(0, 1_800_000_000)
        };
        write_env_token_section(&layout, &original).unwrap();
        let state = AuthState(Arc::new(Mutex::new(Some(original.clone()))));

        clear_env_token_section(&layout).unwrap();
        *state.0.lock().unwrap() = None;

        let refreshed = session_from_refresh(sample_login_raw(), &original);
        let err = commit_refreshed_session(&state, &layout, &original, refreshed).unwrap_err();
        assert!(matches!(err, AuthError::NotAuthenticated));
        assert!(read_env_token_section(&layout).is_none());
    }

    #[test]
    fn vip_credential_is_not_committed_after_logout() {
        let original = sample_session(1_800_000_000, 1_900_000_000);
        let state = AuthState(Arc::new(Mutex::new(None)));
        let credential = VipRuntimeCredential {
            base_url: "https://provider.example".into(),
            api_key: "secret".into(),
            models: vec![],
        };

        let err =
            commit_vip_credential(&mut state.0.lock().unwrap(), &original, credential).unwrap_err();
        assert!(matches!(err, AuthError::NotAuthenticated));
        assert!(state.0.lock().unwrap().is_none());
    }

    #[test]
    fn vip_credential_cannot_be_used_after_logout() {
        let mut session = sample_session(1_800_000_000, 1_900_000_000);
        session.vip = Some(VipRuntimeCredential {
            base_url: "https://provider.example".into(),
            api_key: "secret".into(),
            models: vec![],
        });
        let state = AuthState(Arc::new(Mutex::new(None)));

        let err = with_current_vip_credential(&state, &session, |_| -> Result<(), AuthError> {
            panic!("logout 后不得发起 provider 请求")
        })
        .unwrap_err();

        assert!(matches!(err, AuthError::NotAuthenticated));
    }

    #[test]
    fn vip_provider_work_does_not_hold_authentication_lock() {
        let mut session = sample_session(1_800_000_000, 1_900_000_000);
        session.vip = Some(VipRuntimeCredential {
            base_url: "https://provider.example".into(),
            api_key: "secret".into(),
            models: vec![],
        });
        let state = AuthState(Arc::new(Mutex::new(Some(session.clone()))));
        let state_during_provider_work = state.clone();

        let result = with_current_vip_credential(&state, &session, move |_| {
            assert!(state_during_provider_work.0.try_lock().is_ok());
            Ok(())
        });

        assert!(result.is_ok());
    }

    #[test]
    fn refreshes_user_info_from_the_authenticated_server_profile() {
        let mut session = sample_session(1_800_000_000, 1_900_000_000);
        session.user_info = Some(UserInfo {
            id: 7,
            unionid: None,
            avatar_url: None,
            nick_name: Some("Stale name".into()),
            phone: None,
            gender: 0,
            status: 1,
            login_type: 2,
            description: None,
            member_level: None,
        });

        refresh_user_info(&mut session, |token| {
            assert_eq!(token, "t");
            Ok(UserInfo {
                id: 7,
                unionid: None,
                avatar_url: None,
                nick_name: Some("Trader".into()),
                phone: Some("13800000000".into()),
                gender: 0,
                status: 1,
                login_type: 2,
                description: None,
                member_level: None,
            })
        })
        .unwrap();

        assert_eq!(
            session.user_info.unwrap().nick_name.as_deref(),
            Some("Trader")
        );
    }

    #[test]
    fn decide_action_valid_before_expire() {
        let sess = sample_session(1000, 2000);
        assert!(matches!(
            decide_session_action(999, &sess),
            SessionAction::Valid
        ));
        assert!(matches!(
            decide_session_action(0, &sess),
            SessionAction::Valid
        ));
    }

    #[test]
    fn decide_action_needs_refresh_between_expire_and_refresh_expire() {
        let sess = sample_session(1000, 2000);
        assert!(matches!(
            decide_session_action(1000, &sess),
            SessionAction::NeedsRefresh
        ));
        assert!(matches!(
            decide_session_action(1999, &sess),
            SessionAction::NeedsRefresh
        ));
    }

    #[test]
    fn decide_action_expired_after_refresh_expire() {
        let sess = sample_session(1000, 2000);
        assert!(matches!(
            decide_session_action(2000, &sess),
            SessionAction::Expired
        ));
        assert!(matches!(
            decide_session_action(5000, &sess),
            SessionAction::Expired
        ));
    }

    #[test]
    fn ensure_session_valid_returns_not_authenticated_when_empty() {
        use std::sync::{Arc, Mutex};
        let state = AuthState(Arc::new(Mutex::new(None)));
        let tmp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&tmp.path().join(".vibe-trading"));
        let err = ensure_session_valid(&state, &layout).unwrap_err();
        assert!(matches!(err, AuthError::NotAuthenticated));
    }
}
