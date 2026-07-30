// tests/sidecar_tests.rs
// Integration tests for sidecar module — testing auxiliary/unit-testable functions.
use std::path::PathBuf;

#[path = "../src/runtime_dir.rs"]
mod runtime_dir;

#[path = "../src/version.rs"]
mod version;

#[path = "../src/desktop_config.rs"]
mod desktop_config;

#[path = "../src/auth.rs"]
mod auth;

// We can't directly import from the binary crate, so we test via the
// fact that the sidecar module path is accessible.
// For a proper unit-test approach, we use the module's exported functions
// by including the module source via a path attribute.

// sidecar::spawn() 调 crate::port::kill_listener_on_port;test crate 根没有 port
// 模块,补一个空桩让 include 整个 sidecar.rs 时编译通过(e44a44cb 引入该调用后,
// 此 test crate 编译即断)。测试从不调用 spawn,桩实现留空即可。
mod port {
    #[allow(dead_code)]
    pub fn kill_listener_on_port(_port: u16) {}
}

#[path = "../src/sidecar.rs"]
mod sidecar;

use auth::VipRuntimeCredential;
use sidecar::{build_cmd_with_vip, health_url};

fn env(cmd: &std::process::Command, name: &str) -> Option<String> {
    cmd.get_envs().find_map(|(key, value)| {
        (key == name)
            .then(|| value.and_then(|v| v.to_str()).map(str::to_string))
            .flatten()
    })
}

#[test]
fn sidecar_command_receives_vip_values_only_as_process_env() {
    let vip = VipRuntimeCredential {
        base_url: "https://api.example/v1".into(),
        api_key: "member-key".into(),
        models: vec!["model-a".into()],
    };
    let cmd = build_cmd_with_vip(
        std::path::Path::new("/fake/python"),
        std::path::Path::new("/fake/agent"),
        8899,
        std::path::Path::new("/fake/libs"),
        std::path::Path::new("/fake/sessions"),
        Some(&vip),
    );
    assert_eq!(
        env(&cmd, "VIBE_DESKTOP_VIP_PROVISIONED").as_deref(),
        Some("1")
    );
    assert_eq!(
        env(&cmd, "VIBE_DESKTOP_VIP_API_KEY").as_deref(),
        Some("member-key")
    );
    assert_eq!(
        env(&cmd, "VIBE_DESKTOP_VIP_BASE_URL").as_deref(),
        Some("https://api.example/v1")
    );
    assert_eq!(
        env(&cmd, "VIBE_DESKTOP_VIP_MODELS_JSON").as_deref(),
        Some(r#"["model-a"]"#)
    );
}

#[test]
fn build_cmd_sets_current_dir() {
    let python = PathBuf::from("/fake/python3");
    let agent = PathBuf::from("/fake/agent");
    let cmd = build_cmd_with_vip(
        &python,
        &agent,
        9999,
        &PathBuf::from("/fake/libs"),
        &PathBuf::from("/fake/sessions"),
        None,
    );

    assert_eq!(cmd.get_current_dir(), Some(agent.as_path()));
}

#[test]
fn build_cmd_sets_environment_vars() {
    let python = PathBuf::from("/fake/python3");
    let agent = PathBuf::from("/fake/agent");
    let cmd = build_cmd_with_vip(
        &python,
        &agent,
        9999,
        &PathBuf::from("/fake/libs"),
        &PathBuf::from("/fake/sessions"),
        None,
    );

    let envs: Vec<(&std::ffi::OsStr, Option<&std::ffi::OsStr>)> = cmd.get_envs().collect();

    let has_pythonpath = envs.iter().any(|(k, v)| {
        k.to_string_lossy() == "PYTHONPATH"
            && v.map(|s| s.to_string_lossy().contains("/fake/agent"))
                .unwrap_or(false)
    });
    assert!(has_pythonpath, "PYTHONPATH not set correctly");

    let has_pyonly = envs.iter().any(|(k, v)| {
        k.to_string_lossy() == "PYTHONDONTWRITEBYTECODE"
            && v.map(|s| s.to_string_lossy() == "1").unwrap_or(false)
    });
    assert!(has_pyonly, "PYTHONDONTWRITEBYTECODE not set to '1'");
}

#[test]
fn health_url_format() {
    assert_eq!(health_url(8899), "http://127.0.0.1:8899/health");
    assert_eq!(health_url(0), "http://127.0.0.1:0/health");
}
