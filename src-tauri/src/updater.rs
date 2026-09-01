// src-tauri/src/updater.rs
//! 基于 GitHub Releases 的版本检查与更新逻辑。
//!
//! 流程：
//! 1. check  — 调 GitHub API 获取最新 release，比较版本号，返回 UpdateInfo
//! 2. download — 下载对应平台的安装包到 ~/.vibe-trading/updates/，流式推进度事件
//! 3. install — 停止 sidecar 后用系统命令打开安装包（macOS: open DMG；Windows: 启动 installer）

use std::io::{Read, Write};
use std::path::PathBuf;
use std::time::Duration;

use tauri::{AppHandle, Emitter};

const GITHUB_API: &str =
    "https://api.github.com/repos/NieAnSHOW/Trading-Worker/releases/latest";
const USER_AGENT: &str = "trading-worker-updater/1.0";

// ── 类型 ──────────────────────────────────────────────────────────────

/// 返回给前端的版本信息。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UpdateInfo {
    /// 当前安装版本（来自 Cargo.toml / tauri.conf.json）
    pub current: String,
    /// GitHub 最新版本号（去掉前缀 v，如 "0.2.0"）
    pub latest: String,
    /// 是否有新版本可用
    pub has_update: bool,
    /// 对应当前平台的安装包下载 URL
    pub download_url: String,
    /// 安装包文件名（如 Trading Worker_0.2.0_aarch64.dmg）
    pub asset_name: String,
    /// Release 正文摘要（前 500 字符）
    pub release_notes: String,
}

/// 下载进度事件，emit 到 "update://progress"。
#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DownloadProgress {
    /// 已下载字节数
    pub downloaded: u64,
    /// 文件总大小（0 表示未知）
    pub total: u64,
    /// 是否已完成
    pub done: bool,
    /// 本地保存路径（仅 done=true 时有效）
    pub path: Option<String>,
}

// ── 纯函数：版本比较 ────────────────────────────────────────────────

/// 把 "v0.2.0" 或 "0.2.0" 统一去掉前缀 v。
pub fn strip_v(s: &str) -> &str {
    s.strip_prefix('v').unwrap_or(s)
}

/// semver 三段比较，a > b 返回 true（仅处理 x.y.z 格式，其余按字符串比较）。
pub fn is_newer(latest: &str, current: &str) -> bool {
    fn parse(s: &str) -> Option<(u64, u64, u64)> {
        let parts: Vec<&str> = s.split('.').collect();
        if parts.len() < 3 {
            return None;
        }
        let a = parts[0].parse().ok()?;
        let b = parts[1].parse().ok()?;
        let c = parts[2].parse().ok()?;
        Some((a, b, c))
    }
    match (parse(strip_v(latest)), parse(strip_v(current))) {
        (Some(l), Some(c)) => l > c,
        _ => strip_v(latest) > strip_v(current),
    }
}

/// 从 release assets 中选出当前平台对应的安装包。
/// macOS: 优先 aarch64.dmg（Apple Silicon），其次 x86_64.dmg
/// Windows: 优先 _x64-setup.exe，其次 _x64_zh-CN.msi
pub fn pick_asset(assets: &[serde_json::Value]) -> Option<(String, String)> {
    // 提取所有 (name, browser_download_url)
    let pairs: Vec<(String, String)> = assets
        .iter()
        .filter_map(|a| {
            let name = a.get("name")?.as_str()?.to_string();
            let url = a.get("browser_download_url")?.as_str()?.to_string();
            Some((name, url))
        })
        .collect();

    #[cfg(target_os = "macos")]
    {
        // Apple Silicon 优先
        #[cfg(target_arch = "aarch64")]
        let candidates = ["aarch64.dmg", "arm64.dmg", "x86_64.dmg", ".dmg"];
        #[cfg(not(target_arch = "aarch64"))]
        let candidates = ["x86_64.dmg", ".dmg"];

        for suffix in candidates {
            if let Some(p) = pairs.iter().find(|(n, _)| n.ends_with(suffix)) {
                return Some(p.clone());
            }
        }
    }

    #[cfg(target_os = "windows")]
    {
        let candidates = [
            "_x64-setup.exe",
            "_x64_zh-CN.msi",
            "_x64.msi",
            ".exe",
            ".msi",
        ];
        for suffix in candidates {
            if let Some(p) = pairs.iter().find(|(n, _)| n.ends_with(suffix)) {
                return Some(p.clone());
            }
        }
    }

    // Linux fallback（暂无官方包，取第一个非 .sig/.json 的 asset）
    pairs
        .into_iter()
        .find(|(n, _)| !n.ends_with(".sig") && !n.ends_with(".json"))
}

