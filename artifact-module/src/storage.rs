use crate::{ArtifactError, VerifiedArtifact, MAX_BODY_BYTES, MAX_PIXELS};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    ffi::{CStr, CString},
    fs::File,
    io::{Cursor, Read, Seek, SeekFrom, Write},
    mem::MaybeUninit,
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
    thread,
    time::{Duration, Instant},
    time::{SystemTime, UNIX_EPOCH},
};

const DIRECTORY_MODE: libc::mode_t = 0o700;
const FILE_MODE: libc::mode_t = 0o600;
const MAX_TOMBSTONE_BYTES: usize = 4096;
const MAX_DECODED_BYTES: usize = MAX_PIXELS as usize * 4;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct VerifiedPng {
    pub bytes: Vec<u8>,
    pub width: u32,
    pub height: u32,
    pub sha256: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeleteOutcome {
    Deleted,
    AlreadyDeleted,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ArtifactState {
    Available,
    Removed,
    Unavailable,
}

pub struct ArtifactStore {
    root: OwnedFd,
    deadline_ms: u64,
    hooks: Hooks,
}

impl ArtifactStore {
    pub fn from_app_data_fd(root: OwnedFd, deadline_ms: u64) -> Result<Self, ArtifactError> {
        validate_directory(root.as_raw_fd())?;
        Ok(Self {
            root,
            deadline_ms,
            hooks: Hooks::none(),
        })
    }

    #[cfg(test)]
    fn with_fault(
        root: OwnedFd,
        deadline_ms: u64,
        point: FaultPoint,
    ) -> Result<Self, ArtifactError> {
        validate_directory(root.as_raw_fd())?;
        Ok(Self {
            root,
            deadline_ms,
            hooks: Hooks::fail_once(point),
        })
    }

    #[cfg(test)]
    fn with_pending_publish_swap(root: OwnedFd, deadline_ms: u64) -> Result<Self, ArtifactError> {
        validate_directory(root.as_raw_fd())?;
        Ok(Self {
            root,
            deadline_ms,
            hooks: Hooks::swap_pending_once(),
        })
    }

    pub fn read(&self, verified: &VerifiedArtifact) -> Result<VerifiedPng, ArtifactError> {
        validate_verified(verified)?;
        let _lock = self.lock(verified, LockKind::Shared)?;
        self.recheck_deadline()?;
        match self.tombstone(verified)? {
            TombstoneState::Absent => {}
            TombstoneState::Exact(_) => return Err(ArtifactError::ArtifactRemoved),
            TombstoneState::Conflict => return Err(ArtifactError::ArtifactConflict),
        }
        let opened = self.open_png(verified)?;
        validate_png(opened.bytes, verified)
    }

    pub fn inspect(&self, verified: &VerifiedArtifact) -> Result<ArtifactState, ArtifactError> {
        validate_verified(verified)?;
        let _lock = self.lock(verified, LockKind::Shared)?;
        self.recheck_deadline()?;
        match self.tombstone(verified) {
            Ok(TombstoneState::Exact(_)) => return Ok(ArtifactState::Removed),
            Ok(TombstoneState::Absent) => {}
            Ok(TombstoneState::Conflict) | Err(ArtifactError::ArtifactUnavailable) => {
                return Ok(ArtifactState::Unavailable)
            }
            Err(error) => return Err(error),
        }
        match self.open_png_metadata(verified) {
            Ok(_) => Ok(ArtifactState::Available),
            Err(ArtifactError::ArtifactUnavailable)
            | Err(ArtifactError::BodyTooLarge)
            | Err(ArtifactError::InvalidMetadata) => {
                Ok(ArtifactState::Unavailable)
            }
            Err(error) => Err(error),
        }
    }

    pub fn delete(&self, verified: &VerifiedArtifact) -> Result<DeleteOutcome, ArtifactError> {
        validate_verified(verified)?;
        let _lock = self.lock(verified, LockKind::Exclusive)?;
        self.recheck_deadline()?;
        match self.tombstone(verified)? {
            TombstoneState::Exact(record) => {
                self.sync_tombstone_directory(verified)?;
                self.cleanup_recorded_png(verified, &record)?;
                Ok(DeleteOutcome::AlreadyDeleted)
            }
            TombstoneState::Conflict => Err(ArtifactError::ArtifactConflict),
            TombstoneState::Absent => {
                let opened = self.open_png(verified)?;
                let decoded = validate_png(opened.bytes, verified)?;
                let record = Tombstone::new(verified, &opened.identity, decoded.sha256)?;
                let record = self.publish_tombstone(verified, record)?;
                self.cleanup_recorded_png(verified, &record)?;
                Ok(DeleteOutcome::Deleted)
            }
        }
    }

    fn recheck_deadline(&self) -> Result<(), ArtifactError> {
        let now = unix_ms()?;
        if self.deadline_ms <= now {
            Err(ArtifactError::DeadlineExpired)
        } else {
            Ok(())
        }
    }

    fn lock(
        &self,
        verified: &VerifiedArtifact,
        kind: LockKind,
    ) -> Result<ArtifactLock, ArtifactError> {
        let artifacts = ensure_directory_at(self.root.as_raw_fd(), c("artifacts")?.as_c_str())?;
        let version = ensure_directory_at(artifacts.as_raw_fd(), c("v1")?.as_c_str())?;
        let locks = ensure_directory_at(version.as_raw_fd(), c("locks")?.as_c_str())?;
        let room = ensure_directory_at(
            locks.as_raw_fd(),
            c(&verified.room_id.to_string())?.as_c_str(),
        )?;
        let name = c(&format!("{}.lock", verified.action_id.as_str()))?;
        let fd = open_owner_file(room.as_raw_fd(), &name, true, true)?;
        let operation = match kind {
            LockKind::Shared => libc::LOCK_SH,
            LockKind::Exclusive => libc::LOCK_EX,
        };
        let remaining_ms = self
            .deadline_ms
            .checked_sub(unix_ms()?)
            .filter(|remaining| *remaining > 0)
            .ok_or(ArtifactError::DeadlineExpired)?;
        let wait_until = Instant::now()
            .checked_add(Duration::from_millis(remaining_ms))
            .ok_or(ArtifactError::DeadlineExpired)?;
        loop {
            if unsafe { libc::flock(fd.as_raw_fd(), operation | libc::LOCK_NB) } == 0 {
                let opened = fstat(fd.as_raw_fd())?;
                let current = stat_at(room.as_raw_fd(), &name)?;
                validate_owner_file(&current)?;
                if !same_identity(&opened, &current) {
                    return Err(ArtifactError::ArtifactUnavailable);
                }
                return Ok(ArtifactLock { _fd: fd });
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted
                && error.kind() != std::io::ErrorKind::WouldBlock
            {
                return Err(ArtifactError::ArtifactUnavailable);
            }
            let remaining = wait_until.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return Err(ArtifactError::DeadlineExpired);
            }
            thread::sleep(remaining.min(Duration::from_millis(5)));
        }
    }

    fn screenshot_directory(&self, verified: &VerifiedArtifact) -> Result<OwnedFd, ArtifactError> {
        let rooms = open_directory_at(self.root.as_raw_fd(), c("rooms")?.as_c_str())?;
        let room = open_directory_at(
            rooms.as_raw_fd(),
            c(&verified.room_id.to_string())?.as_c_str(),
        )?;
        open_directory_at(room.as_raw_fd(), c("screenshots")?.as_c_str())
    }

    fn deleted_directory(&self, verified: &VerifiedArtifact) -> Result<OwnedFd, ArtifactError> {
        let artifacts = ensure_directory_at(self.root.as_raw_fd(), c("artifacts")?.as_c_str())?;
        let version = ensure_directory_at(artifacts.as_raw_fd(), c("v1")?.as_c_str())?;
        let deleted = ensure_directory_at(version.as_raw_fd(), c("deleted")?.as_c_str())?;
        ensure_directory_at(
            deleted.as_raw_fd(),
            c(&verified.room_id.to_string())?.as_c_str(),
        )
    }

    fn sync_tombstone_directory(&self, verified: &VerifiedArtifact) -> Result<(), ArtifactError> {
        let directory = self.deleted_directory(verified)?;
        sync_fd(directory.as_raw_fd())?;
        self.hooks.after(FaultPoint::TombstoneDirectorySynced)
    }

    fn open_png_metadata(
        &self,
        verified: &VerifiedArtifact,
    ) -> Result<(OwnedFd, FileIdentity), ArtifactError> {
        let directory = self.screenshot_directory(verified)?;
        let name = png_name(verified)?;
        let entry = stat_at(directory.as_raw_fd(), &name)?;
        let fd = open_readonly_file(directory.as_raw_fd(), &name)?;
        let opened = fstat(fd.as_raw_fd())?;
        validate_artifact_file(&opened)?;
        if opened.st_size as u64 > MAX_BODY_BYTES as u64 {
            return Err(ArtifactError::BodyTooLarge);
        }
        validate_png_header(fd.as_raw_fd(), verified)?;
        let after = fstat(fd.as_raw_fd())?;
        let current = stat_at(directory.as_raw_fd(), &name)?;
        if !same_identity(&entry, &opened)
            || !same_identity(&opened, &after)
            || !same_identity(&after, &current)
        {
            return Err(ArtifactError::ArtifactUnavailable);
        }
        Ok((fd, FileIdentity::from_stat(&opened)?))
    }

    fn open_png(&self, verified: &VerifiedArtifact) -> Result<OpenedPng, ArtifactError> {
        let directory = self.screenshot_directory(verified)?;
        let name = png_name(verified)?;
        let entry = stat_at(directory.as_raw_fd(), &name)?;
        let fd = open_readonly_file(directory.as_raw_fd(), &name)?;
        let opened = fstat(fd.as_raw_fd())?;
        validate_artifact_file(&opened)?;
        if opened.st_size as u64 > MAX_BODY_BYTES as u64 {
            return Err(ArtifactError::BodyTooLarge);
        }
        if !same_identity(&entry, &opened) {
            return Err(ArtifactError::ArtifactUnavailable);
        }
        let identity = FileIdentity::from_stat(&opened)?;
        let mut file = File::from(fd);
        let mut bytes = Vec::with_capacity(identity.size as usize);
        Read::by_ref(&mut file)
            .take(MAX_BODY_BYTES as u64 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| ArtifactError::ArtifactUnavailable)?;
        if bytes.len() > MAX_BODY_BYTES {
            return Err(ArtifactError::BodyTooLarge);
        }
        if bytes.len() as u64 != identity.size {
            return Err(ArtifactError::ArtifactUnavailable);
        }
        let after = fstat(file.as_raw_fd())?;
        let current = stat_at(directory.as_raw_fd(), &name)?;
        if !same_identity(&opened, &after) || !same_identity(&after, &current) {
            return Err(ArtifactError::ArtifactUnavailable);
        }
        Ok(OpenedPng { bytes, identity })
    }

    fn tombstone(&self, verified: &VerifiedArtifact) -> Result<TombstoneState, ArtifactError> {
        let directory = self.deleted_directory(verified)?;
        let name = tombstone_name(verified)?;
        let Some(record) = read_tombstone_optional(directory.as_raw_fd(), &name)? else {
            return Ok(TombstoneState::Absent);
        };
        if record.matches_verified(verified) {
            Ok(TombstoneState::Exact(record))
        } else {
            Ok(TombstoneState::Conflict)
        }
    }

    fn publish_tombstone(
        &self,
        verified: &VerifiedArtifact,
        proposed: Tombstone,
    ) -> Result<Tombstone, ArtifactError> {
        let directory = self.deleted_directory(verified)?;
        let final_name = tombstone_name(verified)?;
        if let Some(existing) = read_tombstone_optional(directory.as_raw_fd(), &final_name)? {
            return if existing.same_deletion(&proposed) {
                Ok(existing)
            } else {
                Err(ArtifactError::ArtifactConflict)
            };
        }
        let pending_name = tombstone_pending_name(verified)?;
        let (record, mut pending_file) =
            match read_tombstone_optional(directory.as_raw_fd(), &pending_name)? {
                Some(_) => {
                    let pending = open_readonly_file(directory.as_raw_fd(), &pending_name)?;
                    let mut pending_file = File::from(pending);
                    self.hooks
                        .before_pending_publish(directory.as_raw_fd(), &pending_name)?;
                    pending_file
                        .sync_all()
                        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
                    let reopened = read_tombstone_file(&mut pending_file)?;
                    if !reopened.same_deletion(&proposed) {
                        return Err(ArtifactError::ArtifactConflict);
                    }
                    self.hooks.after(FaultPoint::TombstoneSynced)?;
                    (reopened, pending_file)
                }
                None => {
                    let fd = create_owner_file_exclusive(directory.as_raw_fd(), &pending_name)?;
                    let encoded = serde_json::to_vec(&proposed)
                        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
                    if encoded.len() > MAX_TOMBSTONE_BYTES {
                        return Err(ArtifactError::ArtifactUnavailable);
                    }
                    let mut file = File::from(fd);
                    file.write_all(&encoded)
                        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
                    self.hooks.after(FaultPoint::TombstoneWritten)?;
                    file.sync_all()
                        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
                    self.hooks.after(FaultPoint::TombstoneSynced)?;
                    let written = read_tombstone_file(&mut file)?;
                    if written != proposed {
                        return Err(ArtifactError::ArtifactConflict);
                    }
                    (written, file)
                }
            };
        verify_tombstone_binding(
            &mut pending_file,
            directory.as_raw_fd(),
            &pending_name,
            &record,
        )?;
        match rename_exclusive(
            directory.as_raw_fd(),
            &pending_name,
            directory.as_raw_fd(),
            &final_name,
        ) {
            Ok(()) => {}
            Err(error) if error.raw_os_error() == Some(libc::EEXIST) => {
                let existing = read_tombstone_optional(directory.as_raw_fd(), &final_name)?
                    .ok_or(ArtifactError::ArtifactConflict)?;
                if !existing.same_deletion(&record) {
                    return Err(ArtifactError::ArtifactConflict);
                }
                verify_tombstone_binding(
                    &mut pending_file,
                    directory.as_raw_fd(),
                    &pending_name,
                    &record,
                )?;
                unlink_optional(directory.as_raw_fd(), &pending_name)?;
                return Ok(existing);
            }
            Err(_) => return Err(ArtifactError::ArtifactUnavailable),
        }
        verify_tombstone_binding(
            &mut pending_file,
            directory.as_raw_fd(),
            &final_name,
            &record,
        )?;
        self.hooks.after(FaultPoint::TombstonePublished)?;
        sync_fd(directory.as_raw_fd())?;
        self.hooks.after(FaultPoint::TombstoneDirectorySynced)?;
        Ok(record)
    }

    fn cleanup_recorded_png(
        &self,
        verified: &VerifiedArtifact,
        record: &Tombstone,
    ) -> Result<(), ArtifactError> {
        let directory = self.screenshot_directory(verified)?;
        let canonical = png_name(verified)?;
        let pending = png_pending_name(verified)?;
        if exists_at(directory.as_raw_fd(), &pending)? {
            if exists_at(directory.as_raw_fd(), &canonical)? {
                return Err(ArtifactError::ArtifactConflict);
            }
            return self.unlink_matching_pending(directory.as_raw_fd(), &pending, record);
        }
        if !exists_at(directory.as_raw_fd(), &canonical)? {
            sync_fd(directory.as_raw_fd())?;
            self.hooks.after(FaultPoint::ScreenshotDirectorySynced)?;
            return Ok(());
        }
        let current = read_identity_and_digest(directory.as_raw_fd(), &canonical)?;
        if !record.matches_identity(&current.identity, &current.sha256) {
            return Err(ArtifactError::ArtifactConflict);
        }
        rename_exclusive(
            directory.as_raw_fd(),
            &canonical,
            directory.as_raw_fd(),
            &pending,
        )
        .map_err(|error| {
            if error.raw_os_error() == Some(libc::EEXIST) {
                ArtifactError::ArtifactConflict
            } else {
                ArtifactError::ArtifactUnavailable
            }
        })?;
        self.hooks.after(FaultPoint::PngRenamed)?;
        let moved = read_identity_and_digest(directory.as_raw_fd(), &pending)?;
        if !record.matches_identity(&moved.identity, &moved.sha256) {
            let _ = rename_exclusive(
                directory.as_raw_fd(),
                &pending,
                directory.as_raw_fd(),
                &canonical,
            );
            return Err(ArtifactError::ArtifactConflict);
        }
        self.hooks.after(FaultPoint::PendingVerified)?;
        let before_unlink = stat_at(directory.as_raw_fd(), &pending)?;
        if !record.matches_stat(&before_unlink)? {
            return Err(ArtifactError::ArtifactConflict);
        }
        unlink_required(directory.as_raw_fd(), &pending)?;
        self.hooks.after(FaultPoint::PngUnlinked)?;
        sync_fd(directory.as_raw_fd())?;
        self.hooks.after(FaultPoint::ScreenshotDirectorySynced)?;
        Ok(())
    }

    fn unlink_matching_pending(
        &self,
        directory: RawFd,
        pending: &CStr,
        record: &Tombstone,
    ) -> Result<(), ArtifactError> {
        let opened = read_identity_and_digest(directory, pending)?;
        if !record.matches_identity(&opened.identity, &opened.sha256) {
            return Err(ArtifactError::ArtifactConflict);
        }
        self.hooks.after(FaultPoint::PendingVerified)?;
        let current = stat_at(directory, pending)?;
        if !record.matches_stat(&current)? {
            return Err(ArtifactError::ArtifactConflict);
        }
        unlink_required(directory, pending)?;
        self.hooks.after(FaultPoint::PngUnlinked)?;
        sync_fd(directory)?;
        self.hooks.after(FaultPoint::ScreenshotDirectorySynced)?;
        Ok(())
    }
}

fn validate_png_header(fd: RawFd, verified: &VerifiedArtifact) -> Result<(), ArtifactError> {
    let mut header = [0u8; 24];
    let mut offset = 0usize;
    while offset < header.len() {
        let read = unsafe {
            libc::pread(
                fd,
                header[offset..].as_mut_ptr().cast(),
                header.len() - offset,
                offset as libc::off_t,
            )
        };
        if read == 0 {
            return Err(ArtifactError::InvalidMetadata);
        }
        if read < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(ArtifactError::ArtifactUnavailable);
        }
        offset += usize::try_from(read).map_err(|_| ArtifactError::ArtifactUnavailable)?;
    }
    if header[..8] != *b"\x89PNG\r\n\x1a\n"
        || header[8..12] != 13u32.to_be_bytes()
        || header[12..16] != *b"IHDR"
    {
        return Err(ArtifactError::InvalidMetadata);
    }
    let width = u32::from_be_bytes(header[16..20].try_into().unwrap());
    let height = u32::from_be_bytes(header[20..24].try_into().unwrap());
    if width != verified.width
        || height != verified.height
        || width == 0
        || height == 0
        || u64::from(width).saturating_mul(u64::from(height)) > MAX_PIXELS
    {
        return Err(ArtifactError::InvalidMetadata);
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum LockKind {
    Shared,
    Exclusive,
}

struct ArtifactLock {
    _fd: OwnedFd,
}

struct OpenedPng {
    bytes: Vec<u8>,
    identity: FileIdentity,
}

struct IdentityDigest {
    identity: FileIdentity,
    sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Tombstone {
    version: u16,
    room_id: u64,
    action_id: String,
    event_seq: u64,
    screenshot_ref: String,
    width: u32,
    height: u32,
    deleted_at_ms: u64,
    device: u64,
    inode: u64,
    size: u64,
    sha256: String,
}

impl Tombstone {
    fn new(
        verified: &VerifiedArtifact,
        identity: &FileIdentity,
        sha256: String,
    ) -> Result<Self, ArtifactError> {
        Ok(Self {
            version: 1,
            room_id: verified.room_id,
            action_id: verified.action_id.as_str().into(),
            event_seq: verified.event_seq,
            screenshot_ref: verified.screenshot_ref.clone(),
            width: verified.width,
            height: verified.height,
            deleted_at_ms: unix_ms()?,
            device: identity.device,
            inode: identity.inode,
            size: identity.size,
            sha256,
        })
    }

    fn matches_verified(&self, verified: &VerifiedArtifact) -> bool {
        self.valid_shape()
            && self.room_id == verified.room_id
            && self.action_id == verified.action_id.as_str()
            && self.event_seq == verified.event_seq
            && self.screenshot_ref == verified.screenshot_ref
            && self.width == verified.width
            && self.height == verified.height
    }

    fn same_deletion(&self, other: &Self) -> bool {
        self.valid_shape()
            && other.valid_shape()
            && self.matches_record_identity(other)
            && self.device == other.device
            && self.inode == other.inode
            && self.size == other.size
            && self.sha256 == other.sha256
    }

    fn matches_record_identity(&self, other: &Self) -> bool {
        self.version == other.version
            && self.room_id == other.room_id
            && self.action_id == other.action_id
            && self.event_seq == other.event_seq
            && self.screenshot_ref == other.screenshot_ref
            && self.width == other.width
            && self.height == other.height
    }

    fn valid_shape(&self) -> bool {
        self.version == 1
            && self.room_id > 0
            && self.event_seq > 0
            && crate::protocol::canonical_uuid(&self.action_id)
            && self.screenshot_ref
                == format!("rooms/{}/screenshots/{}.png", self.room_id, self.action_id)
            && self.width > 0
            && self.height > 0
            && u64::from(self.width).saturating_mul(u64::from(self.height)) <= MAX_PIXELS
            && self.deleted_at_ms > 0
            && valid_digest(&self.sha256)
            && self.size > 0
            && self.size <= MAX_BODY_BYTES as u64
    }

    fn matches_identity(&self, identity: &FileIdentity, sha256: &str) -> bool {
        self.device == identity.device
            && self.inode == identity.inode
            && self.size == identity.size
            && self.sha256 == sha256
    }

    fn matches_stat(&self, stat: &libc::stat) -> Result<bool, ArtifactError> {
        validate_artifact_file(stat)?;
        Ok(self.device == stat_device(stat)
            && self.inode == stat_inode(stat)
            && self.size == u64::try_from(stat.st_size).unwrap_or(u64::MAX))
    }
}

enum TombstoneState {
    Absent,
    Exact(Tombstone),
    Conflict,
}

#[derive(Clone, Copy, Debug)]
struct FileIdentity {
    device: u64,
    inode: u64,
    size: u64,
}

impl FileIdentity {
    fn from_stat(stat: &libc::stat) -> Result<Self, ArtifactError> {
        Ok(Self {
            device: stat_device(stat),
            inode: stat_inode(stat),
            size: u64::try_from(stat.st_size).map_err(|_| ArtifactError::ArtifactUnavailable)?,
        })
    }
}

fn validate_png(bytes: Vec<u8>, verified: &VerifiedArtifact) -> Result<VerifiedPng, ArtifactError> {
    let mut decoder = png::Decoder::new_with_limits(
        Cursor::new(bytes.as_slice()),
        png::Limits {
            bytes: MAX_DECODED_BYTES,
        },
    );
    decoder.set_transformations(png::Transformations::IDENTITY);
    let mut reader = decoder
        .read_info()
        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
    let info = reader.info();
    if info.width != verified.width
        || info.height != verified.height
        || info.width == 0
        || info.height == 0
        || u64::from(info.width).saturating_mul(u64::from(info.height)) > MAX_PIXELS
    {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    let output_size = reader.output_buffer_size();
    if output_size > MAX_DECODED_BYTES {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    let mut decoded = vec![0; output_size];
    reader
        .next_frame(&mut decoded)
        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
    reader
        .finish()
        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
    let sha256 = format!("{:x}", Sha256::digest(&bytes));
    Ok(VerifiedPng {
        bytes,
        width: verified.width,
        height: verified.height,
        sha256,
    })
}

fn validate_verified(verified: &VerifiedArtifact) -> Result<(), ArtifactError> {
    let expected = format!(
        "rooms/{}/screenshots/{}.png",
        verified.room_id,
        verified.action_id.as_str()
    );
    if verified.room_id == 0
        || verified.event_seq == 0
        || verified.screenshot_ref != expected
        || verified.width == 0
        || verified.height == 0
        || u64::from(verified.width).saturating_mul(u64::from(verified.height)) > MAX_PIXELS
    {
        return Err(ArtifactError::InvalidMetadata);
    }
    Ok(())
}

fn read_identity_and_digest(
    directory: RawFd,
    name: &CStr,
) -> Result<IdentityDigest, ArtifactError> {
    let before = stat_at(directory, name)?;
    let fd = open_readonly_file(directory, name)?;
    let opened = fstat(fd.as_raw_fd())?;
    validate_artifact_file(&opened)?;
    if !same_identity(&before, &opened) {
        return Err(ArtifactError::ArtifactConflict);
    }
    let identity = FileIdentity::from_stat(&opened)?;
    if identity.size > MAX_BODY_BYTES as u64 {
        return Err(ArtifactError::BodyTooLarge);
    }
    let mut file = File::from(fd);
    let mut hasher = Sha256::new();
    let copied = std::io::copy(
        &mut Read::by_ref(&mut file).take(MAX_BODY_BYTES as u64 + 1),
        &mut HashWriter(&mut hasher),
    )
    .map_err(|_| ArtifactError::ArtifactUnavailable)?;
    if copied != identity.size {
        return Err(if copied > MAX_BODY_BYTES as u64 {
            ArtifactError::BodyTooLarge
        } else {
            ArtifactError::ArtifactConflict
        });
    }
    let after = fstat(file.as_raw_fd())?;
    let current = stat_at(directory, name)?;
    if !same_identity(&opened, &after) || !same_identity(&after, &current) {
        return Err(ArtifactError::ArtifactConflict);
    }
    Ok(IdentityDigest {
        identity,
        sha256: format!("{:x}", hasher.finalize()),
    })
}

struct HashWriter<'a>(&'a mut Sha256);

impl Write for HashWriter<'_> {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        self.0.update(buffer);
        Ok(buffer.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn read_tombstone_optional(
    directory: RawFd,
    name: &CStr,
) -> Result<Option<Tombstone>, ArtifactError> {
    if !exists_at(directory, name)? {
        return Ok(None);
    }
    let fd = match open_readonly_file(directory, name) {
        Ok(fd) => fd,
        Err(ArtifactError::ArtifactUnavailable) if !exists_at(directory, name)? => return Ok(None),
        Err(error) => return Err(error),
    };
    let opened = fstat(fd.as_raw_fd())?;
    validate_owner_file(&opened)?;
    if opened.st_size <= 0 || opened.st_size as usize > MAX_TOMBSTONE_BYTES {
        return Err(ArtifactError::ArtifactConflict);
    }
    let mut bytes = Vec::with_capacity(opened.st_size as usize);
    let file = File::from(fd);
    Read::take(file, MAX_TOMBSTONE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ArtifactError::ArtifactUnavailable)?;
    if bytes.len() != opened.st_size as usize {
        return Err(ArtifactError::ArtifactConflict);
    }
    let current = stat_at(directory, name)?;
    if !same_identity(&opened, &current) {
        return Err(ArtifactError::ArtifactConflict);
    }
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|_| ArtifactError::ArtifactConflict)
}

fn read_tombstone_file(file: &mut File) -> Result<Tombstone, ArtifactError> {
    let opened = fstat(file.as_raw_fd()).map_err(|_| ArtifactError::ArtifactConflict)?;
    validate_owner_file(&opened).map_err(|_| ArtifactError::ArtifactConflict)?;
    if opened.st_size <= 0 || opened.st_size as usize > MAX_TOMBSTONE_BYTES {
        return Err(ArtifactError::ArtifactConflict);
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| ArtifactError::ArtifactConflict)?;
    let mut bytes = Vec::with_capacity(opened.st_size as usize);
    Read::by_ref(file)
        .take(MAX_TOMBSTONE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| ArtifactError::ArtifactConflict)?;
    if bytes.len() != opened.st_size as usize {
        return Err(ArtifactError::ArtifactConflict);
    }
    serde_json::from_slice(&bytes).map_err(|_| ArtifactError::ArtifactConflict)
}

fn verify_tombstone_binding(
    file: &mut File,
    directory: RawFd,
    name: &CStr,
    expected: &Tombstone,
) -> Result<(), ArtifactError> {
    let opened = fstat(file.as_raw_fd()).map_err(|_| ArtifactError::ArtifactConflict)?;
    validate_owner_file(&opened).map_err(|_| ArtifactError::ArtifactConflict)?;
    let current = stat_at(directory, name).map_err(|_| ArtifactError::ArtifactConflict)?;
    if !same_identity(&opened, &current) || read_tombstone_file(file)? != *expected {
        return Err(ArtifactError::ArtifactConflict);
    }
    let after = fstat(file.as_raw_fd()).map_err(|_| ArtifactError::ArtifactConflict)?;
    let rebound = stat_at(directory, name).map_err(|_| ArtifactError::ArtifactConflict)?;
    if !same_identity(&opened, &after) || !same_identity(&after, &rebound) {
        return Err(ArtifactError::ArtifactConflict);
    }
    Ok(())
}

fn open_directory_at(parent: RawFd, name: &CStr) -> Result<OwnedFd, ArtifactError> {
    let before = stat_at(parent, name)?;
    let raw = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if raw < 0 {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    let opened = fstat(fd.as_raw_fd())?;
    validate_directory_stat(&opened)?;
    let after = stat_at(parent, name)?;
    if !same_identity(&before, &opened) || !same_identity(&opened, &after) {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(fd)
}

fn ensure_directory_at(parent: RawFd, name: &CStr) -> Result<OwnedFd, ArtifactError> {
    match open_directory_at(parent, name) {
        Ok(fd) => return Ok(fd),
        Err(_) if exists_at(parent, name)? => return Err(ArtifactError::ArtifactUnavailable),
        Err(_) => {}
    }
    if unsafe { libc::mkdirat(parent, name.as_ptr(), DIRECTORY_MODE) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::EEXIST) {
            return Err(ArtifactError::ArtifactUnavailable);
        }
    } else {
        sync_fd(parent)?;
    }
    let fd = open_directory_at(parent, name)?;
    if unsafe { libc::fchmod(fd.as_raw_fd(), DIRECTORY_MODE) } != 0 {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    validate_directory(fd.as_raw_fd())?;
    Ok(fd)
}

fn open_owner_file(
    parent: RawFd,
    name: &CStr,
    read_write: bool,
    create: bool,
) -> Result<OwnedFd, ArtifactError> {
    if create {
        match create_owner_file_exclusive(parent, name) {
            Ok(fd) => return Ok(fd),
            Err(ArtifactError::ArtifactConflict) => {}
            Err(error) => return Err(error),
        }
    }
    let before = stat_at(parent, name)?;
    let flags = if read_write {
        libc::O_RDWR
    } else {
        libc::O_RDONLY
    };
    let raw = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            flags | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if raw < 0 {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    let opened = fstat(fd.as_raw_fd())?;
    validate_owner_file(&opened)?;
    let after = stat_at(parent, name)?;
    if !same_identity(&before, &opened) || !same_identity(&opened, &after) {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(fd)
}

fn create_owner_file_exclusive(parent: RawFd, name: &CStr) -> Result<OwnedFd, ArtifactError> {
    let raw = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            FILE_MODE as libc::c_uint,
        )
    };
    if raw < 0 {
        let error = std::io::Error::last_os_error();
        return Err(if error.raw_os_error() == Some(libc::EEXIST) {
            ArtifactError::ArtifactConflict
        } else {
            ArtifactError::ArtifactUnavailable
        });
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    if unsafe { libc::fchmod(fd.as_raw_fd(), FILE_MODE) } != 0 {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    let opened = fstat(fd.as_raw_fd())?;
    validate_owner_file(&opened)?;
    let current = stat_at(parent, name)?;
    if !same_identity(&opened, &current) {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(fd)
}

fn open_readonly_file(parent: RawFd, name: &CStr) -> Result<OwnedFd, ArtifactError> {
    open_owner_file(parent, name, false, false)
}

fn validate_directory(fd: RawFd) -> Result<(), ArtifactError> {
    validate_directory_stat(&fstat(fd)?)
}

fn validate_directory_stat(stat: &libc::stat) -> Result<(), ArtifactError> {
    if stat.st_mode & libc::S_IFMT != libc::S_IFDIR
        || stat.st_uid != unsafe { libc::geteuid() }
        || stat.st_mode & 0o777 != DIRECTORY_MODE
    {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(())
}

fn validate_owner_file(stat: &libc::stat) -> Result<(), ArtifactError> {
    if stat.st_mode & libc::S_IFMT != libc::S_IFREG
        || stat.st_uid != unsafe { libc::geteuid() }
        || stat.st_mode & 0o777 != FILE_MODE
        || stat.st_nlink != 1
    {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(())
}

fn validate_artifact_file(stat: &libc::stat) -> Result<(), ArtifactError> {
    validate_owner_file(stat)?;
    if stat.st_size <= 0 {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(())
}

fn fstat(fd: RawFd) -> Result<libc::stat, ArtifactError> {
    let mut stat = MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, stat.as_mut_ptr()) } != 0 {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(unsafe { stat.assume_init() })
}

fn stat_at(parent: RawFd, name: &CStr) -> Result<libc::stat, ArtifactError> {
    let mut stat = MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } != 0
    {
        return Err(ArtifactError::ArtifactUnavailable);
    }
    Ok(unsafe { stat.assume_init() })
}

fn exists_at(parent: RawFd, name: &CStr) -> Result<bool, ArtifactError> {
    let mut stat = MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } == 0
    {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if error.kind() == std::io::ErrorKind::NotFound {
        Ok(false)
    } else {
        Err(ArtifactError::ArtifactUnavailable)
    }
}

fn same_identity(left: &libc::stat, right: &libc::stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

#[allow(clippy::unnecessary_cast)]
fn stat_device(stat: &libc::stat) -> u64 {
    stat.st_dev as u64
}

#[allow(clippy::unnecessary_cast)]
fn stat_inode(stat: &libc::stat) -> u64 {
    stat.st_ino as u64
}

fn sync_fd(fd: RawFd) -> Result<(), ArtifactError> {
    if unsafe { libc::fsync(fd) } == 0 {
        Ok(())
    } else {
        Err(ArtifactError::ArtifactUnavailable)
    }
}

fn unlink_required(parent: RawFd, name: &CStr) -> Result<(), ArtifactError> {
    if unsafe { libc::unlinkat(parent, name.as_ptr(), 0) } == 0 {
        Ok(())
    } else {
        Err(ArtifactError::ArtifactUnavailable)
    }
}

fn unlink_optional(parent: RawFd, name: &CStr) -> Result<(), ArtifactError> {
    if unsafe { libc::unlinkat(parent, name.as_ptr(), 0) } == 0 {
        return Ok(());
    }
    if std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound {
        Ok(())
    } else {
        Err(ArtifactError::ArtifactUnavailable)
    }
}

#[cfg(target_vendor = "apple")]
fn rename_exclusive(from_dir: RawFd, from: &CStr, to_dir: RawFd, to: &CStr) -> std::io::Result<()> {
    if unsafe {
        libc::renameatx_np(
            from_dir,
            from.as_ptr(),
            to_dir,
            to.as_ptr(),
            libc::RENAME_EXCL,
        )
    } == 0
    {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(target_os = "linux")]
fn rename_exclusive(from_dir: RawFd, from: &CStr, to_dir: RawFd, to: &CStr) -> std::io::Result<()> {
    if unsafe {
        libc::renameat2(
            from_dir,
            from.as_ptr(),
            to_dir,
            to.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    } == 0
    {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(not(any(target_vendor = "apple", target_os = "linux")))]
fn rename_exclusive(
    _from_dir: RawFd,
    _from: &CStr,
    _to_dir: RawFd,
    _to: &CStr,
) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "exclusive rename unavailable",
    ))
}

fn c(value: &str) -> Result<CString, ArtifactError> {
    CString::new(value).map_err(|_| ArtifactError::ArtifactUnavailable)
}

fn png_name(verified: &VerifiedArtifact) -> Result<CString, ArtifactError> {
    c(&format!("{}.png", verified.action_id.as_str()))
}

fn png_pending_name(verified: &VerifiedArtifact) -> Result<CString, ArtifactError> {
    c(&format!(
        ".{}.png.deleted-pending",
        verified.action_id.as_str()
    ))
}

fn tombstone_name(verified: &VerifiedArtifact) -> Result<CString, ArtifactError> {
    c(&format!("{}.json", verified.action_id.as_str()))
}

fn tombstone_pending_name(verified: &VerifiedArtifact) -> Result<CString, ArtifactError> {
    c(&format!(".{}.json.pending", verified.action_id.as_str()))
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn unix_ms() -> Result<u64, ArtifactError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ArtifactError::DeadlineExpired)?
        .as_millis()
        .try_into()
        .map_err(|_| ArtifactError::DeadlineExpired)
}

struct Hooks {
    #[cfg(test)]
    fail_once: std::sync::Mutex<Option<FaultPoint>>,
    #[cfg(test)]
    swap_pending: std::sync::Mutex<bool>,
}

impl Hooks {
    fn none() -> Self {
        Self {
            #[cfg(test)]
            fail_once: std::sync::Mutex::new(None),
            #[cfg(test)]
            swap_pending: std::sync::Mutex::new(false),
        }
    }

    #[cfg(test)]
    fn fail_once(point: FaultPoint) -> Self {
        Self {
            fail_once: std::sync::Mutex::new(Some(point)),
            swap_pending: std::sync::Mutex::new(false),
        }
    }

    #[cfg(test)]
    fn swap_pending_once() -> Self {
        Self {
            fail_once: std::sync::Mutex::new(None),
            swap_pending: std::sync::Mutex::new(true),
        }
    }

    fn before_pending_publish(
        &self,
        directory: RawFd,
        pending_name: &CStr,
    ) -> Result<(), ArtifactError> {
        #[cfg(test)]
        {
            let mut swap = self
                .swap_pending
                .lock()
                .map_err(|_| ArtifactError::ArtifactUnavailable)?;
            if *swap {
                *swap = false;
                unlink_required(directory, pending_name)?;
                let replacement = create_owner_file_exclusive(directory, pending_name)?;
                let mut file = File::from(replacement);
                file.write_all(b"{}")
                    .map_err(|_| ArtifactError::ArtifactUnavailable)?;
                file.sync_all()
                    .map_err(|_| ArtifactError::ArtifactUnavailable)?;
            }
        }
        #[cfg(not(test))]
        let _ = (directory, pending_name);
        Ok(())
    }

    fn after(&self, point: FaultPoint) -> Result<(), ArtifactError> {
        #[cfg(test)]
        {
            let mut selected = self
                .fail_once
                .lock()
                .map_err(|_| ArtifactError::ArtifactUnavailable)?;
            if selected.as_ref() == Some(&point) {
                selected.take();
                return Err(ArtifactError::ArtifactUnavailable);
            }
        }
        #[cfg(not(test))]
        let _ = point;
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FaultPoint {
    TombstoneWritten,
    TombstoneSynced,
    TombstonePublished,
    TombstoneDirectorySynced,
    PngRenamed,
    PendingVerified,
    PngUnlinked,
    ScreenshotDirectorySynced,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ActionId;
    use std::{
        fs::{self, File, OpenOptions},
        io::Write,
        os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
        path::{Path, PathBuf},
        time::{SystemTime, UNIX_EPOCH},
    };

    const ACTION: &str = "6ba7b810-9dad-41d1-80b4-00c04fd430c8";

    struct Root(PathBuf);

    impl Root {
        fn new(label: &str) -> Self {
            let path = std::env::temp_dir().join(format!(
                "gamja-artifact-storage-unit-{label}-{}-{}",
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

    impl Drop for Root {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn create_dir(path: &Path) {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(path)
            .unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn image() -> Vec<u8> {
        let mut bytes = Vec::new();
        {
            let mut encoder = png::Encoder::new(&mut bytes, 1, 1);
            encoder.set_color(png::ColorType::Rgba);
            encoder.set_depth(png::BitDepth::Eight);
            let mut writer = encoder.write_header().unwrap();
            writer.write_image_data(&[0, 0, 0, 0]).unwrap();
        }
        bytes
    }

    fn artifact(root: &Path, bytes: &[u8]) -> PathBuf {
        let directory = root.join("rooms/7/screenshots");
        create_dir(&directory);
        let path = directory.join(format!("{ACTION}.png"));
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .unwrap();
        file.write_all(bytes).unwrap();
        file.sync_all().unwrap();
        path
    }

    fn proof() -> VerifiedArtifact {
        VerifiedArtifact {
            room_id: 7,
            action_id: ActionId::parse(ACTION).unwrap(),
            event_seq: 13,
            screenshot_ref: format!("rooms/7/screenshots/{ACTION}.png"),
            width: 1,
            height: 1,
            created_at: "2026-10-02T01:02:03+00:00".into(),
        }
    }

    fn root_fd(root: &Path) -> OwnedFd {
        File::open(root).unwrap().into()
    }

    fn normal_store(root: &Path) -> ArtifactStore {
        ArtifactStore::from_app_data_fd(root_fd(root), unix_ms().unwrap() + 10_000).unwrap()
    }

    fn fault_store(root: &Path, point: FaultPoint) -> ArtifactStore {
        ArtifactStore::with_fault(root_fd(root), unix_ms().unwrap() + 10_000, point).unwrap()
    }

    fn swap_store(root: &Path) -> ArtifactStore {
        ArtifactStore::with_pending_publish_swap(root_fd(root), unix_ms().unwrap() + 10_000)
            .unwrap()
    }

    #[test]
    fn storage_delete_recovers_after_each_durable_transition() {
        let points = [
            FaultPoint::TombstoneWritten,
            FaultPoint::TombstoneSynced,
            FaultPoint::TombstonePublished,
            FaultPoint::TombstoneDirectorySynced,
            FaultPoint::PngRenamed,
            FaultPoint::PendingVerified,
            FaultPoint::PngUnlinked,
            FaultPoint::ScreenshotDirectorySynced,
        ];
        for point in points {
            let root = Root::new(&format!("{point:?}"));
            artifact(&root.0, &image());
            assert_eq!(
                fault_store(&root.0, point).delete(&proof()),
                Err(ArtifactError::ArtifactUnavailable),
                "{point:?}"
            );
            let retry = normal_store(&root.0);
            let after_publication = matches!(
                point,
                FaultPoint::TombstonePublished
                    | FaultPoint::TombstoneDirectorySynced
                    | FaultPoint::PngRenamed
                    | FaultPoint::PendingVerified
                    | FaultPoint::PngUnlinked
                    | FaultPoint::ScreenshotDirectorySynced
            );
            assert_eq!(
                retry.read(&proof()),
                if after_publication {
                    Err(ArtifactError::ArtifactRemoved)
                } else {
                    Ok(validate_png(image(), &proof()).unwrap())
                },
                "{point:?}"
            );
            let outcome = retry.delete(&proof()).unwrap();
            assert_eq!(
                outcome,
                if after_publication {
                    DeleteOutcome::AlreadyDeleted
                } else {
                    DeleteOutcome::Deleted
                },
                "{point:?}"
            );
            assert!(!root
                .0
                .join(format!("rooms/7/screenshots/{ACTION}.png"))
                .exists());
            assert!(!root
                .0
                .join(format!("rooms/7/screenshots/.{ACTION}.png.deleted-pending"))
                .exists());
        }
    }

    #[test]
    fn storage_retry_never_deletes_a_replacement_after_tombstone_publication() {
        let root = Root::new("canonical-replacement");
        let original = image();
        let path = artifact(&root.0, &original);
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstoneDirectorySynced).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        fs::rename(&path, root.0.join("saved-original")).unwrap();
        let replacement = vec![0x41; 123];
        artifact(&root.0, &replacement);

        assert_eq!(
            normal_store(&root.0).delete(&proof()),
            Err(ArtifactError::ArtifactConflict)
        );
        assert_eq!(fs::read(path).unwrap(), replacement);
        assert_eq!(
            normal_store(&root.0).read(&proof()),
            Err(ArtifactError::ArtifactRemoved)
        );
    }

    #[test]
    fn storage_retry_rejects_a_replaced_pending_file_without_unlinking_it() {
        let root = Root::new("pending-replacement");
        artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::PngRenamed).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        let pending = root
            .0
            .join(format!("rooms/7/screenshots/.{ACTION}.png.deleted-pending"));
        fs::remove_file(&pending).unwrap();
        let replacement = b"replacement-must-survive";
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&pending)
            .unwrap();
        file.write_all(replacement).unwrap();
        drop(file);

        assert_eq!(
            normal_store(&root.0).delete(&proof()),
            Err(ArtifactError::ArtifactConflict)
        );
        assert_eq!(fs::read(pending).unwrap(), replacement);
    }

    #[test]
    fn storage_retry_rejects_an_invalid_pending_tombstone() {
        let root = Root::new("invalid-pending-tombstone");
        artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstoneSynced).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        let pending = root
            .0
            .join(format!("artifacts/v1/deleted/7/.{ACTION}.json.pending"));
        let mut record: serde_json::Value =
            serde_json::from_slice(&fs::read(&pending).unwrap()).unwrap();
        record["deleted_at_ms"] = 0.into();
        fs::write(&pending, serde_json::to_vec(&record).unwrap()).unwrap();
        fs::set_permissions(&pending, fs::Permissions::from_mode(0o600)).unwrap();

        assert_eq!(
            normal_store(&root.0).delete(&proof()),
            Err(ArtifactError::ArtifactConflict)
        );
        assert!(root
            .0
            .join(format!("rooms/7/screenshots/{ACTION}.png"))
            .exists());
    }

    #[test]
    fn storage_retry_fsyncs_a_reused_pending_tombstone_before_publish() {
        let root = Root::new("pending-tombstone-fsync");
        artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstoneWritten).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstoneSynced).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        assert!(!root
            .0
            .join(format!("artifacts/v1/deleted/7/{ACTION}.json"))
            .exists());
        assert!(root
            .0
            .join(format!("rooms/7/screenshots/{ACTION}.png"))
            .exists());
    }

    #[test]
    fn storage_retry_rejects_pending_tombstone_swap_after_reopen_before_publish() {
        let root = Root::new("pending-tombstone-swap");
        let png = artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstoneWritten).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );

        assert_eq!(
            swap_store(&root.0).delete(&proof()),
            Err(ArtifactError::ArtifactConflict)
        );
        assert!(png.exists());
        assert!(!root
            .0
            .join(format!("artifacts/v1/deleted/7/{ACTION}.json"))
            .exists());
    }

    #[test]
    fn storage_retry_fsyncs_prepublished_tombstone_directory_before_cleanup() {
        let root = Root::new("published-directory-fsync");
        artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstonePublished).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        assert_eq!(
            fault_store(&root.0, FaultPoint::TombstoneDirectorySynced).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        assert!(root
            .0
            .join(format!("rooms/7/screenshots/{ACTION}.png"))
            .exists());
    }

    #[test]
    fn storage_retry_fsyncs_screenshot_directory_after_prior_unlink() {
        let root = Root::new("unlinked-directory-fsync");
        artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::PngUnlinked).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        assert_eq!(
            fault_store(&root.0, FaultPoint::ScreenshotDirectorySynced).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
    }

    #[test]
    fn storage_retry_conflicts_when_pending_original_and_canonical_replacement_both_exist() {
        let root = Root::new("pending-original-canonical-replacement");
        artifact(&root.0, &image());
        assert_eq!(
            fault_store(&root.0, FaultPoint::PngRenamed).delete(&proof()),
            Err(ArtifactError::ArtifactUnavailable)
        );
        let replacement = b"canonical-replacement";
        let canonical = artifact(&root.0, replacement);
        let pending = root
            .0
            .join(format!("rooms/7/screenshots/.{ACTION}.png.deleted-pending"));

        assert_eq!(
            normal_store(&root.0).delete(&proof()),
            Err(ArtifactError::ArtifactConflict)
        );
        assert_eq!(fs::read(canonical).unwrap(), replacement);
        assert!(pending.exists());
    }

    #[test]
    fn storage_owner_checks_reject_a_different_uid() {
        let root = Root::new("wrong-owner");
        let stat = fstat(root_fd(&root.0).as_raw_fd()).unwrap();
        let mut wrong = stat;
        wrong.st_uid = wrong.st_uid.saturating_add(1);
        assert_eq!(
            validate_directory_stat(&wrong),
            Err(ArtifactError::ArtifactUnavailable)
        );
    }
}
