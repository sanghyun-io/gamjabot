#![cfg(unix)]

use gamja_artifact::{
    ActionId, ArtifactError, ArtifactState, ArtifactStore, DeleteOutcome, VerifiedArtifact,
    MAX_BODY_BYTES,
};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::{
        fd::{AsRawFd, OwnedFd},
        unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const ACTION: &str = "6ba7b810-9dad-41d1-80b4-00c04fd430c8";

struct TestRoot(PathBuf);

impl TestRoot {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "gamja-artifact-storage-{label}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
        .try_into()
        .unwrap()
}

fn verified(width: u32, height: u32) -> VerifiedArtifact {
    VerifiedArtifact {
        room_id: 7,
        action_id: ActionId::parse(ACTION).unwrap(),
        event_seq: 13,
        screenshot_ref: format!("rooms/7/screenshots/{ACTION}.png"),
        width,
        height,
        created_at: "2026-10-02T01:02:03+00:00".into(),
    }
}

fn make_dir(path: &Path) {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}

fn png(width: u32, height: u32) -> Vec<u8> {
    let mut bytes = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut bytes, width, height);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder.write_header().unwrap();
        writer
            .write_image_data(&vec![0x7f; width as usize * height as usize * 4])
            .unwrap();
    }
    bytes
}

fn streaming_png(
    width: u32,
    height: u32,
    color: png::ColorType,
    depth: png::BitDepth,
    decoded_bytes: usize,
) -> Vec<u8> {
    let mut bytes = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut bytes, width, height);
        encoder.set_color(color);
        encoder.set_depth(depth);
        let mut writer = encoder.write_header().unwrap();
        {
            let mut stream = writer.stream_writer().unwrap();
            let zeros = [0u8; 64 * 1024];
            let mut remaining = decoded_bytes;
            while remaining > 0 {
                let count = remaining.min(zeros.len());
                stream.write_all(&zeros[..count]).unwrap();
                remaining -= count;
            }
            stream.finish().unwrap();
        }
        writer.finish().unwrap();
    }
    bytes
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for byte in bytes {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xedb8_8320u32 & 0u32.wrapping_sub(crc & 1));
        }
    }
    !crc
}

fn pad_png_to(mut bytes: Vec<u8>, size: usize) -> Vec<u8> {
    assert!(size >= bytes.len() + 13);
    let iend = bytes.split_off(bytes.len() - 12);
    let payload_len = size - bytes.len() - iend.len() - 12;
    bytes.extend_from_slice(&(payload_len as u32).to_be_bytes());
    bytes.extend_from_slice(b"vpAg");
    bytes.extend(std::iter::repeat(0x5a).take(payload_len));
    let chunk_start = bytes.len() - payload_len - 4;
    bytes.extend_from_slice(&crc32(&bytes[chunk_start..]).to_be_bytes());
    bytes.extend_from_slice(&iend);
    assert_eq!(bytes.len(), size);
    bytes
}