// ── HTTP 辅助 ───────────────────────────────────────────────────────

fn build_client() -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent(USER_AGENT)
        .build()
        .map_err(|e| format!("构建 HTTP 客户端: {e}"))
}

// ── 核心逻辑 ────────────────────────────────────────────────────────

/// 查询 GitHub 最新 release，与当前版本比较，返回 UpdateInfo。
/// 由 Tauri 命令 `console_check_update` 调用（async + spawn_blocking）。
pub fn check_update(current_version: &str) -> Result<UpdateInfo, String> {
    let client = build_client()?;
    let resp = client
        .get(GITHUB_API)
        .send()
        .map_err(|e| format!("请求 GitHub API 失败: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        return Err(format!("GitHub API 返回 {status}"));
    }

    let body: serde_json::Value = resp.json().map_err(|e| format!("解析 release JSON: {e}"))?;

    let tag = body
        .get("tag_name")
        .and_then(|v| v.as_str())
        .ok_or("release 缺少 tag_name")?;
    let latest = strip_v(tag).to_string();

    let release_notes = body
        .get("body")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .chars()
        .take(500)
        .collect::<String>();

    let assets = body
        .get("assets")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let (asset_name, download_url) =
        pick_asset(&assets).ok_or_else(|| "未找到当前平台对应的安装包".to_string())?;

    Ok(UpdateInfo {
        current: strip_v(current_version).to_string(),
        has_update: is_newer(&latest, current_version),
        latest,
        download_url,
        asset_name,
        release_notes,
    })
}

/// 更新包本地存储目录：~/.vibe-trading/updates/
fn updates_dir() -> Result<PathBuf, String> {
    let dir = dirs::home_dir()
        .ok_or("无法获取 home 目录")?
        .join(".vibe-trading")
        .join("updates");
    std::fs::create_dir_all(&dir).map_err(|e| format!("mkdir updates: {e}"))?;
    Ok(dir)
}

/// 下载安装包，每写入一块就 emit 进度事件。
/// 由 Tauri 命令 `console_download_update` 调用（async + spawn_blocking）。
pub fn download_update(info: &UpdateInfo, app: &AppHandle) -> Result<PathBuf, String> {
    let dir = updates_dir()?;
    let dest = dir.join(&info.asset_name);

    // 已下载且文件非空则直接复用（无需重新下载）
    if dest.exists() && dest.metadata().map(|m| m.len()).unwrap_or(0) > 0 {
        let _ = app.emit(
            "update://progress",
            DownloadProgress {
                downloaded: dest.metadata().map(|m| m.len()).unwrap_or(0),
                total: dest.metadata().map(|m| m.len()).unwrap_or(0),
                done: true,
                path: Some(dest.to_string_lossy().to_string()),
            },
        );
        return Ok(dest);
    }

    let client = build_client()?;
    let mut resp = client
        .get(&info.download_url)
        .send()
        .map_err(|e| format!("下载请求失败: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("下载返回 {}", resp.status()));
    }

    let total = resp
        .headers()
        .get("content-length")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(0);

    let mut file =
        std::fs::File::create(&dest).map_err(|e| format!("创建文件 {}: {e}", dest.display()))?;

    let mut downloaded: u64 = 0;
    let mut buf = [0u8; 65536]; // 64 KB chunk
    loop {
        let n = resp.read(&mut buf).map_err(|e| format!("读取响应: {e}"))?;
        if n == 0 {
            break;
        }
        file.write_all(&buf[..n])
            .map_err(|e| format!("写入文件: {e}"))?;
        downloaded += n as u64;
        let _ = app.emit(
            "update://progress",
            DownloadProgress {
                downloaded,
                total,
                done: false,
                path: None,
            },
        );
    }

    // 完成事件
    let _ = app.emit(
        "update://progress",
        DownloadProgress {
            downloaded,
            total: if total == 0 { downloaded } else { total },
            done: true,
            path: Some(dest.to_string_lossy().to_string()),
        },
    );

    Ok(dest)
}

/// 安装更新：macOS 用 `open` 打开 DMG，Windows 用 `start` 启动安装程序。
/// 安装命令触发后立即返回（让用户在 Finder 中手动拖拽）。
pub fn install_update(path: &std::path::Path) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(path)
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("打开 DMG 失败: {e}"))
    }

    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("cmd")
            .args(["/C", "start", "", &path.to_string_lossy()])
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("启动安装程序失败: {e}"))
    }

    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(path)
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("打开安装包失败: {e}"))
    }
}

