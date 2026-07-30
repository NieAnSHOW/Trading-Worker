// src-tauri/src/runtime_dir.rs
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

static PREPARE_LOCK: Mutex<()> = Mutex::new(());

#[allow(dead_code)]
#[derive(Clone)]
pub struct Layout {
    pub root: PathBuf,           // ~/.vibe-trading
    pub runtime_agent: PathBuf,  // ~/.vibe-trading/runtime/agent
    pub runtime_libs: PathBuf,   // ~/.vibe-trading/runtime/libs (按需安装的可选依赖)
    pub marker: PathBuf,         // ~/.vibe-trading/runtime/.installed_version
    pub user_env: PathBuf,       // ~/.vibe-trading/.env
    pub desktop_config: PathBuf, // ~/.vibe-trading/desktop-config.json
    pub member_key: PathBuf,     // ~/.vibe-trading/member.key
    pub venv_dir: PathBuf,       // ~/.vibe-trading/venv
    pub venv_python: PathBuf,    // venv interpreter executable (platform-dependent)
    pub sessions_dir: PathBuf,   // ~/.vibe-trading/sessions
    pub logs_dir: PathBuf,       // ~/.vibe-trading/logs
}

impl Layout {
    pub fn new(home_vibe: &Path) -> Self {
        let venv_dir = home_vibe.join("venv");
        let venv_python = if cfg!(windows) {
            venv_dir.join("Scripts").join("python.exe")
        } else {
            venv_dir.join("bin").join("python")
        };
        Self {
            root: home_vibe.to_path_buf(),
            runtime_agent: home_vibe.join("runtime").join("agent"),
            runtime_libs: home_vibe.join("runtime").join("libs"),
            marker: home_vibe.join("runtime").join(".installed_version"),
            user_env: home_vibe.join(".env"),
            desktop_config: home_vibe.join("desktop-config.json"),
            member_key: home_vibe.join("member.key"),
            venv_dir,
            venv_python,
            sessions_dir: home_vibe.join("sessions"),
            logs_dir: home_vibe.join("logs"),
        }
    }

    /// 生产用: 解析 ~/.vibe-trading
    pub fn from_home() -> Result<Self, String> {
        let home = dirs::home_dir().ok_or("home dir unavailable")?;
        Ok(Self::new(&home.join(".vibe-trading")))
    }
}

fn copy_dir_recursive(src: &Path, dst: &Path) -> Result<(), String> {
    fs::create_dir_all(dst).map_err(|e| format!("mkdir {dst:?}: {e}"))?;
    for entry in fs::read_dir(src).map_err(|e| format!("read_dir {src:?}: {e}"))? {
        let entry = entry.map_err(|e| e.to_string())?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copy_dir_recursive(&from, &to)?;
        } else {
            fs::copy(&from, &to).map_err(|e| format!("copy {from:?}: {e}"))?;
        }
    }
    Ok(())
}

fn replace_dir_recursive(src: &Path, dst: &Path) -> Result<(), String> {
    if dst.exists() {
        fs::remove_dir_all(dst).map_err(|e| format!("remove_dir_all {dst:?}: {e}"))?;
    }
    copy_dir_recursive(src, dst)
}

fn merge_missing_env_keys(existing: &str, seed: &str) -> String {
    let mut known_keys: HashSet<String> = crate::auth::parse_env_to_map(existing)
        .into_keys()
        .collect();
    let mut merged = existing.to_string();

    for line in seed.lines() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let Some((key, _)) = trimmed.split_once('=') else {
            continue;
        };
        let key = key.trim();
        if key.is_empty() || !known_keys.insert(key.to_string()) {
            continue;
        }
        if !merged.is_empty() && !merged.ends_with('\n') {
            merged.push('\n');
        }
        merged.push_str(line);
        merged.push('\n');
    }

    merged
}