fn fixture(root: &Path, image: &[u8]) -> PathBuf {
    let directory = root.join("rooms/7/screenshots");
    make_dir(&directory);
    let path = directory.join(format!("{ACTION}.png"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .unwrap();
    file.write_all(image).unwrap();
    file.sync_all().unwrap();
    path
}

fn store(root: &Path) -> ArtifactStore {
    let root_file = File::open(root).unwrap();
    let root_fd: OwnedFd = root_file.into();
    ArtifactStore::from_app_data_fd(root_fd, now_ms() + 10_000).unwrap()
}

#[test]
fn storage_reads_a_valid_png_from_the_pinned_directory_fd() {
    let root = TestRoot::new("read");
    let image = png(2, 3);
    fixture(&root.0, &image);
    let artifact_store = store(&root.0);

    let moved = root.0.with_extension("moved");
    fs::rename(&root.0, &moved).unwrap();
    std::os::unix::fs::symlink("/tmp", &root.0).unwrap();

    let result = artifact_store.read(&verified(2, 3)).unwrap();
    assert_eq!(result.bytes, image);
    assert_eq!((result.width, result.height), (2, 3));
    assert_eq!(
        result.sha256,
        format!("{:x}", Sha256::digest(&result.bytes))
    );

    fs::remove_file(&root.0).unwrap();
    fs::rename(moved, &root.0).unwrap();
}

#[test]
fn storage_rejects_every_symlink_ancestor_and_the_leaf() {
    for component in ["rooms", "room", "screenshots", "leaf"] {
        let root = TestRoot::new(component);
        let outside = TestRoot::new("outside");
        let image = png(1, 1);
        fixture(&outside.0, &image);
        match component {
            "rooms" => std::os::unix::fs::symlink(outside.0.join("rooms"), root.0.join("rooms")),
            "room" => {
                make_dir(&root.0.join("rooms"));
                std::os::unix::fs::symlink(outside.0.join("rooms/7"), root.0.join("rooms/7"))
            }
            "screenshots" => {
                make_dir(&root.0.join("rooms/7"));
                std::os::unix::fs::symlink(
                    outside.0.join("rooms/7/screenshots"),
                    root.0.join("rooms/7/screenshots"),
                )
            }
            "leaf" => {
                make_dir(&root.0.join("rooms/7/screenshots"));
                std::os::unix::fs::symlink(
                    outside.0.join(format!("rooms/7/screenshots/{ACTION}.png")),
                    root.0.join(format!("rooms/7/screenshots/{ACTION}.png")),
                )
            }
            _ => unreachable!(),
        }
        .unwrap();
        assert_eq!(
            store(&root.0).read(&verified(1, 1)),
            Err(ArtifactError::ArtifactUnavailable),
            "{component}"
        );
    }
}

#[test]
fn storage_rejects_wrong_modes_types_and_hard_links() {
    let image = png(1, 1);

    let wrong_directory_mode = TestRoot::new("directory-mode");
    fixture(&wrong_directory_mode.0, &image);
    fs::set_permissions(
        wrong_directory_mode.0.join("rooms/7"),
        fs::Permissions::from_mode(0o750),
    )
    .unwrap();
    assert_eq!(
        store(&wrong_directory_mode.0).read(&verified(1, 1)),
        Err(ArtifactError::ArtifactUnavailable)
    );

    let wrong_file_mode = TestRoot::new("file-mode");
    let path = fixture(&wrong_file_mode.0, &image);
    fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    assert_eq!(
        store(&wrong_file_mode.0).read(&verified(1, 1)),
        Err(ArtifactError::ArtifactUnavailable)
    );

    let hard_link = TestRoot::new("hard-link");
    let path = fixture(&hard_link.0, &image);
    fs::hard_link(&path, hard_link.0.join("second-link")).unwrap();
    assert_eq!(
        store(&hard_link.0).read(&verified(1, 1)),
        Err(ArtifactError::ArtifactUnavailable)
    );

    let wrong_type = TestRoot::new("wrong-type");
    make_dir(&wrong_type.0.join("rooms/7/screenshots"));
    make_dir(
        &wrong_type
            .0
            .join(format!("rooms/7/screenshots/{ACTION}.png")),
    );
    assert_eq!(
        store(&wrong_type.0).read(&verified(1, 1)),
        Err(ArtifactError::ArtifactUnavailable)
    );
}

#[test]
fn storage_rejects_invalid_png_chunks_and_metadata_dimensions() {
    let valid = png(2, 3);
    for (label, image) in [
        ("signature", {
            let mut bytes = valid.clone();
            bytes[0] = 0;
            bytes
        }),
        ("crc", {
            let mut bytes = valid.clone();
            let index = bytes.len() - 1;
            bytes[index] ^= 1;
            bytes
        }),
        ("truncated", valid[..valid.len() - 1].to_vec()),
    ] {
        let root = TestRoot::new(label);
        fixture(&root.0, &image);
        assert_eq!(
            store(&root.0).read(&verified(2, 3)),
            Err(ArtifactError::ArtifactUnavailable),
            "{label}"
        );
    }

    let mismatch = TestRoot::new("dimensions");
    fixture(&mismatch.0, &valid);
    assert_eq!(
        store(&mismatch.0).read(&verified(3, 2)),
        Err(ArtifactError::ArtifactUnavailable)
    );
}

#[test]
fn storage_rejects_a_noncanonical_verified_reference_before_artifact_access() {
    let root = TestRoot::new("forged-proof");
    fixture(&root.0, &png(1, 1));
    let mut forged = verified(1, 1);
    forged.screenshot_ref = "rooms/7/screenshots/other.png".into();
    assert_eq!(
        store(&root.0).read(&forged),
        Err(ArtifactError::InvalidMetadata)
    );
}

#[test]
fn storage_inspection_checks_identity_without_decoding_png_bytes() {
    let root = TestRoot::new("metadata-only");
    let mut corrupt_pixels = png(1, 1);
    let last = corrupt_pixels.len() - 1;
    corrupt_pixels[last] ^= 1;
    fixture(&root.0, &corrupt_pixels);
    let artifact_store = store(&root.0);
    assert_eq!(
        artifact_store.inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Available
    );
    assert_eq!(
        artifact_store.read(&verified(1, 1)),
        Err(ArtifactError::ArtifactUnavailable)
    );
}

#[test]
fn storage_inspection_reports_missing_or_corrupt_headers_unavailable() {
    let missing = TestRoot::new("metadata-missing");
    assert_eq!(
        store(&missing.0).inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Unavailable
    );

    let corrupt = TestRoot::new("metadata-corrupt-header");
    fixture(&corrupt.0, b"not-a-png");
    assert_eq!(
        store(&corrupt.0).inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Unavailable
    );

    let dimensions = TestRoot::new("metadata-dimension-mismatch");
    fixture(&dimensions.0, &png(2, 3));
    assert_eq!(
        store(&dimensions.0).inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Unavailable
    );
}

#[test]
fn storage_accepts_exact_encoded_size_limit_and_rejects_one_more() {
    let exact = TestRoot::new("exact-size");
    fixture(&exact.0, &pad_png_to(png(1, 1), MAX_BODY_BYTES));
    assert_eq!(
        store(&exact.0).read(&verified(1, 1)).unwrap().bytes.len(),
        MAX_BODY_BYTES
    );

    let oversized = TestRoot::new("oversized");
    fixture(&oversized.0, &pad_png_to(png(1, 1), MAX_BODY_BYTES + 1));
    assert_eq!(
        store(&oversized.0).read(&verified(1, 1)),
        Err(ArtifactError::BodyTooLarge)
    );
}

#[test]
fn storage_accepts_exact_pixel_limit_and_rejects_one_over() {
    let exact = TestRoot::new("exact-pixels");
    let exact_image = streaming_png(
        5000,
        5000,
        png::ColorType::Grayscale,
        png::BitDepth::Eight,
        25_000_000,
    );
    fixture(&exact.0, &exact_image);
    assert_eq!(
        store(&exact.0).read(&verified(5000, 5000)).unwrap().bytes,
        exact_image
    );

    let over = TestRoot::new("pixels-plus-one");
    fixture(&over.0, &png(1, 1));
    assert_eq!(
        store(&over.0).read(&verified(25_000_001, 1)),
        Err(ArtifactError::InvalidMetadata)
    );
}

#[test]
fn storage_accepts_exact_decoded_byte_limit_and_rejects_one_sample_over() {
    let exact = TestRoot::new("exact-decoded-bytes");
    let exact_image = streaming_png(
        2500,
        5000,
        png::ColorType::Rgba,
        png::BitDepth::Sixteen,
        100_000_000,
    );
    fixture(&exact.0, &exact_image);
    assert!(store(&exact.0).read(&verified(2500, 5000)).is_ok());

    let over = TestRoot::new("decoded-bytes-over");
    let over_image = streaming_png(
        2500,
        5001,
        png::ColorType::Rgba,
        png::BitDepth::Sixteen,
        100_020_000,
    );
    fixture(&over.0, &over_image);
    assert_eq!(
        store(&over.0).read(&verified(2500, 5001)),
        Err(ArtifactError::ArtifactUnavailable)
    );
}

#[test]
fn storage_delete_durably_hides_and_idempotently_cleans_the_exact_artifact() {
    let root = TestRoot::new("delete");
    let image = png(2, 3);
    let path = fixture(&root.0, &image);
    let artifact_store = store(&root.0);

    assert_eq!(
        artifact_store.inspect(&verified(2, 3)).unwrap(),
        ArtifactState::Available
    );
    assert_eq!(
        artifact_store.delete(&verified(2, 3)).unwrap(),
        DeleteOutcome::Deleted
    );
    assert!(!path.exists());
    assert_eq!(
        artifact_store.inspect(&verified(2, 3)).unwrap(),
        ArtifactState::Removed
    );
    assert_eq!(
        artifact_store.read(&verified(2, 3)),
        Err(ArtifactError::ArtifactRemoved)
    );
    assert_eq!(
        store(&root.0).delete(&verified(2, 3)).unwrap(),
        DeleteOutcome::AlreadyDeleted
    );

    let tombstone = root.0.join(format!("artifacts/v1/deleted/7/{ACTION}.json"));
    let metadata = fs::metadata(&tombstone).unwrap();
    assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
    let record: serde_json::Value = serde_json::from_slice(&fs::read(tombstone).unwrap()).unwrap();
    assert_eq!(record["room_id"], 7);
    assert_eq!(record["action_id"], ACTION);
    assert_eq!(record["sha256"], format!("{:x}", Sha256::digest(image)));
    for directory in [
        root.0.join("artifacts"),
        root.0.join("artifacts/v1"),
        root.0.join("artifacts/v1/deleted"),
        root.0.join("artifacts/v1/deleted/7"),
        root.0.join("artifacts/v1/locks"),
        root.0.join("artifacts/v1/locks/7"),
    ] {
        assert_eq!(
            fs::metadata(directory).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }
    assert_eq!(
        fs::metadata(root.0.join(format!("artifacts/v1/locks/7/{ACTION}.lock")))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn storage_rejects_a_conflicting_tombstone_without_deleting_the_png() {
    let root = TestRoot::new("conflict");
    let path = fixture(&root.0, &png(1, 1));
    let artifact_store = store(&root.0);
    assert_eq!(
        artifact_store.delete(&verified(1, 1)).unwrap(),
        DeleteOutcome::Deleted
    );
    fixture(&root.0, &png(1, 1));
    let tombstone = root.0.join(format!("artifacts/v1/deleted/7/{ACTION}.json"));
    let mut record: serde_json::Value =
        serde_json::from_slice(&fs::read(&tombstone).unwrap()).unwrap();
    record["event_seq"] = 99.into();
    fs::write(&tombstone, serde_json::to_vec(&record).unwrap()).unwrap();
    fs::set_permissions(&tombstone, fs::Permissions::from_mode(0o600)).unwrap();

    assert_eq!(
        store(&root.0).delete(&verified(1, 1)),
        Err(ArtifactError::ArtifactConflict)
    );
    assert!(path.exists());
}

#[test]
fn storage_rechecks_the_deadline_after_waiting_for_the_cross_process_lock() {
    let root = TestRoot::new("deadline-lock");
    fixture(&root.0, &png(1, 1));
    let initializer = store(&root.0);
    assert_eq!(
        initializer.inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Available
    );
    let lock_path = root.0.join(format!("artifacts/v1/locks/7/{ACTION}.lock"));
    let ready = root.0.join("child-ready");
    let child = Command::new(std::env::current_exe().unwrap())
        .arg("--ignored")
        .arg("--exact")
        .arg("storage_cross_process_lock_holder_child")
        .env("GAMJA_TEST_LOCK_PATH", &lock_path)
        .env("GAMJA_TEST_READY_PATH", &ready)
        .env("GAMJA_TEST_LOCK_MS", "350")
        .env("GAMJA_TEST_LOCK_KIND", "exclusive")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while !ready.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert!(ready.exists());

    let expiring = {
        let root_fd: OwnedFd = File::open(&root.0).unwrap().into();
        ArtifactStore::from_app_data_fd(root_fd, now_ms() + 100).unwrap()
    };
    assert_eq!(
        expiring.read(&verified(1, 1)),
        Err(ArtifactError::DeadlineExpired)
    );
    let status = child.wait_with_output().unwrap().status;
    assert!(status.success());
}

#[test]
fn storage_lock_wait_ends_at_deadline_when_holder_never_releases() {
    let root = TestRoot::new("never-released-lock");
    fixture(&root.0, &png(1, 1));
    let initializer = store(&root.0);
    assert_eq!(
        initializer.inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Available
    );
    let lock_path = root.0.join(format!("artifacts/v1/locks/7/{ACTION}.lock"));
    let ready = root.0.join("never-released-ready");
    let mut child = Command::new(std::env::current_exe().unwrap())
        .arg("--ignored")
        .arg("--exact")
        .arg("storage_cross_process_lock_holder_child")
        .env("GAMJA_TEST_LOCK_PATH", &lock_path)
        .env("GAMJA_TEST_READY_PATH", &ready)
        .env("GAMJA_TEST_LOCK_MS", "60000")
        .env("GAMJA_TEST_LOCK_KIND", "exclusive")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let ready_deadline = Instant::now() + Duration::from_secs(2);
    while !ready.exists() && Instant::now() < ready_deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert!(ready.exists());
    let child_pid = child.id() as i32;
    let watchdog = thread::spawn(move || {
        thread::sleep(Duration::from_millis(700));
        unsafe { libc::kill(child_pid, libc::SIGKILL) };
    });

    let root_fd: OwnedFd = File::open(&root.0).unwrap().into();
    let expiring = ArtifactStore::from_app_data_fd(root_fd, now_ms() + 100).unwrap();
    let started = Instant::now();
    assert_eq!(
        expiring.read(&verified(1, 1)),
        Err(ArtifactError::DeadlineExpired)
    );
    let elapsed = started.elapsed();
    unsafe { libc::kill(child.id() as i32, libc::SIGKILL) };
    let _ = child.wait();
    watchdog.join().unwrap();
    assert!(elapsed < Duration::from_millis(400), "elapsed={elapsed:?}");
}

#[test]
fn storage_shared_gets_overlap_while_exclusive_delete_waits() {
    let root = TestRoot::new("shared-lock");
    fixture(&root.0, &png(1, 1));
    let artifact_store = store(&root.0);
    assert_eq!(
        artifact_store.inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Available
    );
    let lock_path = root.0.join(format!("artifacts/v1/locks/7/{ACTION}.lock"));
    let ready = root.0.join("shared-ready");
    let mut child = Command::new(std::env::current_exe().unwrap())
        .arg("--ignored")
        .arg("--exact")
        .arg("storage_cross_process_lock_holder_child")
        .env("GAMJA_TEST_LOCK_PATH", &lock_path)
        .env("GAMJA_TEST_READY_PATH", &ready)
        .env("GAMJA_TEST_LOCK_MS", "350")
        .env("GAMJA_TEST_LOCK_KIND", "shared")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while !ready.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert!(ready.exists());

    let started = Instant::now();
    artifact_store.read(&verified(1, 1)).unwrap();
    assert!(started.elapsed() < Duration::from_millis(200));

    let started = Instant::now();
    assert_eq!(
        artifact_store.delete(&verified(1, 1)).unwrap(),
        DeleteOutcome::Deleted
    );
    assert!(started.elapsed() >= Duration::from_millis(200));
    assert!(child.wait().unwrap().success());
}

#[test]
fn storage_rejects_lock_path_replacement_while_waiting() {
    let root = TestRoot::new("lock-replacement");
    fixture(&root.0, &png(1, 1));
    let artifact_store = store(&root.0);
    assert_eq!(
        artifact_store.inspect(&verified(1, 1)).unwrap(),
        ArtifactState::Available
    );
    let lock_path = root.0.join(format!("artifacts/v1/locks/7/{ACTION}.lock"));
    let ready = root.0.join("replacement-ready");
    let mut child = Command::new(std::env::current_exe().unwrap())
        .arg("--ignored")
        .arg("--exact")
        .arg("storage_cross_process_lock_holder_child")
        .env("GAMJA_TEST_LOCK_PATH", &lock_path)
        .env("GAMJA_TEST_READY_PATH", &ready)
        .env("GAMJA_TEST_LOCK_MS", "350")
        .env("GAMJA_TEST_LOCK_KIND", "exclusive")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    while !ready.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert!(ready.exists());

    let waiter = thread::spawn(move || artifact_store.read(&verified(1, 1)));
    thread::sleep(Duration::from_millis(50));
    fs::rename(&lock_path, root.0.join("replaced-lock")).unwrap();
    let replacement = OpenOptions::new()
        .create_new(true)
        .read(true)
        .write(true)
        .mode(0o600)
        .open(&lock_path)
        .unwrap();
    replacement.sync_all().unwrap();
    drop(replacement);

    assert!(child.wait().unwrap().success());
    assert_eq!(
        waiter.join().unwrap(),
        Err(ArtifactError::ArtifactUnavailable)
    );
}

#[test]
#[ignore]
fn storage_cross_process_lock_holder_child() {
    let path = PathBuf::from(std::env::var_os("GAMJA_TEST_LOCK_PATH").unwrap());
    let ready = PathBuf::from(std::env::var_os("GAMJA_TEST_READY_PATH").unwrap());
    let millis: u64 = std::env::var("GAMJA_TEST_LOCK_MS")
        .unwrap()
        .parse()
        .unwrap();
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(path)
        .unwrap();
    let operation = match std::env::var("GAMJA_TEST_LOCK_KIND").as_deref() {
        Ok("shared") => libc::LOCK_SH,
        _ => libc::LOCK_EX,
    };
    let result = unsafe { libc::flock(file.as_raw_fd(), operation) };
    assert_eq!(result, 0);
    fs::write(ready, b"ready").unwrap();
    thread::sleep(Duration::from_millis(millis));
}
