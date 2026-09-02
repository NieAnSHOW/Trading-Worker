// src-tauri/src/sidecar.rs
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use crate::auth::VipRuntimeCredential;

const BOOT: &str = "import cli, sys; raise SystemExit(cli.main(sys.argv[1:]))";

/// 将桌面 VIP 链路的非敏感诊断事件同时输出到 stderr 和持久日志。
/// 凭据值、令牌、密文与 Base URL 不得传入此函数。
pub fn log_vip_runtime_event(log_dir: &Path, event: &str) {
    let line = format!("[vip-runtime] {event}");
    eprintln!("{line}");
    if std::fs::create_dir_all(log_dir).is_err() {
        eprintln!("[vip-runtime] unable to create persistent log directory");
        return;
    }
    use std::io::Write;
    let result = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("desktop-vip-runtime.log"))
        .and_then(|mut file| writeln!(file, "{line}"));
    if result.is_err() {
        eprintln!("[vip-runtime] unable to persist diagnostic event");
    }
}

/// Resolve the bundled Pi sidecar binary path from Tauri's resource directory.
/// Resource layout (tauri.conf.json bundle.resources): "pi" -> <resource_dir>/pi/<target>/<bin>
pub fn vibe_pi_sidecar_bin(resource_dir: &std::path::Path) -> Option<std::path::PathBuf> {
    // 与 scripts/desktop/build-pi.sh 的目标目录保持一致（B7）
    let target = match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => "macos-aarch64",
        ("macos", "x86_64") => "macos-x86_64",
        ("windows", "x86_64") => "windows-x64",
        _ => return None,
    };
    let bin = if std::env::consts::OS == "windows" { "pi.exe" } else { "pi" };
    let path = resource_dir.join("pi").join(target).join(bin);
    if path.exists() { Some(path) } else { None }
}

