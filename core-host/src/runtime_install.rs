//! Canonical Core runtime installation and receipt validation authority.
//!
//! Callers supply integrity pins. Release provenance and signature verification
//! remain separate package/operator responsibilities.

use std::path::{Path, PathBuf};

use crate::Result;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimePins {
    pub manifest_sha256: String,
    pub core_sha256: String,
    pub engine_sha256: String,
}

impl RuntimePins {
    pub fn new(
        manifest_sha256: impl Into<String>,
        core_sha256: impl Into<String>,
        engine_sha256: impl Into<String>,
    ) -> Result<Self> {
        let pins = Self {
            manifest_sha256: manifest_sha256.into(),
            core_sha256: core_sha256.into(),
            engine_sha256: engine_sha256.into(),
        };
        if [
            &pins.manifest_sha256,
            &pins.core_sha256,
            &pins.engine_sha256,
        ]
        .into_iter()
        .any(|value| value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)))
        {
            return Err("core_runtime_pin_invalid");
        }
        Ok(pins)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RuntimeScope {
    InstalledForCurrentUser,
    CandidatePrefix(PathBuf),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RuntimeStatus {
    Installed,
    AlreadyValid,
    Valid,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimeSummary {
    pub status: RuntimeStatus,
    pub pins: RuntimePins,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OwnedRuntimeIdentity {
    #[cfg(target_os = "macos")]
    inner: macos::RuntimeIdentity,
    #[cfg(not(target_os = "macos"))]
    _private: (),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ExpectedInstalled {
    Missing,
    Exact(OwnedRuntimeIdentity),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrokerUnavailableReason {
    BrokerUnwired,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BrokerAvailability {
    Available,
    Unavailable { reason: BrokerUnavailableReason },
}

pub fn broker_availability() -> BrokerAvailability {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    if let Ok(app_data) = crate::platform::app_data_dir() {
        if let Ok(status) = (crate::cli::CoreClient { app_data }).status() {
            return broker_availability_from_status(&status);
        }
    }
    BrokerAvailability::Unavailable {
        reason: BrokerUnavailableReason::BrokerUnwired,
    }
}

/// Safe public readiness only; this does not start the runtime or read a secret.
pub fn broker_availability_from_status(status: &serde_json::Value) -> BrokerAvailability {
    if status.get("status").and_then(serde_json::Value::as_str) == Some("healthy")
        && status.get("engine").and_then(serde_json::Value::as_str) == Some("ready")
        && status
            .get("credential_broker")
            .and_then(serde_json::Value::as_str)
            == Some("available")
    {
        BrokerAvailability::Available
    } else {
        BrokerAvailability::Unavailable {
            reason: BrokerUnavailableReason::BrokerUnwired,
        }
    }
}

pub fn installed_executable() -> Result<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        return Ok(macos::runtime_root(&macos::trusted_home()?).join("bin/gamja"));
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err("core_runtime_installer_unavailable")
    }
}

pub fn expected_installed() -> Result<ExpectedInstalled> {
    #[cfg(target_os = "macos")]
    {
        let root = macos::runtime_root(&macos::trusted_home()?);
        match std::fs::symlink_metadata(&root) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                Ok(ExpectedInstalled::Missing)
            }
            Err(_) => Err("core_runtime_invalid"),
            Ok(_) => macos::runtime_identity(&root).map(|inner| {
                ExpectedInstalled::Exact(OwnedRuntimeIdentity { inner })
            }),
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err("core_runtime_installer_unavailable")
    }
}

pub fn install_runtime(
    source: &Path,
    pins: &RuntimePins,
    expected: ExpectedInstalled,
) -> Result<RuntimeSummary> {
    #[cfg(target_os = "macos")]
    {
        return macos::install_runtime_at(&macos::trusted_home()?, source, pins, expected);
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (source, pins, expected);
        Err("core_runtime_installer_unavailable")
    }
}

pub fn validate_runtime(scope: RuntimeScope, pins: &RuntimePins) -> Result<RuntimeSummary> {
    #[cfg(target_os = "macos")]
    {
        let root = match scope {
            RuntimeScope::InstalledForCurrentUser => {
                macos::runtime_root(&macos::trusted_home()?)
            }
            RuntimeScope::CandidatePrefix(path) => path,
        };
        macos::validate_at(&root, pins)?;
        return Ok(RuntimeSummary {
            status: RuntimeStatus::Valid,
            pins: pins.clone(),
        });
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (scope, pins);
        Err("core_runtime_installer_unavailable")
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use super::{ExpectedInstalled, RuntimePins, RuntimeStatus, RuntimeSummary};
    use crate::Result;
    use serde::{Deserialize, Serialize};
    use sha2::{Digest, Sha256};
    use std::{
        collections::{BTreeMap, BTreeSet},
        ffi::{CStr, CString},
        fs::{self, OpenOptions},
        io::{Read, Write},
        os::{
            fd::AsRawFd,
            unix::{
                ffi::OsStrExt,
                fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
            },
        },
        path::{Component, Path, PathBuf},
    };

    const MANIFEST: &str = "core-manifest.json";
    const RECEIPT: &str = ".gamja-runtime-receipt.json";
    const MAX_MANIFEST_BYTES: u64 = 2 * 1024 * 1024;
    const MAX_RECEIPT_BYTES: u64 = 16 * 1024;

    #[derive(Deserialize)]
    struct Manifest {
        files: BTreeMap<String, ManifestEntry>,
    }

    #[derive(Deserialize)]
    struct ManifestEntry {
        bytes: Option<u64>,
        executable: Option<bool>,
        sha256: Option<String>,
        symlink: Option<String>,
    }

    #[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
    #[serde(deny_unknown_fields)]
    struct RuntimeReceiptV1 {
        version: u8,
        runtime_path: String,
        manifest_sha256: String,
        core_sha256: String,
        engine_sha256: String,
        core_device: u64,
        core_inode: u64,
        core_uid: u32,
        core_mode: u32,
        engine_device: u64,
        engine_inode: u64,
        engine_uid: u32,
        engine_mode: u32,
    }

    #[derive(Clone, Debug, PartialEq, Eq)]
    struct DirectoryIdentity {
        device: u64,
        inode: u64,
        uid: u32,
        mode: u32,
    }

    #[derive(Clone, Debug, PartialEq, Eq)]
    pub(super) struct RuntimeIdentity {
        directory: DirectoryIdentity,
        receipt: RuntimeReceiptV1,
    }

    #[derive(Clone, Copy)]
    enum Ownership {
        CurrentUser,
        RootOrCurrentUser,
    }

    impl Ownership {
        fn accepts(self, uid: u32) -> bool {
            uid == unsafe { libc::geteuid() }
                || matches!(self, Self::RootOrCurrentUser) && uid == 0
        }
    }

    pub(super) fn trusted_home() -> Result<PathBuf> {
        let uid = unsafe { libc::geteuid() };
        let mut record: libc::passwd = unsafe { std::mem::zeroed() };
        let mut result = std::ptr::null_mut();
        let mut buffer = vec![0u8; 16 * 1024];
        let status = unsafe {
            libc::getpwuid_r(
                uid,
                &mut record,
                buffer.as_mut_ptr().cast(),
                buffer.len(),
                &mut result,
            )
        };
        if status != 0 || result.is_null() || record.pw_dir.is_null() {
            return Err("core_runtime_home_unavailable");
        }
        let bytes = unsafe { CStr::from_ptr(record.pw_dir) }.to_bytes();
        if bytes.is_empty() || bytes.contains(&0) {
            return Err("core_runtime_home_unavailable");
        }
        Ok(PathBuf::from(std::ffi::OsStr::from_bytes(bytes)))
    }

    pub(super) fn runtime_root(home: &Path) -> PathBuf {
        home.join(".local/share/gamja-bot/core/runtime")
    }

    fn safe_relative(value: &str) -> Result<PathBuf> {
        let path = PathBuf::from(value);
        if path.as_os_str().is_empty()
            || path.is_absolute()
            || path
                .components()
                .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err("core_runtime_manifest_invalid");
        }
        Ok(path)
    }

    struct RegularSnapshot {
        device: u64,
        inode: u64,
        uid: u32,
        mode: u32,
        links: u64,
        bytes: u64,
        sha256: String,
    }

    fn snapshot_matches(metadata: &fs::Metadata, snapshot: &RegularSnapshot) -> bool {
        metadata.is_file()
            && !metadata.file_type().is_symlink()
            && metadata.dev() == snapshot.device
            && metadata.ino() == snapshot.inode
            && metadata.uid() == snapshot.uid
            && metadata.mode() == snapshot.mode
            && metadata.nlink() == snapshot.links
            && metadata.len() == snapshot.bytes
    }

    fn regular_snapshot_with_hook<F>(
        path: &Path,
        ownership: Ownership,
        exact_bytes: Option<u64>,
        hook: F,
    ) -> Result<RegularSnapshot>
    where
        F: FnOnce() -> Result<()>,
    {
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "core_runtime_invalid")?;
        let before = file.metadata().map_err(|_| "core_runtime_invalid")?;
        let path_before = fs::symlink_metadata(path).map_err(|_| "core_runtime_invalid")?;
        if !before.is_file()
            || before.file_type().is_symlink()
            || !ownership.accepts(before.uid())
            || before.nlink() != 1
            || before.mode() & 0o022 != 0
            || exact_bytes.is_some_and(|bytes| before.len() != bytes)
        {
            return Err("core_runtime_invalid");
        }
        let mut snapshot = RegularSnapshot {
            device: before.dev(),
            inode: before.ino(),
            uid: before.uid(),
            mode: before.mode(),
            links: before.nlink(),
            bytes: before.len(),
            sha256: String::new(),
        };
        if !snapshot_matches(&path_before, &snapshot) {
            return Err("core_runtime_invalid");
        }
        hook()?;
        let mut hash = Sha256::new();
        let mut block = [0u8; 64 * 1024];
        loop {
            let size = file.read(&mut block).map_err(|_| "core_runtime_invalid")?;
            if size == 0 {
                break;
            }
            hash.update(&block[..size]);
        }
        let after = file.metadata().map_err(|_| "core_runtime_invalid")?;
        let path_after = fs::symlink_metadata(path).map_err(|_| "core_runtime_invalid")?;
        if !snapshot_matches(&after, &snapshot) || !snapshot_matches(&path_after, &snapshot)
        {
            return Err("core_runtime_invalid");
        }
        snapshot.sha256 = format!("{:x}", hash.finalize());
        Ok(snapshot)
    }

    fn regular_snapshot(
        path: &Path,
        ownership: Ownership,
        exact_bytes: Option<u64>,
    ) -> Result<RegularSnapshot> {
        regular_snapshot_with_hook(path, ownership, exact_bytes, || Ok(()))
    }

    fn digest(path: &Path) -> Result<String> {
        Ok(regular_snapshot(path, Ownership::RootOrCurrentUser, None)?.sha256)
    }

    fn read_stable_bounded(
        path: &Path,
        ownership: Ownership,
        max_bytes: u64,
        exact_mode: Option<u32>,
    ) -> Result<Vec<u8>> {
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "core_runtime_invalid")?;
        let before = file.metadata().map_err(|_| "core_runtime_invalid")?;
        let path_before = fs::symlink_metadata(path).map_err(|_| "core_runtime_invalid")?;
        if !before.is_file()
            || !ownership.accepts(before.uid())
            || before.nlink() != 1
            || before.mode() & 0o022 != 0
            || exact_mode.is_some_and(|mode| before.mode() & 0o777 != mode)
            || before.len() > max_bytes
            || before.dev() != path_before.dev()
            || before.ino() != path_before.ino()
        {
            return Err("core_runtime_invalid");
        }
        let mut bytes = Vec::with_capacity(before.len() as usize);
        Read::by_ref(&mut file)
            .take(max_bytes + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "core_runtime_invalid")?;
        let after = file.metadata().map_err(|_| "core_runtime_invalid")?;
        let path_after = fs::symlink_metadata(path).map_err(|_| "core_runtime_invalid")?;
        if bytes.len() as u64 != before.len()
            || before.dev() != after.dev()
            || before.ino() != after.ino()
            || before.len() != after.len()
            || before.mode() != after.mode()
            || before.uid() != after.uid()
            || before.nlink() != after.nlink()
            || after.dev() != path_after.dev()
            || after.ino() != path_after.ino()
        {
            return Err("core_runtime_invalid");
        }
        Ok(bytes)
    }

    fn read_manifest(root: &Path, expected: &str, ownership: Ownership) -> Result<Manifest> {
        let bytes = read_stable_bounded(
            &root.join(MANIFEST),
            ownership,
            MAX_MANIFEST_BYTES,
            None,
        )?;
        if format!("{:x}", Sha256::digest(&bytes)) != expected {
            return Err("core_runtime_manifest_mismatch");
        }
        serde_json::from_slice(&bytes).map_err(|_| "core_runtime_manifest_invalid")
    }

    fn directory_identity(path: &Path, ownership: Ownership) -> Result<DirectoryIdentity> {
        let metadata = fs::symlink_metadata(path).map_err(|_| "core_runtime_invalid")?;
        if !metadata.is_dir()
            || metadata.file_type().is_symlink()
            || !ownership.accepts(metadata.uid())
            || metadata.mode() & 0o022 != 0
        {
            return Err("core_runtime_invalid");
        }
        Ok(DirectoryIdentity {
            device: metadata.dev(),
            inode: metadata.ino(),
            uid: metadata.uid(),
            mode: metadata.mode() & 0o777,
        })
    }

    fn secure_parent(home: &Path) -> Result<PathBuf> {
        directory_identity(home, Ownership::CurrentUser)?;
        let mut current = home.to_path_buf();
        for component in [".local", "share", "gamja-bot", "core"] {
            current.push(component);
            match fs::symlink_metadata(&current) {
                Ok(_) => {
                    directory_identity(&current, Ownership::CurrentUser)?;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    let mut builder = fs::DirBuilder::new();
                    builder.mode(0o700);
                    match builder.create(&current) {
                        Ok(()) => {}
                        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                        Err(_) => return Err("core_runtime_install_failed"),
                    }
                    directory_identity(&current, Ownership::CurrentUser)?;
                }
                Err(_) => return Err("core_runtime_install_failed"),
            }
        }
        Ok(current)
    }

    fn verify_file(path: &Path, entry: &ManifestEntry, ownership: Ownership) -> Result<()> {
        let bytes = entry.bytes.ok_or("core_runtime_manifest_invalid")?;
        let expected_hash = entry
            .sha256
            .as_deref()
            .ok_or("core_runtime_manifest_invalid")?;
        let executable = entry
            .executable
            .ok_or("core_runtime_manifest_invalid")?;
        let snapshot = regular_snapshot(path, ownership, Some(bytes))?;
        if snapshot.sha256 != expected_hash {
            return Err("core_runtime_invalid");
        }
        if executable != (snapshot.mode & 0o111 != 0) {
            return Err("core_runtime_invalid");
        }
        Ok(())
    }

    fn expected_paths(manifest: &Manifest, with_receipt: bool) -> Result<BTreeSet<PathBuf>> {
        let mut expected = BTreeSet::from([PathBuf::from(MANIFEST)]);
        if with_receipt {
            expected.insert(PathBuf::from(RECEIPT));
        }
        for name in manifest.files.keys() {
            let path = safe_relative(name)?;
            let mut parent = path.parent();
            while let Some(value) = parent {
                if value.as_os_str().is_empty() {
                    break;
                }
                expected.insert(value.to_path_buf());
                parent = value.parent();
            }
            expected.insert(path);
        }
        Ok(expected)
    }

    fn actual_paths(
        root: &Path,
        ownership: Ownership,
        expected: &BTreeSet<PathBuf>,
    ) -> Result<BTreeSet<PathBuf>> {
        let max_depth = expected
            .iter()
            .map(|path| path.components().count())
            .max()
            .unwrap_or(0);
        let max_path_bytes = expected
            .iter()
            .map(|path| path.as_os_str().as_bytes().len())
            .max()
            .unwrap_or(0);
        let max_total_path_bytes = expected
            .iter()
            .map(|path| path.as_os_str().as_bytes().len())
            .sum::<usize>();
        fn visit(
            root: &Path,
            current: &Path,
            output: &mut BTreeSet<PathBuf>,
            ownership: Ownership,
            expected: &BTreeSet<PathBuf>,
            max_depth: usize,
            max_path_bytes: usize,
            max_total_path_bytes: usize,
            total_path_bytes: &mut usize,
        ) -> Result<()> {
            for item in fs::read_dir(current).map_err(|_| "core_runtime_invalid")? {
                let item = item.map_err(|_| "core_runtime_invalid")?;
                let path = item.path();
                let relative = path
                    .strip_prefix(root)
                    .map_err(|_| "core_runtime_invalid")?
                    .to_path_buf();
                let path_bytes = relative.as_os_str().as_bytes().len();
                if !expected.contains(&relative)
                    || output.len() >= expected.len()
                    || relative.components().count() > max_depth
                    || path_bytes > max_path_bytes
                    || total_path_bytes
                        .checked_add(path_bytes)
                        .is_none_or(|total| total > max_total_path_bytes)
                {
                    return Err("core_runtime_inventory_mismatch");
                }
                *total_path_bytes += path_bytes;
                if !output.insert(relative) {
                    return Err("core_runtime_inventory_mismatch");
                }
                let metadata = fs::symlink_metadata(&path).map_err(|_| "core_runtime_invalid")?;
                if metadata.is_dir() && !metadata.file_type().is_symlink() {
                    if !ownership.accepts(metadata.uid()) || metadata.mode() & 0o022 != 0 {
                        return Err("core_runtime_invalid");
                    }
                    visit(
                        root,
                        &path,
                        output,
                        ownership,
                        expected,
                        max_depth,
                        max_path_bytes,
                        max_total_path_bytes,
                        total_path_bytes,
                    )?;
                }
            }
            Ok(())
        }
        let mut output = BTreeSet::new();
        let mut total_path_bytes = 0;
        visit(
            root,
            root,
            &mut output,
            ownership,
            expected,
            max_depth,
            max_path_bytes,
            max_total_path_bytes,
            &mut total_path_bytes,
        )?;
        Ok(output)
    }

    fn verify_package(
        root: &Path,
        pins: &RuntimePins,
        with_receipt: bool,
        ownership: Ownership,
    ) -> Result<Manifest> {
        directory_identity(root, ownership)?;
        let manifest = read_manifest(root, &pins.manifest_sha256, ownership)?;
        if digest(&root.join("bin/gamja"))? != pins.core_sha256
            || digest(&root.join("libexec/gamja/gamja-engine"))? != pins.engine_sha256
        {
            return Err("core_runtime_mismatch");
        }
        for (name, entry) in &manifest.files {
            let path = root.join(safe_relative(name)?);
            match (&entry.symlink, entry.bytes, &entry.sha256, entry.executable) {
                (Some(target), None, None, None) => {
                    let metadata =
                        fs::symlink_metadata(&path).map_err(|_| "core_runtime_invalid")?;
                    if !metadata.file_type().is_symlink()
                        || fs::read_link(&path).map_err(|_| "core_runtime_invalid")?
                            != PathBuf::from(target)
                    {
                        return Err("core_runtime_invalid");
                    }
                }
                (None, Some(_), Some(_), Some(_)) => verify_file(&path, entry, ownership)?,
                _ => return Err("core_runtime_manifest_invalid"),
            }
        }
        let expected = expected_paths(&manifest, with_receipt)?;
        if actual_paths(root, ownership, &expected)? != expected {
            return Err("core_runtime_inventory_mismatch");
        }
        Ok(manifest)
    }

    fn copy_package(source: &Path, destination: &Path, manifest: &Manifest) -> Result<()> {
        directory_identity(destination, Ownership::CurrentUser)
            .map_err(|_| "core_runtime_install_failed")?;
        fs::copy(source.join(MANIFEST), destination.join(MANIFEST))
            .map_err(|_| "core_runtime_install_failed")?;
        OpenOptions::new()
            .read(true)
            .open(destination.join(MANIFEST))
            .and_then(|file| file.sync_all())
            .map_err(|_| "core_runtime_install_failed")?;
        for (name, entry) in &manifest.files {
            let relative = safe_relative(name)?;
            let from = source.join(&relative);
            let to = destination.join(&relative);
            if let Some(parent) = to.parent() {
                fs::create_dir_all(parent).map_err(|_| "core_runtime_install_failed")?;
            }
            if let Some(target) = &entry.symlink {
                std::os::unix::fs::symlink(target, &to)
                    .map_err(|_| "core_runtime_install_failed")?;
            } else {
                fs::copy(&from, &to).map_err(|_| "core_runtime_install_failed")?;
                let mode = fs::metadata(&from)
                    .map_err(|_| "core_runtime_install_failed")?
                    .mode()
                    & 0o777;
                fs::set_permissions(&to, fs::Permissions::from_mode(mode))
                    .map_err(|_| "core_runtime_install_failed")?;
                OpenOptions::new()
                    .read(true)
                    .open(&to)
                    .and_then(|file| file.sync_all())
                    .map_err(|_| "core_runtime_install_failed")?;
            }
        }
        Ok(())
    }

    fn receipt(root: &Path, pins: &RuntimePins) -> Result<RuntimeReceiptV1> {
        let core = regular_snapshot(
            &root.join("bin/gamja"),
            Ownership::CurrentUser,
            None,
        )?;
        let engine = regular_snapshot(
            &root.join("libexec/gamja/gamja-engine"),
            Ownership::CurrentUser,
            None,
        )?;
        let core_mode = core.mode & 0o777;
        let engine_mode = engine.mode & 0o777;
        if !matches!(core_mode, 0o700 | 0o755) || engine_mode != 0o755 {
            return Err("core_runtime_invalid");
        }
        Ok(RuntimeReceiptV1 {
            version: 1,
            runtime_path: root.to_str().ok_or("core_runtime_invalid")?.to_owned(),
            manifest_sha256: pins.manifest_sha256.clone(),
            core_sha256: pins.core_sha256.clone(),
            engine_sha256: pins.engine_sha256.clone(),
            core_device: core.device,
            core_inode: core.inode,
            core_uid: core.uid,
            core_mode,
            engine_device: engine.device,
            engine_inode: engine.inode,
            engine_uid: engine.uid,
            engine_mode,
        })
    }

    fn write_receipt(root: &Path, value: &RuntimeReceiptV1) -> Result<()> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(root.join(RECEIPT))
            .map_err(|_| "core_runtime_install_failed")?;
        file.write_all(&serde_json::to_vec(value).map_err(|_| "core_runtime_install_failed")?)
            .and_then(|_| file.sync_all())
            .map_err(|_| "core_runtime_install_failed")
    }

    fn read_receipt(root: &Path) -> Result<RuntimeReceiptV1> {
        let bytes = read_stable_bounded(
            &root.join(RECEIPT),
            Ownership::CurrentUser,
            MAX_RECEIPT_BYTES,
            Some(0o600),
        )?;
        let receipt: RuntimeReceiptV1 =
            serde_json::from_slice(&bytes).map_err(|_| "core_runtime_invalid")?;
        if receipt.version != 1 {
            return Err("core_runtime_invalid");
        }
        Ok(receipt)
    }

    fn verify_receipt(root: &Path, expected: &RuntimeReceiptV1) -> Result<()> {
        if read_receipt(root)? != *expected {
            return Err("core_runtime_mismatch");
        }
        Ok(())
    }

    fn sync_directory(path: &Path) -> Result<()> {
        OpenOptions::new()
            .read(true)
            .open(path)
            .and_then(|file| file.sync_all())
            .map_err(|_| "core_runtime_install_failed")
    }

    fn sync_tree_directories(path: &Path) -> Result<()> {
        for item in fs::read_dir(path).map_err(|_| "core_runtime_install_failed")? {
            let item = item.map_err(|_| "core_runtime_install_failed")?;
            let metadata =
                fs::symlink_metadata(item.path()).map_err(|_| "core_runtime_install_failed")?;
            if metadata.is_dir() && !metadata.file_type().is_symlink() {
                sync_tree_directories(&item.path())?;
            }
        }
        sync_directory(path)
    }

    fn rename_exclusive(from: &Path, to: &Path) -> Result<()> {
        rename_with_flags(from, to, libc::RENAME_EXCL)
    }

    fn rename_swap(first: &Path, second: &Path) -> Result<()> {
        rename_with_flags(first, second, libc::RENAME_SWAP)
    }

    fn rename_with_flags(from: &Path, to: &Path, flags: u32) -> Result<()> {
        let from = CString::new(from.as_os_str().as_bytes())
            .map_err(|_| "core_runtime_install_failed")?;
        let to = CString::new(to.as_os_str().as_bytes())
            .map_err(|_| "core_runtime_install_failed")?;
        let status = unsafe {
            libc::renameatx_np(
                libc::AT_FDCWD,
                from.as_ptr(),
                libc::AT_FDCWD,
                to.as_ptr(),
                flags,
            )
        };
        if status != 0 {
            return Err("core_runtime_install_failed");
        }
        Ok(())
    }

    fn random_path(parent: &Path, prefix: &str) -> Result<PathBuf> {
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).map_err(|_| "core_runtime_install_failed")?;
        Ok(parent.join(format!(
            "{prefix}{}",
            random
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        )))
    }

    struct StageGuard {
        path: PathBuf,
        identity: DirectoryIdentity,
        active: bool,
    }

    impl StageGuard {
        fn create(parent: &Path) -> Result<Self> {
            for _ in 0..16 {
                let path = random_path(parent, ".runtime-stage-")?;
                let mut builder = fs::DirBuilder::new();
                builder.mode(0o700);
                match builder.create(&path) {
                    Ok(()) => {
                        let identity = directory_identity(&path, Ownership::CurrentUser)?;
                        if identity.mode != 0o700 {
                            let _ = fs::remove_dir(&path);
                            return Err("core_runtime_install_failed");
                        }
                        return Ok(Self {
                            path,
                            identity,
                            active: true,
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                    Err(_) => return Err("core_runtime_install_failed"),
                }
            }
            Err("core_runtime_install_failed")
        }

        fn refresh(&mut self) -> Result<()> {
            self.identity = directory_identity(&self.path, Ownership::CurrentUser)?;
            Ok(())
        }

        fn cleanup(&mut self) -> Result<()> {
            if !self.active {
                return Ok(());
            }
            if directory_identity(&self.path, Ownership::CurrentUser)? != self.identity {
                return Err("core_runtime_install_failed");
            }
            fs::remove_dir_all(&self.path).map_err(|_| "core_runtime_install_failed")?;
            self.active = false;
            Ok(())
        }

        fn published(&mut self) {
            self.active = false;
        }

        fn retain_as_rollback(&mut self, parent: &Path) -> Result<PathBuf> {
            self.refresh()?;
            for _ in 0..16 {
                let rollback = random_path(parent, ".runtime-rollback-")?;
                match rename_exclusive(&self.path, &rollback) {
                    Ok(()) => {
                        self.active = false;
                        return Ok(rollback);
                    }
                    Err(_) if fs::symlink_metadata(&rollback).is_ok() => continue,
                    Err(_) => {
                        self.active = false;
                        return Err("core_runtime_publish_uncertain");
                    }
                }
            }
            self.active = false;
            Err("core_runtime_publish_uncertain")
        }
    }

    impl Drop for StageGuard {
        fn drop(&mut self) {
            let _ = self.cleanup();
        }
    }

    pub(super) fn validate_at(root: &Path, pins: &RuntimePins) -> Result<()> {
        verify_package(root, pins, true, Ownership::CurrentUser)?;
        verify_receipt(root, &receipt(root, pins)?)
    }

    fn runtime_identity_at(root: &Path, receipt_root: &Path) -> Result<RuntimeIdentity> {
        let actual = read_receipt(root)?;
        let pins = RuntimePins::new(
            actual.manifest_sha256.clone(),
            actual.core_sha256.clone(),
            actual.engine_sha256.clone(),
        )?;
        verify_package(root, &pins, true, Ownership::CurrentUser)?;
        let mut expected = receipt(root, &pins)?;
        expected.runtime_path = receipt_root
            .to_str()
            .ok_or("core_runtime_invalid")?
            .to_owned();
        verify_receipt(root, &expected)?;
        Ok(RuntimeIdentity {
            directory: directory_identity(root, Ownership::CurrentUser)?,
            receipt: actual,
        })
    }

    pub(super) fn runtime_identity(root: &Path) -> Result<RuntimeIdentity> {
        runtime_identity_at(root, root)
    }

    fn same_current(root: &Path, expected: &RuntimeIdentity) -> Result<()> {
        if runtime_identity(root)? != *expected {
            return Err("core_runtime_current_changed");
        }
        Ok(())
    }

    fn restore_swapped_pair(
        stage: &Path,
        runtime: &Path,
        parent: &Path,
        staged: &RuntimeIdentity,
        moved: &RuntimeIdentity,
    ) -> Result<()> {
        if runtime_identity_at(runtime, runtime)? != *staged
            || runtime_identity_at(stage, runtime)? != *moved
        {
            return Err("core_runtime_rollback_uncertain");
        }
        rename_swap(stage, runtime).map_err(|_| "core_runtime_rollback_uncertain")?;
        sync_directory(parent).map_err(|_| "core_runtime_rollback_uncertain")?;
        if runtime_identity_at(runtime, runtime)? != *moved
            || runtime_identity_at(stage, runtime)? != *staged
        {
            return Err("core_runtime_rollback_uncertain");
        }
        Ok(())
    }

    pub(super) fn install_runtime_at(
        home: &Path,
        source: &Path,
        pins: &RuntimePins,
        expected: ExpectedInstalled,
    ) -> Result<RuntimeSummary> {
        install_runtime_at_with_hook(home, source, pins, expected, |_, _| Ok(()))
    }

    fn install_runtime_at_with_hook<F>(
        home: &Path,
        source: &Path,
        pins: &RuntimePins,
        expected: ExpectedInstalled,
        mut hook: F,
    ) -> Result<RuntimeSummary>
    where
        F: FnMut(&str, &Path) -> Result<()>,
    {
        let parent = secure_parent(home)?;
        let runtime = parent.join("runtime");
        let lock_path = parent.join(".runtime-install.lock");
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&lock_path)
            .map_err(|_| "core_runtime_install_failed")?;
        let lock_metadata = lock.metadata().map_err(|_| "core_runtime_install_failed")?;
        let lock_path_metadata =
            fs::symlink_metadata(&lock_path).map_err(|_| "core_runtime_install_failed")?;
        if !lock_metadata.is_file()
            || lock_metadata.uid() != unsafe { libc::geteuid() }
            || lock_metadata.nlink() != 1
            || lock_metadata.mode() & 0o777 != 0o600
            || lock_metadata.dev() != lock_path_metadata.dev()
            || lock_metadata.ino() != lock_path_metadata.ino()
        {
            return Err("core_runtime_install_failed");
        }
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err("core_runtime_install_failed");
        }

        let current = fs::symlink_metadata(&runtime);
        match (&expected, current) {
            (ExpectedInstalled::Missing, Err(error))
                if error.kind() == std::io::ErrorKind::NotFound => {}
            (ExpectedInstalled::Missing, Ok(_)) => {
                if validate_at(&runtime, pins).is_ok() {
                    return Ok(RuntimeSummary {
                        status: RuntimeStatus::AlreadyValid,
                        pins: pins.clone(),
                    });
                }
                return Err("core_runtime_current_changed");
            }
            (ExpectedInstalled::Missing, Err(_)) => return Err("core_runtime_invalid"),
            (ExpectedInstalled::Exact(expected), Ok(_)) => {
                same_current(&runtime, &expected.inner)?;
                if expected.inner.receipt.manifest_sha256 == pins.manifest_sha256
                    && expected.inner.receipt.core_sha256 == pins.core_sha256
                    && expected.inner.receipt.engine_sha256 == pins.engine_sha256
                {
                    validate_at(&runtime, pins)?;
                    return Ok(RuntimeSummary {
                        status: RuntimeStatus::AlreadyValid,
                        pins: pins.clone(),
                    });
                }
            }
            (ExpectedInstalled::Exact(_), _) => return Err("core_runtime_current_changed"),
        }

        let source_manifest =
            verify_package(source, pins, false, Ownership::RootOrCurrentUser)?;
        let mut stage = StageGuard::create(&parent)?;
        let result = (|| {
            hook("stage_created", &stage.path)?;
            copy_package(source, &stage.path, &source_manifest)?;
            hook("copy_complete", &stage.path)?;
            let mut staged_receipt = receipt(&stage.path, pins)?;
            staged_receipt.runtime_path = runtime
                .to_str()
                .ok_or("core_runtime_install_failed")?
                .to_owned();
            write_receipt(&stage.path, &staged_receipt)?;
            hook("receipt_complete", &stage.path)?;
            verify_package(&stage.path, pins, true, Ownership::CurrentUser)?;
            verify_receipt(&stage.path, &staged_receipt)?;
            sync_tree_directories(&stage.path)?;
            hook("fsync_complete", &stage.path)?;

            match &expected {
                ExpectedInstalled::Missing => {
                    hook("before_publish", &stage.path)?;
                    rename_exclusive(&stage.path, &runtime)?;
                    stage.published();
                    sync_directory(&parent)?;
                    validate_at(&runtime, pins)?;
                }
                ExpectedInstalled::Exact(expected) => {
                    let staged_identity = runtime_identity_at(&stage.path, &runtime)?;
                    same_current(&runtime, &expected.inner)?;
                    hook("before_publish", &stage.path)?;
                    rename_swap(&stage.path, &runtime)?;
                    if sync_directory(&parent).is_err() {
                        stage.published();
                        return Err("core_runtime_rollback_uncertain");
                    }
                    let moved_identity = match runtime_identity_at(&stage.path, &runtime) {
                        Ok(identity) => identity,
                        Err(_) => {
                            stage.published();
                            return Err("core_runtime_rollback_uncertain");
                        }
                    };
                    if moved_identity != expected.inner {
                        if hook("before_competing_restore", &stage.path).is_err() {
                            stage.published();
                            return Err("core_runtime_rollback_uncertain");
                        }
                        if restore_swapped_pair(
                            &stage.path,
                            &runtime,
                            &parent,
                            &staged_identity,
                            &moved_identity,
                        )
                        .is_err()
                        {
                            stage.published();
                            return Err("core_runtime_rollback_uncertain");
                        }
                        stage.refresh()?;
                        stage.cleanup()?;
                        return Err("core_runtime_current_changed");
                    }
                    let published = hook("after_publish", &runtime)
                        .and_then(|_| validate_at(&runtime, pins));
                    if let Err(error) = published {
                        if restore_swapped_pair(
                            &stage.path,
                            &runtime,
                            &parent,
                            &staged_identity,
                            &moved_identity,
                        )
                        .is_err()
                        {
                            stage.published();
                            return Err("core_runtime_rollback_uncertain");
                        }
                        stage.refresh()?;
                        stage.cleanup()?;
                        return Err(error);
                    }
                    if runtime_identity_at(&stage.path, &runtime).ok().as_ref()
                        != Some(&moved_identity)
                    {
                        stage.published();
                        return Err("core_runtime_rollback_uncertain");
                    }
                    stage.retain_as_rollback(&parent)?;
                    sync_directory(&parent)?;
                }
            }
            Ok(RuntimeSummary {
                status: RuntimeStatus::Installed,
                pins: pins.clone(),
            })
        })();
        if result.is_err() && stage.active {
            stage.cleanup()?;
        }
        result
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use serde_json::json;
        use std::sync::atomic::{AtomicUsize, Ordering};

        static NEXT: AtomicUsize = AtomicUsize::new(0);

        struct Fixture {
            outer: PathBuf,
            home: PathBuf,
            source: PathBuf,
            pins: RuntimePins,
        }

        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.outer);
            }
        }

        fn fixture(core_bytes: &[u8], engine_bytes: &[u8]) -> Fixture {
            let outer = std::env::temp_dir().join(format!(
                "gamja-runtime-install-unit-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            let home = outer.join("home");
            let source = outer.join("source");
            fs::create_dir_all(source.join("bin")).unwrap();
            fs::create_dir_all(source.join("libexec/gamja")).unwrap();
            fs::create_dir(&home).unwrap();
            for path in [&outer, &home, &source] {
                fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
            }
            let core = source.join("bin/gamja");
            let engine = source.join("libexec/gamja/gamja-engine");
            fs::write(&core, core_bytes).unwrap();
            fs::write(&engine, engine_bytes).unwrap();
            fs::set_permissions(&core, fs::Permissions::from_mode(0o700)).unwrap();
            fs::set_permissions(&engine, fs::Permissions::from_mode(0o755)).unwrap();
            let manifest = json!({"files": {
                "bin/gamja": {"bytes": core_bytes.len(), "executable": true, "sha256": digest(&core).unwrap()},
                "libexec/gamja/gamja-engine": {"bytes": engine_bytes.len(), "executable": true, "sha256": digest(&engine).unwrap()}
            }});
            let manifest_path = source.join(MANIFEST);
            fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
            fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o600)).unwrap();
            let pins = RuntimePins::new(
                digest(&manifest_path).unwrap(),
                digest(&core).unwrap(),
                digest(&engine).unwrap(),
            )
            .unwrap();
            Fixture {
                outer,
                home,
                source,
                pins,
            }
        }

        fn no_stage(parent: &Path) -> bool {
            !parent.exists()
                || fs::read_dir(parent).unwrap().all(|entry| {
                    !entry
                        .unwrap()
                        .file_name()
                        .to_string_lossy()
                        .starts_with(".runtime-stage-")
                })
        }

        fn prepare_competing_runtime(
            fixture: &Fixture,
            destination: &Path,
            canonical_runtime: &Path,
        ) -> DirectoryIdentity {
            fs::create_dir(destination).unwrap();
            fs::set_permissions(destination, fs::Permissions::from_mode(0o700)).unwrap();
            let manifest = verify_package(
                &fixture.source,
                &fixture.pins,
                false,
                Ownership::RootOrCurrentUser,
            )
            .unwrap();
            copy_package(&fixture.source, destination, &manifest).unwrap();
            let mut value = receipt(destination, &fixture.pins).unwrap();
            value.runtime_path = canonical_runtime.to_str().unwrap().to_owned();
            write_receipt(destination, &value).unwrap();
            directory_identity(destination, Ownership::CurrentUser).unwrap()
        }

        #[test]
        fn absent_install_is_private_bound_and_reused() {
            let fixture = fixture(b"core-one", b"engine-one");
            let first = install_runtime_at(
                &fixture.home,
                &fixture.source,
                &fixture.pins,
                ExpectedInstalled::Missing,
            )
            .unwrap();
            assert_eq!(first.status, RuntimeStatus::Installed);
            let root = runtime_root(&fixture.home);
            validate_at(&root, &fixture.pins).unwrap();
            let expected = ExpectedInstalled::Exact(super::super::OwnedRuntimeIdentity {
                inner: runtime_identity(&root).unwrap(),
            });
            let second =
                install_runtime_at(&fixture.home, &fixture.source, &fixture.pins, expected).unwrap();
            assert_eq!(second.status, RuntimeStatus::AlreadyValid);
            assert!(no_stage(&root.parent().unwrap()));
        }

        #[test]
        fn exact_update_retains_old_runtime_and_rolls_back_failed_publish() {
            let old = fixture(b"core-old", b"engine-old");
            install_runtime_at(
                &old.home,
                &old.source,
                &old.pins,
                ExpectedInstalled::Missing,
            )
            .unwrap();
            let root = runtime_root(&old.home);
            let old_identity = runtime_identity(&root).unwrap();

            let new = fixture(b"core-new", b"engine-new");
            let updated = install_runtime_at(
                &old.home,
                &new.source,
                &new.pins,
                ExpectedInstalled::Exact(super::super::OwnedRuntimeIdentity {
                    inner: old_identity,
                }),
            )
            .unwrap();
            assert_eq!(updated.status, RuntimeStatus::Installed);
            validate_at(&root, &new.pins).unwrap();
            assert!(fs::read_dir(root.parent().unwrap()).unwrap().any(|entry| entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".runtime-rollback-")));

            let current = runtime_identity(&root).unwrap();
            let third = fixture(b"core-third", b"engine-third");
            let error = install_runtime_at_with_hook(
                &old.home,
                &third.source,
                &third.pins,
                ExpectedInstalled::Exact(super::super::OwnedRuntimeIdentity { inner: current }),
                |phase, _| {
                    if phase == "after_publish" {
                        Err("injected_after_publish")
                    } else {
                        Ok(())
                    }
                },
            )
            .unwrap_err();
            assert_eq!(error, "injected_after_publish");
            validate_at(&root, &new.pins).unwrap();
            assert!(no_stage(root.parent().unwrap()));
        }

        #[test]
        fn before_publish_winner_is_restored_instead_of_overwritten() {
            let old = fixture(b"core-old", b"engine-old");
            install_runtime_at(
                &old.home,
                &old.source,
                &old.pins,
                ExpectedInstalled::Missing,
            )
            .unwrap();
            let root = runtime_root(&old.home);
            let expected = runtime_identity(&root).unwrap();

            let winner = fixture(b"core-winner", b"engine-winner");
            let parent = root.parent().unwrap();
            let prepared_winner = parent.join(".prepared-winner");
            let winner_directory = prepare_competing_runtime(&winner, &prepared_winner, &root);
            let displaced = parent.join(".displaced-expected");

            let update = fixture(b"core-update", b"engine-update");
            let error = install_runtime_at_with_hook(
                &old.home,
                &update.source,
                &update.pins,
                ExpectedInstalled::Exact(super::super::OwnedRuntimeIdentity { inner: expected }),
                |phase, _| {
                    if phase == "before_publish" {
                        fs::rename(&root, &displaced).unwrap();
                        fs::rename(&prepared_winner, &root).unwrap();
                    }
                    Ok(())
                },
            )
            .unwrap_err();

            assert_eq!(error, "core_runtime_current_changed");
            assert_eq!(
                directory_identity(&root, Ownership::CurrentUser).unwrap(),
                winner_directory
            );
            validate_at(&root, &winner.pins).unwrap();
            assert!(no_stage(parent));
        }

        #[test]
        fn failed_competing_winner_restore_is_uncertain_and_preserves_winner() {
            let old = fixture(b"core-old", b"engine-old");
            install_runtime_at(
                &old.home,
                &old.source,
                &old.pins,
                ExpectedInstalled::Missing,
            )
            .unwrap();
            let root = runtime_root(&old.home);
            let expected = runtime_identity(&root).unwrap();
            let parent = root.parent().unwrap();

            let winner = fixture(b"core-winner", b"engine-winner");
            let prepared_winner = parent.join(".prepared-winner-uncertain");
            prepare_competing_runtime(&winner, &prepared_winner, &root);
            let displaced = parent.join(".displaced-expected-uncertain");
            let mut retained_winner = None;

            let update = fixture(b"core-update", b"engine-update");
            let error = install_runtime_at_with_hook(
                &old.home,
                &update.source,
                &update.pins,
                ExpectedInstalled::Exact(super::super::OwnedRuntimeIdentity { inner: expected }),
                |phase, stage| {
                    if phase == "before_publish" {
                        fs::rename(&root, &displaced).unwrap();
                        fs::rename(&prepared_winner, &root).unwrap();
                    } else if phase == "before_competing_restore" {
                        retained_winner = Some(stage.to_path_buf());
                        fs::remove_dir_all(&root).unwrap();
                    }
                    Ok(())
                },
            )
            .unwrap_err();

            assert_eq!(error, "core_runtime_rollback_uncertain");
            let retained_winner = retained_winner.unwrap();
            assert!(retained_winner.is_dir());
            assert!(runtime_identity_at(&retained_winner, &root).is_ok());
        }

        #[test]
        fn unexpected_deep_tree_is_rejected_before_descending() {
            let fixture = fixture(b"core-one", b"engine-one");
            let unexpected = fixture.source.join("unexpected");
            fs::create_dir(&unexpected).unwrap();
            fs::set_permissions(&unexpected, fs::Permissions::from_mode(0o700)).unwrap();
            let mut unsafe_nested = unexpected.clone();
            for depth in 0..64 {
                unsafe_nested.push(format!("deep-{depth}"));
                fs::create_dir(&unsafe_nested).unwrap();
                fs::set_permissions(
                    &unsafe_nested,
                    fs::Permissions::from_mode(if depth == 63 { 0o777 } else { 0o700 }),
                )
                .unwrap();
            }

            let error = match verify_package(
                &fixture.source,
                &fixture.pins,
                false,
                Ownership::RootOrCurrentUser,
            ) {
                Err(error) => error,
                Ok(_) => panic!("unexpected tree was accepted"),
            };
            assert_eq!(error, "core_runtime_inventory_mismatch");
        }

        #[test]
        fn stable_regular_snapshot_rejects_hardlink_replacement_before_hash() {
            let fixture = fixture(b"core-one", b"engine-one");
            let path = fixture.source.join("bin/gamja");
            let hardlink_source = fixture.outer.join("hardlink-source");
            let hardlink_alias = fixture.outer.join("hardlink-alias");
            fs::write(&hardlink_source, b"core-one").unwrap();
            fs::set_permissions(
                &hardlink_source,
                fs::Permissions::from_mode(0o700),
            )
            .unwrap();
            fs::hard_link(&hardlink_source, &hardlink_alias).unwrap();

            let error = match regular_snapshot_with_hook(
                &path,
                Ownership::RootOrCurrentUser,
                Some(8),
                || {
                    fs::remove_file(&path).unwrap();
                    fs::rename(&hardlink_alias, &path).unwrap();
                    Ok(())
                },
            ) {
                Err(error) => error,
                Ok(_) => panic!("hardlink replacement was accepted"),
            };
            assert_eq!(error, "core_runtime_invalid");
        }
    }
}