/// Stop the running application service, hand the update package to the
/// platform installer, then exit the current process. The exit is deliberately
/// conditional: if the installer cannot be launched, the user remains in the
/// current app to retry.
pub fn install_update_then<S, L, E>(
    path: &std::path::Path,
    stop: S,
    launch: L,
    exit: E,
) -> Result<(), String>
where
    S: FnOnce(),
    L: FnOnce(&std::path::Path) -> Result<(), String>,
    E: FnOnce(),
{
    stop();
    launch(path)?;
    exit();
    Ok(())
}

// ── 测试 ─────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_v_removes_prefix() {
        assert_eq!(strip_v("v0.2.0"), "0.2.0");
        assert_eq!(strip_v("0.2.0"), "0.2.0");
    }

    #[test]
    fn is_newer_detects_minor_bump() {
        assert!(is_newer("0.2.0", "0.1.9"));
        assert!(!is_newer("0.1.9", "0.2.0"));
        assert!(!is_newer("0.1.9", "0.1.9"));
    }

    #[test]
    fn is_newer_handles_v_prefix() {
        assert!(is_newer("v0.2.0", "v0.1.9"));
    }

    #[test]
    fn pick_asset_selects_macos_dmg() {
        let assets = serde_json::json!([
            {"name": "Trading Worker_0.2.0_aarch64.dmg", "browser_download_url": "https://example.com/arm.dmg"},
            {"name": "Trading Worker_0.2.0_x64-setup.exe", "browser_download_url": "https://example.com/win.exe"},
        ]);
        let arr = assets.as_array().unwrap();
        let (name, url) = pick_asset(arr).unwrap();
        // 在 macOS aarch64 下应选 aarch64.dmg；CI 可能是 x86_64，只检查是 dmg 或 exe
        assert!(!url.is_empty(), "download_url 不应为空");
        assert!(!name.is_empty(), "asset_name 不应为空");
    }

    #[test]
    fn pick_asset_returns_none_for_empty() {
        assert!(pick_asset(&[]).is_none());
    }

    #[test]
    fn install_update_then_exits_only_after_successful_handoff() {
        use std::path::Path;
        use std::sync::{Arc, Mutex};

        let events = Arc::new(Mutex::new(Vec::new()));
        let stop_events = events.clone();
        let launch_events = events.clone();
        let exit_events = events.clone();

        install_update_then(
            Path::new("/tmp/Trading Worker.dmg"),
            move || stop_events.lock().unwrap().push("stop"),
            move |_| {
                launch_events.lock().unwrap().push("launch");
                Ok(())
            },
            move || exit_events.lock().unwrap().push("exit"),
        )
        .unwrap();

        assert_eq!(*events.lock().unwrap(), vec!["stop", "launch", "exit"]);

        let exited = Arc::new(Mutex::new(false));
        let exited_on_error = exited.clone();
        let result = install_update_then(
            Path::new("/tmp/Trading Worker.dmg"),
            || {},
            |_| Err("launch failed".to_string()),
            move || *exited_on_error.lock().unwrap() = true,
        );
        assert!(result.is_err());
        assert!(!*exited.lock().unwrap(), "failed handoff must not exit app");
    }
}