pub fn build_cmd_with_vip(
    python: &Path,
    runtime_agent: &Path,
    port: u16,
    runtime_libs: &Path,
    sessions_dir: &Path,
    vip: Option<&VipRuntimeCredential>,
    pi_sidecar_bin: Option<&Path>,
) -> std::process::Command {
    let mut cmd = Command::new(python);
    cmd.arg("-c")
        .arg(BOOT)
        .arg("serve")
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .current_dir(runtime_agent)
        .env("PYTHONPATH", runtime_agent)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("VIBE_RUNTIME_LIBS", runtime_libs)
        .env("VIBE_SESSIONS_DIR", sessions_dir)
        // 桌面 sidecar 启动时自动恢复 enabled IM 渠道(复用磁盘 token),避免
        // "启停服务→微信 poll 循环随进程被杀→重启后渠道不自动起→前端要求重新扫码"。
        // CLI 直接 `vibe-trading serve` 不经此函数,行为不受影响。
        .env("VIBE_TRADING_CHANNELS_AUTO_START", "1")
        .env("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        .env_remove("VIBE_DESKTOP_VIP_PROVISIONED")
        .env_remove("VIBE_DESKTOP_VIP_API_KEY")
        .env_remove("VIBE_DESKTOP_VIP_BASE_URL")
        .env_remove("VIBE_DESKTOP_VIP_MODELS_JSON")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    if let Some(vip) = vip {
        cmd.env("VIBE_DESKTOP_VIP_PROVISIONED", "1")
            .env("VIBE_DESKTOP_VIP_API_KEY", &vip.api_key)
            .env("VIBE_DESKTOP_VIP_BASE_URL", &vip.base_url)
            .env(
                "VIBE_DESKTOP_VIP_MODELS_JSON",
                serde_json::to_string(&vip.models).expect("Vec<String> always serializes"),
            );
    }

    // B7：打包态把 Pi sidecar 二进制路径透传给 Python serve 进程
    // （PiSidecarClient._default_command() 优先使用该 env 指向的编译产物）。
    // 缺失时不设置 env：Python 侧回退 bun dev 命令（开发态），打包冒烟（smoke_pi.py）会拦截该回归。
    if let Some(pi_bin) = pi_sidecar_bin {
        cmd.env("VIBE_PI_SIDECAR_BIN", pi_bin);
    }

    #[cfg(unix)]
    unsafe {
        use std::os::unix::process::CommandExt;
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    cmd
}

pub fn spawn(
    python: &Path,
    runtime_agent: &Path,
    port: u16,
    runtime_libs: &Path,
    sessions_dir: &Path,
    logs_dir: &Path,
    vip: Option<&VipRuntimeCredential>,
    pi_sidecar_bin: Option<&Path>,
) -> Result<Child, String> {
    let models_count = vip.map(|credential| credential.models.len());
    log_vip_runtime_event(
        logs_dir,
        &format!(
            "preparing sidecar environment (port={port}, vip_runtime_credential={}, models_count={})",
            vip.is_some(),
            models_count.map_or_else(|| "n/a".to_string(), |count| count.to_string())
        ),
    );
    let mut cmd = build_cmd_with_vip(python, runtime_agent, port, runtime_libs, sessions_dir, vip, pi_sidecar_bin);
    // ponytail: kill stale listener before bind, else await_health may
    // connect to a leftover sidecar from a killed previous session.
    crate::port::kill_listener_on_port(port);
    cmd.spawn()
        .map(|child| {
            log_vip_runtime_event(
                logs_dir,
                &format!("sidecar process spawned (pid={})", child.id()),
            );
            child
        })
        .map_err(|e| {
            log_vip_runtime_event(logs_dir, "sidecar process spawn failed");
            format!("spawn sidecar failed: {e}")
        })
}

/// SIGTERM 后回收 sidecar 的最长等待时间。超时则升级为 SIGKILL 兜底。
///
/// 5s 同时覆盖 Python 侧 uvicorn 的 `timeout_graceful_shutdown=5s`——正常情况下
/// sidecar 会在此窗口内自行退出；若它卡住（graceful shutdown 等不到 SSE 连接关闭
/// 等），SIGKILL 兜底确保 `terminate` 一定在此上限内返回。
#[cfg(unix)]
const TERMINATE_GRACE: Duration = Duration::from_secs(5);

/// mac/unix: kill by process group (child.id() is the pgid, because it is the group leader).
/// killpg sends SIGTERM to all processes in the group; we then wait for the leader to exit.
///
/// 必须带超时兜底：若 sidecar 收到 SIGTERM 后不退出（uvicorn 默认 `timeout_graceful_shutdown=None`,
/// 会无限等待活跃 SSE 连接关闭），`child.wait()` 会无限阻塞——而本函数运行在
/// `RunEvent::ExitRequested` 回调即 Tauri 主事件循环线程上，一旦阻塞，应用窗口
/// 无法关闭、进程无法退出，表现为「退出时卡死 / 必须强制退出」。
#[cfg(unix)]
pub fn terminate(child: &mut Child) {
    let pid = child.id() as i32;
    unsafe {
        libc::killpg(pid, libc::SIGTERM);
    }
    wait_or_kill(child, pid, TERMINATE_GRACE);
}

/// SIGTERM 之后限时轮询子进程；超时或异常则对整个进程组 SIGKILL 再回收。
#[cfg(unix)]
fn wait_or_kill(child: &mut Child, pid: i32, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(_) => break,
        }
    }
    // 超时兜底：SIGKILL 整个进程组（连同 sidecar fork 出的子孙进程），再回收僵尸。
    unsafe {
        libc::killpg(pid, libc::SIGKILL);
    }
    let _ = child.wait();
}

/// Windows 进程清理：用 `taskkill /F /T` 递归终止整个进程树，再回收直接子进程。
///
/// `child.kill()`（即 `TerminateProcess`）只结束直接子进程；uvicorn worker、
/// Python subprocess 等孙进程仍在运行，会持有 python-runtime 目录下的
/// `.pyd`/`.dll` 文件句柄——安装程序覆写这些文件时出现
/// "Error opening file for writing: ..._asyncio.pyd" 报错。
/// `taskkill /F /T /PID <pid>` 等价于 Unix 的 `killpg`，内核级递归终止
/// 整棵子进程树（包括孙进程），确保所有文件句柄在安装前全部释放。
#[cfg(windows)]
pub fn terminate(child: &mut Child) {
    let pid = child.id();
    // /F 强制; /T 递归终止子进程树; 忽略返回值（进程已退出时会报错 128，属正常）
    let _ = std::process::Command::new("taskkill")
        .args(["/F", "/T", "/PID", &pid.to_string()])
        .output();
    // 回收直接子进程的句柄，避免僵尸进程
    let _ = child.wait();
}

pub enum Ready {
    Ok,
    ProcessExited(Option<i32>),
    Timeout,
}

pub fn health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/health")
}