pub fn prepare(
    bundle_agent: &Path,
    bundle_env_seed: &Path,
    bundle_version: &Path,
    bundle_frontend_dist: Option<&Path>,
    layout: &Layout,
) -> Result<(), String> {
    let _guard = PREPARE_LOCK
        .lock()
        .map_err(|e| format!("prepare lock poisoned: {e}"))?;
    if !bundle_agent.exists() {
        return Err(format!("bundle agent template missing: {bundle_agent:?}"));
    }
    let bundle_ver = fs::read_to_string(bundle_version)
        .map_err(|e| format!("read bundle VERSION {bundle_version:?}: {e}"))?;
    let installed = fs::read_to_string(&layout.marker).ok();
    let mut action = crate::version::decide(installed.as_deref(), &bundle_ver);
    if matches!(action, crate::version::Action::Reuse) && !layout.runtime_agent.is_dir() {
        action = crate::version::Action::FirstRun;
    }

    fs::create_dir_all(&layout.root).map_err(|e| format!("create root {:?}: {e}", layout.root))?;
    // 可写可选依赖目录：始终确保存在；升级时不被清空（与 runtime_agent 的
    // copy_dir_recursive 无关——libs 永远是用户拥有的数据，不来自 bundle 模板）。
    fs::create_dir_all(&layout.runtime_libs)
        .map_err(|e| format!("create runtime_libs {:?}: {e}", layout.runtime_libs))?;
    // 会话/日志目录在 home 根层，属用户数据：始终确保存在，升级不清空。
    // (venv 由 vibe-trading bootstrap 创建；这里只确保父目录 home 就绪。)
    for dir in [&layout.sessions_dir, &layout.logs_dir] {
        fs::create_dir_all(dir).map_err(|e| format!("create {dir:?}: {e}"))?;
    }

    if let Some(frontend_dist) = bundle_frontend_dist {
        if frontend_dist.exists() {
            let dest = layout
                .runtime_agent
                .parent()
                .unwrap()
                .join("frontend")
                .join("dist");
            replace_dir_recursive(frontend_dist, &dest)?;
        }
    }

    match action {
        crate::version::Action::Reuse => {}
        crate::version::Action::FirstRun | crate::version::Action::Upgrade => {
            copy_dir_recursive(bundle_agent, &layout.runtime_agent)?;
            if bundle_env_seed.exists() {
                if layout.user_env.exists() {
                    let existing = fs::read_to_string(&layout.user_env)
                        .map_err(|e| format!("read user .env: {e}"))?;
                    let seed = fs::read_to_string(bundle_env_seed)
                        .map_err(|e| format!("read seed .env: {e}"))?;
                    let merged = merge_missing_env_keys(&existing, &seed);
                    crate::auth::write_env_atomic(&layout.user_env, &merged)
                        .map_err(|e| format!("merge .env: {e}"))?;
                } else {
                    fs::copy(bundle_env_seed, &layout.user_env)
                        .map_err(|e| format!("seed .env: {e}"))?;
                }
            }
            if let Some(parent) = layout.marker.parent() {
                fs::create_dir_all(parent).map_err(|e| e.to_string())?;
            }
            fs::write(&layout.marker, bundle_ver.trim())
                .map_err(|e| format!("write marker: {e}"))?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    fn make_bundle(dir: &std::path::Path, version: &str) {
        let agent = dir.join("agent");
        fs::create_dir_all(agent.join("src")).unwrap();
        fs::write(agent.join("api_server.py"), "# v1").unwrap();
        fs::write(agent.join(".env"), "SEED=1").unwrap();
        fs::write(dir.join("VERSION"), version).unwrap();
    }

    #[test]
    fn first_run_copies_agent_seeds_env_writes_marker() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let layout = Layout::new(&home);

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        assert!(layout.runtime_agent.join("api_server.py").exists());
        assert_eq!(fs::read_to_string(layout.user_env).unwrap(), "SEED=1");
        assert_eq!(fs::read_to_string(layout.marker).unwrap().trim(), "1.0.0");
    }

    #[test]
    fn upgrade_preserves_existing_user_env_including_remembered_auth() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let layout = Layout::new(&home);

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        let user_env = "SEED=user-config\nLLM_PROVIDER=openai\nUSER_ACCESS_TOKEN=remembered-access-token\nUSER_REFRESH_TOKEN=remembered-refresh-token\nUSER_TOKEN_EXPIRE=1800000000\nUSER_REFRESH_EXPIRE=1900000000\nUSER_REMEMBER_UNTIL=1790000000\n";
        fs::write(&layout.user_env, user_env).unwrap();

        fs::write(bundle.join("agent/.env"), "SEED=2\nNEW_KEY=fresh\n").unwrap();
        fs::write(bundle.join("VERSION"), "2.0.0").unwrap();

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        assert_eq!(
            fs::read_to_string(&layout.user_env).unwrap(),
            format!("{user_env}NEW_KEY=fresh\n")
        );
        let session = crate::auth::read_env_token_section_at(&layout, 1_700_000_000).unwrap();
        assert_eq!(session.token, "remembered-access-token");
        assert_eq!(session.refresh_token, "remembered-refresh-token");
    }

    #[test]
    fn upgrade_refreshes_code_but_preserves_data_dirs() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let layout = Layout::new(&home);
        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();
        fs::create_dir_all(layout.runtime_agent.join("runs/r1")).unwrap();
        fs::write(layout.runtime_agent.join("runs/r1/x"), "data").unwrap();
        // bundle 升级到 v2
        fs::write(bundle.join("agent/api_server.py"), "# v2").unwrap();
        fs::write(bundle.join("VERSION"), "2.0.0").unwrap();

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        assert_eq!(
            fs::read_to_string(layout.runtime_agent.join("api_server.py")).unwrap(),
            "# v2"
        );
        assert!(
            layout.runtime_agent.join("runs/r1/x").exists(),
            "user data preserved"
        );
        assert_eq!(fs::read_to_string(layout.marker).unwrap().trim(), "2.0.0");
    }

    #[test]
    fn prepare_failure_returns_readable_error() {
        let tmp = tempdir().unwrap();
        let home = tmp.path().join("home");
        let layout = Layout::new(&home);
        let missing = tmp.path().join("nope/agent");
        let err = prepare(
            &missing,
            &missing.join(".env"),
            &tmp.path().join("VERSION"),
            None,
            &layout,
        )
        .unwrap_err();
        assert!(
            err.contains("agent") || err.contains("VERSION"),
            "msg: {err}"
        );
    }

    #[test]
    fn layout_exposes_runtime_libs_path() {
        let home = std::path::Path::new("/fake/home/.vibe-trading");
        let layout = Layout::new(home);
        assert_eq!(layout.runtime_libs, home.join("runtime").join("libs"));
    }

    #[test]
    fn prepare_creates_runtime_libs_dir() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let layout = Layout::new(&home);

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        assert!(
            layout.runtime_libs.exists(),
            "runtime_libs should be created"
        );
        assert!(layout.runtime_libs.is_dir());
    }

    #[test]
    fn reuse_marker_without_runtime_agent_recopies_bundle_agent() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let layout = Layout::new(&home);
        fs::create_dir_all(layout.marker.parent().unwrap()).unwrap();
        fs::write(&layout.marker, "1.0.0").unwrap();

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        assert!(layout.runtime_agent.join("api_server.py").exists());
    }

    #[test]
    fn reuse_refreshes_frontend_dist_and_removes_stale_assets() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let frontend_dist = bundle.join("frontend/dist");
        fs::create_dir_all(frontend_dist.join("assets")).unwrap();
        fs::write(
            frontend_dist.join("index.html"),
            r#"<script src="/assets/old.js"></script>"#,
        )
        .unwrap();
        fs::write(frontend_dist.join("assets/old.js"), "old bundle").unwrap();
        let layout = Layout::new(&home);

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            Some(&frontend_dist),
            &layout,
        )
        .unwrap();

        fs::write(
            frontend_dist.join("index.html"),
            r#"<script src="/assets/new.js"></script>"#,
        )
        .unwrap();
        fs::remove_file(frontend_dist.join("assets/old.js")).unwrap();
        fs::write(frontend_dist.join("assets/new.js"), "new bundle").unwrap();

        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            Some(&frontend_dist),
            &layout,
        )
        .unwrap();

        let runtime_dist = layout.runtime_agent.parent().unwrap().join("frontend/dist");
        assert_eq!(
            fs::read_to_string(runtime_dist.join("index.html")).unwrap(),
            r#"<script src="/assets/new.js"></script>"#
        );
        assert!(runtime_dist.join("assets/new.js").exists());
        assert!(!runtime_dist.join("assets/old.js").exists());
    }

    #[test]
    fn layout_exposes_venv_python_path() {
        let home = std::path::Path::new("/fake/home/.vibe-trading");
        let layout = Layout::new(home);
        assert_eq!(layout.venv_dir, home.join("venv"));
        if cfg!(windows) {
            assert_eq!(
                layout.venv_python,
                home.join("venv").join("Scripts").join("python.exe")
            );
        } else {
            assert_eq!(
                layout.venv_python,
                home.join("venv").join("bin").join("python")
            );
        }
    }

    #[test]
    fn layout_exposes_sessions_and_logs_at_home_root() {
        let home = std::path::Path::new("/fake/home/.vibe-trading");
        let layout = Layout::new(home);
        assert_eq!(layout.sessions_dir, home.join("sessions"));
        assert_eq!(layout.logs_dir, home.join("logs"));
        // 确认 sessions/logs 在 home 根目录，不在 runtime/ 内
        assert!(!layout.sessions_dir.starts_with(home.join("runtime")));
        assert!(!layout.logs_dir.starts_with(home.join("runtime")));
    }

    #[test]
    fn upgrade_preserves_live_audit_and_sessions() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        let home = tmp.path().join("home");
        make_bundle(&bundle, "1.0.0");
        let layout = Layout::new(&home);

        // 先首次运行，创建基础结构
        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        // 模拟用户在 home 根部创建 live/ 和 sessions/
        let live_dir = home.join("live");
        let sessions_dir = home.join("sessions");
        fs::create_dir_all(&live_dir).unwrap();
        fs::write(live_dir.join("audit_ledger.json"), r#"{"trades":[]}"#).unwrap();
        fs::create_dir_all(&sessions_dir).unwrap();
        fs::write(sessions_dir.join("session_abc.json"), r#"{"runId":"abc"}"#).unwrap();

        // 升级到 v2
        fs::write(bundle.join("agent/api_server.py"), "# v2").unwrap();
        fs::write(bundle.join("VERSION"), "2.0.0").unwrap();
        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        // live/ 资产必须存活
        assert!(
            live_dir.join("audit_ledger.json").exists(),
            "live/audit_ledger.json must survive upgrade"
        );
        assert_eq!(
            fs::read_to_string(live_dir.join("audit_ledger.json")).unwrap(),
            r#"{"trades":[]}"#
        );

        // sessions/ 资产必须存活
        assert!(
            sessions_dir.join("session_abc.json").exists(),
            "sessions/session_abc.json must survive upgrade"
        );
        assert_eq!(
            fs::read_to_string(sessions_dir.join("session_abc.json")).unwrap(),
            r#"{"runId":"abc"}"#
        );
    }

    #[test]
    fn prepare_readable_error_on_unwritable_target() {
        let tmp = tempdir().unwrap();
        let bundle = tmp.path().join("bundle");
        make_bundle(&bundle, "1.0.0");
        let home = tmp.path().join("home");
        let layout = Layout::new(&home);

        // 先成功 prepare 一次，创建 marker 文件
        prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        )
        .unwrap();

        // 再次升级，但把 marker 文件路径替换为一个目录，让 fs::write(&marker, ...) 失败
        fs::write(bundle.join("VERSION"), "2.0.0").unwrap();
        // 删除 marker 文件，在它位置上创建目录
        fs::remove_file(&layout.marker).unwrap();
        fs::create_dir_all(&layout.marker).unwrap();

        let err = prepare(
            &bundle.join("agent"),
            &bundle.join("agent/.env"),
            &bundle.join("VERSION"),
            None,
            &layout,
        );
        assert!(
            err.is_err(),
            "should fail writing to a path that is a directory"
        );
        let msg = err.unwrap_err();
        // 错误消息必须包含失败的路径信息
        assert!(
            msg.contains("marker") || msg.contains("agent") || msg.contains("runtime"),
            "error message must mention the failed path, got: {msg}"
        );
    }
}
