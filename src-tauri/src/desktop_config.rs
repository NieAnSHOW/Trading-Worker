use std::fs;
use std::io::Write;
use std::path::Path;

#[derive(serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct DesktopConfig {
    version: u8,
    user_api_url: String,
}

fn validate_user_api_url(raw: &str) -> Result<String, String> {
    let url = tauri::Url::parse(raw)
        .map_err(|_| "userApiUrl must be an absolute HTTP(S) URL".to_string())?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(
            "userApiUrl must be an HTTP(S) origin with a host and without credentials, path, query, or fragment".into(),
        );
    }
    Ok(url.to_string().trim_end_matches('/').to_string())
}

fn validate_config(config: &DesktopConfig) -> Result<String, String> {
    if config.version != 1 {
        return Err("desktop configuration version must be 1".into());
    }
    validate_user_api_url(&config.user_api_url)
}

pub fn read_user_api_url(path: &Path) -> Option<String> {
    let source = fs::read_to_string(path).ok()?;
    let config: DesktopConfig = serde_json::from_str(&source).ok()?;
    validate_config(&config).ok()
}

pub fn import_user_api_config(path: &Path, source: &str) -> Result<String, String> {
    let mut config: DesktopConfig = serde_json::from_str(source)
        .map_err(|e| format!("invalid desktop configuration JSON: {e}"))?;
    let url = validate_config(&config)?;
    config.user_api_url = url.clone();
    let encoded =
        serde_json::to_vec(&config).map_err(|e| format!("serialize desktop configuration: {e}"))?;

    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent).map_err(|e| format!("create config directory {parent:?}: {e}"))?;
    let filename = path
        .file_name()
        .ok_or_else(|| "desktop configuration path must name a file".to_string())?
        .to_string_lossy();

    for attempt in 0..10 {
        let temporary = parent.join(format!(
            ".{filename}.{}.{}.tmp",
            std::process::id(),
            attempt
        ));
        let mut file = match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("create temporary config {temporary:?}: {error}")),
        };

        let result = (|| {
            file.write_all(&encoded)
                .map_err(|e| format!("write temporary config {temporary:?}: {e}"))?;
            file.sync_all()
                .map_err(|e| format!("sync temporary config {temporary:?}: {e}"))?;
            fs::rename(&temporary, path)
                .map_err(|e| format!("replace desktop configuration {path:?}: {e}"))
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result?;
        return Ok(url);
    }

    Err("could not create a unique temporary desktop configuration file".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::Path;

    #[test]
    fn reads_a_valid_https_user_api_url() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://api.example.test"}"#,
        )
        .unwrap();
        assert_eq!(
            read_user_api_url(&path),
            Some("https://api.example.test".into())
        );
    }

    #[test]
    fn missing_configuration_has_no_api_url() {
        assert_eq!(
            read_user_api_url(Path::new("/missing/desktop-config.json")),
            None
        );
    }

    #[test]
    fn non_current_version_has_no_api_url() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":2,"userApiUrl":"https://api.example.test"}"#,
        )
        .unwrap();

        assert_eq!(read_user_api_url(&path), None);
    }

    #[test]
    fn import_rejects_unknown_fields_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(
            &path,
            r#"{"version":1,"userApiUrl":"https://new.example.test","extra":true}"#,
        );

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn import_rejects_invalid_json_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(&path, "not json");

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn import_rejects_file_urls_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result =
            import_user_api_config(&path, r#"{"version":1,"userApiUrl":"file:///tmp/service"}"#);

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn import_rejects_non_current_version_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(
            &path,
            r#"{"version":2,"userApiUrl":"https://new.example.test"}"#,
        );

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn import_rejects_path_bearing_urls_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(
            &path,
            r#"{"version":1,"userApiUrl":"https://new.example.test/api"}"#,
        );

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn successful_import_persists_normalized_config_and_reads_it_back() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");

        let imported = import_user_api_config(
            &path,
            r#"{"version":1,"userApiUrl":"https://api.example.test/"}"#,
        )
        .unwrap();

        assert_eq!(imported, "https://api.example.test");
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            r#"{"version":1,"userApiUrl":"https://api.example.test"}"#
        );
        assert_eq!(
            read_user_api_url(&path),
            Some("https://api.example.test".into())
        );
    }

    #[test]
    fn import_rejects_credentials_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(
            &path,
            r#"{"version":1,"userApiUrl":"https://user:secret@new.example.test"}"#,
        );

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn import_rejects_query_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(
            &path,
            r#"{"version":1,"userApiUrl":"https://new.example.test?tenant=1"}"#,
        );

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }

    #[test]
    fn import_rejects_fragment_without_replacing_existing_config() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("desktop-config.json");
        fs::write(
            &path,
            r#"{"version":1,"userApiUrl":"https://old.example.test"}"#,
        )
        .unwrap();

        let result = import_user_api_config(
            &path,
            r#"{"version":1,"userApiUrl":"https://new.example.test#settings"}"#,
        );

        assert!(result.is_err());
        assert_eq!(
            read_user_api_url(&path),
            Some("https://old.example.test".into())
        );
    }
}