/// Poll /health endpoint, monitoring child process for early exit.
pub fn await_health(child: &mut Child, port: u16) -> Ready {
    let url = health_url(port);
    let client = reqwest::blocking::Client::new();
    let deadline = Instant::now() + Duration::from_secs(120); // was 60; Python cold-start (pandas/scipy/duckdb) can exceed 60 s on first run
    while Instant::now() < deadline {
        if let Ok(Some(status)) = child.try_wait() {
            return Ready::ProcessExited(status.code());
        }
        if let Ok(resp) = client.get(&url).timeout(Duration::from_millis(1000)).send() {
            if resp.status().is_success() {
                return Ready::Ok;
            }
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Ready::Timeout
}

/// 为已启动的 sidecar 各开一个后台线程消费 stdout/stderr 管道，
/// 按天日志的保留窗口；早于「今天 - N 天」的 `sidecar-*.log` 在跨天时被清理。
const LOG_RETAIN_DAYS: i64 = 14;

/// epoch 天 → (year, month, day)。Howard Hinnant civil_from_days，无日期库依赖。
/// <https://howardhinnant.github.io/date_algorithms.html>
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn today_epoch_day() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64 / 86400)
        .unwrap_or(0)
}

/// 当天日志文件名 `sidecar-YYYY-MM-DD.log`（UTC 天）。
fn today_stamp() -> String {
    let (y, m, d) = civil_from_days(today_epoch_day());
    format!("sidecar-{y:04}-{m:02}-{d:02}.log")
}

/// 清理日志目录：删保留窗口外的旧 `sidecar-YYYY-MM-DD.log`，以及无日期格式的
/// 遗留 `sidecar.log`（旧版本产物，新代码只写带日期格式）。
/// `YYYY-MM-DD` 固定宽度，字典序即时间序，直接与 cutoff 字符串比较。
fn cleanup_old_logs(log_dir: &Path, today_day: i64) {
    let (y, m, d) = civil_from_days(today_day - LOG_RETAIN_DAYS);
    let cutoff = format!("{y:04}-{m:02}-{d:02}");
    let Ok(entries) = std::fs::read_dir(log_dir) else {
        return;
    };
    for ent in entries.flatten() {
        let fname = ent.file_name();
        let Some(name) = fname.to_str() else {
            continue;
        };
        // 遗留的无日期 sidecar.log（旧版本格式）直接删——新代码不再写它
        if name == "sidecar.log" {
            let _ = std::fs::remove_file(ent.path());
            continue;
        }
        let Some(date) = name
            .strip_prefix("sidecar-")
            .and_then(|s| s.strip_suffix(".log"))
        else {
            continue;
        };
        if date.len() == 10 && date < cutoff.as_str() {
            let _ = std::fs::remove_file(ent.path());
        }
    }
}

/// 防止管道缓冲区（macOS/Linux ~64 KB）写满后 Python 端 write() 阻塞，
/// 导致 uvicorn event loop 卡死（长时间运行假死根因）。
///
/// 日志按天追加写入 `log_dir/sidecar-YYYY-MM-DD.log`（BufWriter + 行级 flush），
/// 跨天自动切换并清理保留窗口外的旧文件。磁盘满时停止写入但继续消费管道——
/// 管道永不满，sidecar 运行不受影响。
pub fn drain_child_pipes(child: &mut Child, log_dir: &Path) {
    if let Some(stdout) = child.stdout.take() {
        let dir = log_dir.to_path_buf();
        std::thread::spawn(move || drain_stream(stdout, &dir, "[out]"));
    }
    if let Some(stderr) = child.stderr.take() {
        let dir = log_dir.to_path_buf();
        std::thread::spawn(move || drain_stream(stderr, &dir, "[err]"));
    }
}

fn drain_stream<R: std::io::Read>(stream: R, dir: &Path, tag: &str) {
    use std::io::BufRead;
    let mut cur_day = today_epoch_day();
    let mut w = open_log_append(&dir.join(today_stamp()));
    cleanup_old_logs(dir, cur_day);
    for line in std::io::BufReader::new(stream)
        .lines()
        .map_while(Result::ok)
    {
        let day = today_epoch_day();
        if day != cur_day {
            // 跨天：重开当天文件并清理过期日志。
            cur_day = day;
            w = open_log_append(&dir.join(today_stamp()));
            cleanup_old_logs(dir, day);
        }
        drain_line(&mut w, tag, &line);
    }
}

