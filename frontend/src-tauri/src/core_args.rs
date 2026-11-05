//! Public instance selection for attach shells. No credentials are accepted.

use std::{
    ffi::OsString,
    fs,
    path::{Component, PathBuf},
};

// Windows and Linux receive a cold deep link as their sole CLI argument.
// It remains untrusted; the renderer and backend validate it again before use.
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn public_template_link(raw: &str) -> bool {
    if raw.len() > 4096 || !raw.starts_with("gamjabot://template?url=")
        || raw.bytes().any(|b| b <= b' ' || b == 0x7f || b == b'\\') { return false; }
    let Ok(outer) = tauri::Url::parse(raw) else { return false };
    if outer.scheme() != "gamjabot" || outer.host_str() != Some("template")
        || !outer.username().is_empty() || outer.password().is_some() || outer.port().is_some()
        || !outer.path().is_empty() || outer.fragment().is_some() { return false; }
    let pairs: Vec<_> = outer.query_pairs().collect();
    if pairs.len() != 1 || pairs[0].0 != "url" { return false; }
    let value = pairs[0].1.as_ref();
    if !value.starts_with("https://") || value.bytes().any(|b| b <= b' ' || b == 0x7f || b == b'\\') { return false; }
    let Ok(url) = tauri::Url::parse(value) else { return false };
    if url.as_str() != value || url.scheme() != "https" || !url.username().is_empty()
        || url.password().is_some() || url.port().is_some() || url.query().is_some() || url.fragment().is_some() { return false; }
    let Some(host) = url.host_str() else { return false };
    let labels: Vec<_> = host.split('.').collect();
    if host.len() > 253 || labels.len() < 2 || host.parse::<std::net::IpAddr>().is_ok()
        || matches!(labels.last().copied(), Some("localhost" | "local" | "internal" | "home"))
        || labels.iter().any(|label| label.is_empty() || label.len() > 63
            || label.starts_with('-') || label.ends_with('-')
            || !label.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')) { return false; }
    let Some(id) = url.path().strip_prefix("/t/") else { return false };
    let b = id.as_bytes();
    b.len() == 36 && [8,13,18,23].iter().all(|&i| b[i] == b'-')
        && b[14] == b'4' && matches!(b[19], b'8' | b'9' | b'a' | b'b')
        && b.iter().enumerate().all(|(i,&v)| [8,13,18,23].contains(&i) || v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
}

/// Parse only the shell's supported native arguments before creating a window.
/// The core's existing private-directory/endpoint validation remains mandatory.
pub fn app_data_override(
    arguments: impl Iterator<Item = OsString>,
    attach: bool,
) -> Result<Option<PathBuf>, &'static str> {
    let arguments: Vec<_> = arguments.collect();
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    if arguments.len() == 1 && arguments[0].to_str().is_some_and(public_template_link) {
        return Ok(None);
    }
    let mut arguments = arguments.into_iter();
    let mut selected = None;
    let mut skip_auto_restore = false;
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    let mut headless = false;
    while let Some(word) = arguments.next() {
        match word.to_str() {
            Some("--skip-auto-restore") if attach && !skip_auto_restore => skip_auto_restore = true,
            #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
            Some("--headless") if !attach && !headless => headless = true,
            Some("--app-data") => {
                if !attach {
                    return Err("app_data_requires_attach");
                }
                if selected.is_some() {
                    return Err("invalid_gui_arguments");
                }
                let path = PathBuf::from(arguments.next().ok_or("invalid_gui_arguments")?);
                if !path.is_absolute() || path.components().any(|part| part == Component::ParentDir)
                {
                    return Err("invalid_gui_arguments");
                }
                match fs::symlink_metadata(&path) {
                    Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                    _ => return Err("app_data_unavailable"),
                }
                selected = Some(path);
            }
            _ => return Err("invalid_gui_arguments"),
        }
    }
    Ok(selected)
}

/// Call only after app_data_override has validated the same full argument list.
pub fn skip_auto_restore(arguments: &[OsString]) -> bool {
    arguments.iter().any(|word| word == "--skip-auto-restore")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        path::PathBuf,
        sync::atomic::{AtomicUsize, Ordering},
        time::{SystemTime, UNIX_EPOCH},
    };

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let root = std::env::temp_dir().join(format!(
                "gamja-core-args-{}-{}-{}",
                std::process::id(),
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            Self(root)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn parse(words: &[&str], attach: bool) -> Result<Option<PathBuf>, &'static str> {
        app_data_override(words.iter().map(std::ffi::OsString::from), attach)
    }

    #[test]
    fn default_has_no_override_and_legacy_headless_is_preserved() {
        assert_eq!(parse(&[], true), Ok(None));
        assert_eq!(parse(&[], false), Ok(None));
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        assert_eq!(parse(&["--headless"], false), Ok(None));
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        assert_eq!(parse(&["--headless"], false), Err("invalid_gui_arguments"));
        assert!(parse(&["--headless"], true).is_err());
    }

    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    #[test]
    fn only_single_canonical_public_template_link_is_an_accepted_argument() {
        let valid = "gamjabot://template?url=https%3A%2F%2Fexample.com%2Ft%2F12345678-1234-4234-8234-123456789abc";
        assert_eq!(parse(&[valid], false), Ok(None));
        for invalid in [
            "gamjabot://template?url=https%3A%2F%2F127.0.0.1%2Ft%2F12345678-1234-4234-8234-123456789abc",
            "gamjabot://template?url=https%3A%2F%2Fexample.com%2Ft%2F12345678-1234-4234-8234-123456789abc&other=1",
            "gamjabot://template?url=https%3A%2F%2Fuser%40example.com%2Ft%2F12345678-1234-4234-8234-123456789abc",
            "gamjabot://other?url=https%3A%2F%2Fexample.com%2Ft%2F12345678-1234-4234-8234-123456789abc",
        ] { assert_eq!(parse(&[invalid], false), Err("invalid_gui_arguments")); }
        assert_eq!(parse(&[valid, "--headless"], false), Err("invalid_gui_arguments"));
    }

    #[test]
    fn safe_start_is_explicit_unique_and_attach_only() {
        let args = [OsString::from("--skip-auto-restore")];
        assert_eq!(app_data_override(args.clone().into_iter(), true), Ok(None));
        assert!(skip_auto_restore(&args));
        assert!(!skip_auto_restore(&[]));
        assert!(parse(&["--skip-auto-restore", "--skip-auto-restore"], true).is_err());
        assert!(parse(&["--skip-auto-restore"], false).is_err());
    }

    #[test]
    fn absolute_directory_selects_attach_instance_without_writing() {
        let f = Fixture::new();
        let path = f.0.to_str().unwrap();
        assert_eq!(parse(&["--app-data", path], true), Ok(Some(f.0.clone())));
        assert_eq!(parse(&["--app-data", path, "--skip-auto-restore"], true), Ok(Some(f.0.clone())));
        assert_eq!(fs::read_dir(&f.0).unwrap().count(), 0);
        assert!(parse(&["--app-data", path], false).is_err());
    }

    #[test]
    fn incomplete_duplicate_relative_and_secret_arguments_fail_closed() {
        for words in [
            vec!["--app-data"],
            vec!["--app-data", "relative"],
            vec!["--app-data", "/tmp/../tmp/unsafe"],
            vec!["--app-data", "/tmp", "--app-data", "/tmp"],
            vec!["--capability", "sentinel"],
            vec!["--session-token", "sentinel"],
            vec!["--jwt", "sentinel"],
            vec!["--unknown"],
        ] {
            assert!(parse(&words, true).is_err());
        }
    }

    #[test]
    fn files_and_symlink_directories_are_rejected() {
        let f = Fixture::new();
        let file = f.0.join("file");
        fs::write(&file, b"unchanged").unwrap();
        assert!(parse(&["--app-data", file.to_str().unwrap()], true).is_err());
        assert_eq!(fs::read(&file).unwrap(), b"unchanged");
        #[cfg(unix)]
        {
            let link = f.0.join("link");
            std::os::unix::fs::symlink(&f.0, &link).unwrap();
            assert!(parse(&["--app-data", link.to_str().unwrap()], true).is_err());
        }
    }
}