fn open_log_append(path: &Path) -> Option<std::io::BufWriter<std::fs::File>> {
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map(std::io::BufWriter::new)
        .ok()
}

fn drain_line(w: &mut Option<std::io::BufWriter<std::fs::File>>, tag: &str, line: &str) {
    use std::io::Write;
    let Some(writer) = w else { return };
    if writeln!(writer, "{tag} {line}").is_err() || writer.flush().is_err() {
        *w = None; // 磁盘满：停写，但外层循环继续消费管道
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn vip_runtime_events_are_persisted_in_desktop_log() {
        let temp = tempfile::tempdir().unwrap();

        log_vip_runtime_event(temp.path(), "credential request started");

        let text = std::fs::read_to_string(temp.path().join("desktop-vip-runtime.log")).unwrap();
        assert_eq!(text, "[vip-runtime] credential request started\n");
    }

    #[test]
    fn spawn_command_has_expected_args() {
        let python = Path::new("/fake/python3");
        let agent = Path::new("/fake/agent");
        let cmd = build_cmd_with_vip(
            python,
            agent,
            8899,
            Path::new("/fake/libs"),
            Path::new("/fake/sessions"),
            None,
            None,
        );

        // Verify the program path is set correctly
        assert_eq!(cmd.get_program(), "/fake/python3");

        // Verify key env vars via iterator (get_envs returns HashMap<&OsStr, Option<&OsStr>>)
        let mut found_pythonpath = false;
        let mut found_bytecode = false;
        for (key, val) in cmd.get_envs() {
            let k = key.to_str().unwrap_or("");
            let v = val.and_then(|v| v.to_str()).unwrap_or("");
            if k == "PYTHONPATH" && v == "/fake/agent" {
                found_pythonpath = true;
            }
            if k == "PYTHONDONTWRITEBYTECODE" && v == "1" {
                found_bytecode = true;
            }
        }
        assert!(found_pythonpath, "PYTHONPATH not set correctly");
        assert!(found_bytecode, "PYTHONDONTWRITEBYTECODE not set correctly");
    }

    #[test]
    fn custom_command_removes_all_vip_runtime_environment_values() {
        let cmd = build_cmd_with_vip(
            Path::new("/fake/python3"),
            Path::new("/fake/agent"),
            8899,
            Path::new("/fake/libs"),
            Path::new("/fake/sessions"),
            None,
            None,
        );

        for key in [
            "VIBE_DESKTOP_VIP_PROVISIONED",
            "VIBE_DESKTOP_VIP_API_KEY",
            "VIBE_DESKTOP_VIP_BASE_URL",
            "VIBE_DESKTOP_VIP_MODELS_JSON",
        ] {
            assert!(
                cmd.get_envs()
                    .any(|(name, value)| name == key && value.is_none()),
                "{key} must be explicitly removed for custom mode"
            );
        }
    }

    #[test]
    fn boot_const_is_valid() {
        // BOOT is the -c argument for Python, which imports cli and calls cli.main
        assert!(BOOT.contains("cli.main"));
        assert!(BOOT.contains("import"));
    }

    #[test]
    fn civil_from_days_known_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(31), (1970, 2, 1)); // 1970 非闰，1 月 31 天
        assert_eq!(civil_from_days(365), (1971, 1, 1)); // 1970 整年 365 天
        assert_eq!(civil_from_days(730), (1972, 1, 1)); // 1970+1971 各 365
        assert_eq!(civil_from_days(789), (1972, 2, 29)); // 闰日
        assert_eq!(civil_from_days(790), (1972, 3, 1));
    }

    #[test]
    fn today_stamp_is_dated_filename() {
        let s = today_stamp();
        assert!(s.starts_with("sidecar-"));
        assert!(s.ends_with(".log"));
        assert_eq!(s.len(), "sidecar-0000-00-00.log".len()); // 21 字符
    }

    #[test]
    fn cleanup_old_logs_keeps_recent_removes_expired() {
        use std::fs;
        let base = std::env::temp_dir().join("vibe_sidecar_cleanup_test");
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(&base).unwrap();

        let today = today_epoch_day();
        // 远古（今天-40，应删）和昨天（应留）各放一个
        let (oy, om, od) = civil_from_days(today - 40);
        let old_name = format!("sidecar-{oy:04}-{om:02}-{od:02}.log");
        let (ry, rm, rd) = civil_from_days(today - 1);
        let recent_name = format!("sidecar-{ry:04}-{rm:02}-{rd:02}.log");
        fs::File::create(base.join(&old_name)).unwrap();
        fs::File::create(base.join(&recent_name)).unwrap();
        fs::File::create(base.join("sidecar.log")).unwrap(); // 旧版本遗留

        cleanup_old_logs(&base, today);

        assert!(!base.join(&old_name).exists(), "过期日志应被清理");
        assert!(base.join(&recent_name).exists(), "近期日志应保留");
        assert!(
            !base.join("sidecar.log").exists(),
            "无日期遗留 sidecar.log 应被清理"
        );

        fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn build_cmd_includes_serve_args() {
        let python = Path::new("/fake/python3");
        let agent = Path::new("/fake/agent");
        let cmd = build_cmd_with_vip(
            python,
            agent,
            8899,
            Path::new("/fake/libs"),
            Path::new("/fake/sessions"),
            None,
            None,
        );

        let args: Vec<&str> = cmd.get_args().map(|a| a.to_str().unwrap()).collect();
        let args_str = args.join(" ");
        assert!(
            args_str.contains("serve"),
            "expected 'serve' in args: {}",
            args_str
        );
        assert!(
            args_str.contains("127.0.0.1"),
            "expected '127.0.0.1' in args: {}",
            args_str
        );
        assert!(
            args_str.contains("8899"),
            "expected '8899' in args: {}",
            args_str
        );
    }

    #[test]
    fn build_cmd_injects_runtime_libs_env() {
        let python = Path::new("/fake/python3");
        let agent = Path::new("/fake/agent");
        let libs = Path::new("/fake/libs");
        let cmd = build_cmd_with_vip(python, agent, 8899, libs, Path::new("/fake/sessions"), None, None);

        let mut found = false;
        for (key, val) in cmd.get_envs() {
            if key.to_str() == Some("VIBE_RUNTIME_LIBS")
                && val.and_then(|v| v.to_str()) == Some("/fake/libs")
            {
                found = true;
            }
        }
        assert!(found, "VIBE_RUNTIME_LIBS not set to libs path");
    }

    #[test]
    fn build_cmd_does_not_force_pip_mirror_env() {
        // The mirror must come from optional_deps.mirror config (explicit
        // --index-url per install). A forced PIP_INDEX_URL env silently
        // overrode the user's "off" (official PyPI) selection.
        let python = Path::new("/fake/python3");
        let agent = Path::new("/fake/agent");
        let libs = Path::new("/fake/libs");
        let cmd = build_cmd_with_vip(python, agent, 8899, libs, Path::new("/fake/sessions"), None, None);

        for (key, _val) in cmd.get_envs() {
            assert_ne!(
                key.to_str(),
                Some("PIP_INDEX_URL"),
                "PIP_INDEX_URL must not be forced into the serve env"
            );
        }
    }
    #[test]
    fn build_cmd_injects_sessions_dir_env() {
        let python = Path::new("/fake/python3");
        let agent = Path::new("/fake/agent");
        let libs = Path::new("/fake/libs");
        let sessions = Path::new("/fake/home/.vibe-trading/sessions");
        let cmd = build_cmd_with_vip(python, agent, 8899, libs, sessions, None, None);

        let mut found = false;
        for (key, val) in cmd.get_envs() {
            if key.to_str() == Some("VIBE_SESSIONS_DIR")
                && val.and_then(|v| v.to_str()) == Some("/fake/home/.vibe-trading/sessions")
            {
                found = true;
            }
        }
        assert!(found, "VIBE_SESSIONS_DIR not set to sessions path");
    }

    #[test]
    fn build_cmd_injects_channels_autostart() {
        // 桌面 sidecar 必须自动恢复 IM 渠道:启停服务会杀进程,若启动时不
        // autostart,微信 poll 循环不会恢复 → 前端判定需重新扫码(磁盘 token 仍在)。
        let python = Path::new("/fake/python3");
        let agent = Path::new("/fake/agent");
        let cmd = build_cmd_with_vip(
            python,
            agent,
            8899,
            Path::new("/fake/libs"),
            Path::new("/fake/sessions"),
            None,
            None,
        );
        let mut found = false;
        for (key, val) in cmd.get_envs() {
            if key.to_str() == Some("VIBE_TRADING_CHANNELS_AUTO_START")
                && val.and_then(|v| v.to_str()) == Some("1")
            {
                found = true;
            }
        }
        assert!(found, "VIBE_TRADING_CHANNELS_AUTO_START=1 not injected");
    }

    #[test]
    fn build_cmd_injects_pi_sidecar_bin_env() {
        // B7：打包态 Python serve 进程必须拿到 VIBE_PI_SIDECAR_BIN（指向打包内 Pi 二进制）
        let cmd = build_cmd_with_vip(
            Path::new("/fake/python3"),
            Path::new("/fake/agent"),
            8899,
            Path::new("/fake/libs"),
            Path::new("/fake/sessions"),
            None,
            Some(Path::new("/fake/Resources/pi/macos-aarch64/pi")),
        );

        let mut found = false;
        for (key, val) in cmd.get_envs() {
            if key.to_str() == Some("VIBE_PI_SIDECAR_BIN")
                && val.and_then(|v| v.to_str()) == Some("/fake/Resources/pi/macos-aarch64/pi")
            {
                found = true;
            }
        }
        assert!(found, "VIBE_PI_SIDECAR_BIN not injected when pi binary resolved");
    }

    #[test]
    fn build_cmd_omits_pi_sidecar_bin_env_when_not_resolved() {
        // 开发态（无打包资源）：env 不设置，Python 侧回退 bun dev 命令
        let cmd = build_cmd_with_vip(
            Path::new("/fake/python3"),
            Path::new("/fake/agent"),
            8899,
            Path::new("/fake/libs"),
            Path::new("/fake/sessions"),
            None,
            None,
        );

        for (key, _val) in cmd.get_envs() {
            assert_ne!(
                key.to_str(),
                Some("VIBE_PI_SIDECAR_BIN"),
                "VIBE_PI_SIDECAR_BIN must be omitted when no bundled pi binary"
            );
        }
    }

    #[test]
    fn vibe_pi_sidecar_bin_resolves_existing_packaged_binary() {
        // 与 scripts/desktop/build-pi.sh 的目标目录保持一致（B7）
        let (target, bin) = match (std::env::consts::OS, std::env::consts::ARCH) {
            ("macos", "aarch64") => ("macos-aarch64", "pi"),
            ("macos", "x86_64") => ("macos-x86_64", "pi"),
            ("windows", "x86_64") => ("windows-x64", "pi.exe"),
            (os, arch) => panic!("unsupported test host {os}/{arch}"),
        };
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path().join("pi").join(target);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(bin), b"x").unwrap();

        let resolved = vibe_pi_sidecar_bin(tmp.path());
        assert_eq!(resolved, Some(dir.join(bin)));
    }

    #[test]
    fn vibe_pi_sidecar_bin_returns_none_when_missing() {
        let tmp = tempfile::tempdir().unwrap();
        assert_eq!(vibe_pi_sidecar_bin(tmp.path()), None);
    }

    /// 回归测试：模拟一个「收到 SIGTERM 但拒绝退出」的 sidecar（复刻 uvicorn
    /// 在 `timeout_graceful_shutdown=None` 下无限等待 SSE 关闭的场景），断言
    /// `wait_or_kill` 会升级为 SIGKILL 并在有限时间内回收，而不是无限阻塞。
    #[cfg(unix)]
    #[test]
    fn wait_or_kill_escalates_to_sigkill_when_sigterm_is_ignored() {
        use std::os::unix::process::CommandExt;

        // 复刻 build_cmd 的 setsid 语义：让子进程成为新 session/group 组长，
        // 这样 child.id() == pgid，killpg 才能命中整个进程组。
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg("trap '' TERM; sleep 30");
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let mut child = cmd.spawn().expect("spawn sh");
        let pid = child.id() as i32;

        // 先发 SIGTERM（被 trap 忽略），再进入限时回收路径。
        unsafe {
            libc::killpg(pid, libc::SIGTERM);
        }
        let start = Instant::now();
        super::wait_or_kill(&mut child, pid, Duration::from_secs(1));

        // 必须已退出，且远早于 `sleep 30` 的自然结束 —— 证明 SIGKILL 兜底生效，
        // 而非把 Tauri 主事件循环挂死。
        let status = child.try_wait().expect("try_wait after kill");
        assert!(status.is_some(), "sidecar 应被 SIGKILL 回收，而非继续挂起");
        assert!(
            start.elapsed() < Duration::from_secs(5),
            "terminate 不应阻塞超过 5s，实际 {:?}",
            start.elapsed()
        );
    }
}
