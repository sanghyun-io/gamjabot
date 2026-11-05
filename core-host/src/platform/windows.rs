//! Private Windows DACL creation/read-back and kill-on-close owned engine jobs.
//! No fallback ACL is permitted. Native cutover remains gated by Windows fixture/install tests.
use crate::Result;
use std::{
    ffi::c_void,
    fs::{self, File, OpenOptions},
    mem::{size_of, zeroed},
    os::windows::{
        ffi::OsStrExt,
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawHandle,
    },
    path::Path,
    process::Child,
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, LocalFree, HANDLE, INVALID_HANDLE_VALUE},
    Security::{Authorization::*, *},
    Storage::FileSystem::{
        FILE_ALL_ACCESS, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT,
    },
    System::{Diagnostics::ToolHelp::*, JobObjects::*, Threading::*},
};
const DENIED: &str = "private_dacl_unavailable";
struct Handle(HANDLE);
impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
struct LocalAllocation(*mut c_void);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                LocalFree(self.0);
            }
        }
    }
}
fn user_sid() -> Result<Vec<u8>> {
    unsafe {
        let mut raw = null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) == 0 {
            return Err(DENIED);
        }
        let token = Handle(raw);
        let mut length = 0;
        GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut length);
        if length < size_of::<TOKEN_USER>() as u32 || length > 65536 {
            return Err(DENIED);
        }
        // Word-aligned token storage; the API writes pointers and SID_AND_ATTRIBUTES.
        let mut buffer = vec![0usize; (length as usize).div_ceil(size_of::<usize>())];
        if GetTokenInformation(
            token.0,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            length,
            &mut length,
        ) == 0
        {
            return Err(DENIED);
        }
        let sid = (*(buffer.as_ptr().cast::<TOKEN_USER>())).User.Sid;
        if IsValidSid(sid) == 0 {
            return Err(DENIED);
        }
        let size = GetLengthSid(sid);
        if size == 0 || size > SECURITY_MAX_SID_SIZE {
            return Err(DENIED);
        }
        let mut copy = vec![0u8; size as usize];
        if CopySid(size, copy.as_mut_ptr().cast(), sid) == 0 {
            return Err(DENIED);
        }
        Ok(copy)
    }
}
fn system_sid() -> Result<Vec<u8>> {
    let mut bytes = vec![0u8; SECURITY_MAX_SID_SIZE as usize];
    let mut size = bytes.len() as u32;
    unsafe {
        if CreateWellKnownSid(
            WinLocalSystemSid,
            null_mut(),
            bytes.as_mut_ptr().cast(),
            &mut size,
        ) == 0
        {
            return Err(DENIED);
        }
    }
    bytes.truncate(size as usize);
    Ok(bytes)
}
fn no_reparse(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path).map_err(|_| DENIED)?;
    if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(DENIED);
    }
    Ok(())
}
pub(super) fn secure(path: &Path) -> Result<()> {
    no_reparse(path)?;
    let user = user_sid()?;
    let system = system_sid()?;
    let name: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let directory = path.is_dir();
    unsafe {
        let mut entries: [EXPLICIT_ACCESS_W; 2] = zeroed();
        for (entry, sid) in entries.iter_mut().zip([user.as_ptr(), system.as_ptr()]) {
            entry.grfAccessPermissions = FILE_ALL_ACCESS;
            entry.grfAccessMode = SET_ACCESS;
            // Newly created children inherit only these two principals before their own private ACL is applied.
            entry.grfInheritance = if directory {
                OBJECT_INHERIT_ACE as u32 | CONTAINER_INHERIT_ACE as u32
            } else {
                NO_INHERITANCE
            };
            BuildTrusteeWithSidW(&mut entry.Trustee, sid.cast_mut().cast());
        }
        let mut acl = null_mut();
        if SetEntriesInAclW(2, entries.as_ptr(), null(), &mut acl) != 0 {
            return Err(DENIED);
        }
        let allocation = LocalAllocation(acl.cast());
        if SetNamedSecurityInfoW(
            name.as_ptr(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION
                | DACL_SECURITY_INFORMATION
                | PROTECTED_DACL_SECURITY_INFORMATION,
            user.as_ptr().cast_mut().cast(),
            null_mut(),
            acl,
            null(),
        ) != 0
        {
            return Err(DENIED);
        }
        drop(allocation);
    }
    verify(path)
}
pub(super) fn verify(path: &Path) -> Result<()> {
    no_reparse(path)?;
    let handle = OpenOptions::new()
        .read(true)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)
        .map_err(|_| DENIED)?;
    verify_handle(&handle)
}
pub(super) fn verify_handle(file: &File) -> Result<()> {
    if file.metadata().map_err(|_| DENIED)?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(DENIED);
    }
    let user = user_sid()?;
    let system = system_sid()?;
    unsafe {
        let mut owner = null_mut();
        let mut acl = null_mut();
        let mut descriptor = null_mut();
        if GetSecurityInfo(
            file.as_raw_handle().cast(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut acl,
            null_mut(),
            &mut descriptor,
        ) != 0
        {
            return Err(DENIED);
        }
        let _allocation = LocalAllocation(descriptor);
        if owner.is_null()
            || acl.is_null()
            || IsValidAcl(acl) == 0
            || EqualSid(owner, user.as_ptr().cast_mut().cast()) == 0
        {
            return Err(DENIED);
        }
        let mut control = 0;
        let mut revision = 0;
        if GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) == 0
            || control & SE_DACL_PROTECTED == 0
        {
            return Err(DENIED);
        }
        let mut information: ACL_SIZE_INFORMATION = zeroed();
        if GetAclInformation(
            acl,
            (&mut information as *mut ACL_SIZE_INFORMATION).cast(),
            size_of::<ACL_SIZE_INFORMATION>() as u32,
            AclSizeInformation,
        ) == 0
            || information.AceCount != 2
        {
            return Err(DENIED);
        }
        let mut found_user = false;
        let mut found_system = false;
        for index in 0..information.AceCount {
            let mut raw = null_mut();
            if GetAce(acl, index, &mut raw) == 0 || raw.is_null() {
                return Err(DENIED);
            }
            let ace = &*(raw.cast::<ACCESS_ALLOWED_ACE>());
            // ACCESS_ALLOWED_ACE_TYPE is zero. Reject deny/object/callback/inherited/extra ACEs.
            if ace.Header.AceType != 0
                || ace.Header.AceSize < (size_of::<ACCESS_ALLOWED_ACE>() as u16)
                || ace.Mask != FILE_ALL_ACCESS
            {
                return Err(DENIED);
            }
            let allowed_flags = if file.metadata().map_err(|_| DENIED)?.is_dir() {
                OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
            } else {
                0
            };
            if ace.Header.AceFlags as u32 != allowed_flags {
                return Err(DENIED);
            }
            let sid = std::ptr::addr_of!(ace.SidStart).cast_mut().cast();
            if IsValidSid(sid) == 0 {
                return Err(DENIED);
            }
            if EqualSid(sid, user.as_ptr().cast_mut().cast()) != 0 {
                if found_user {
                    return Err(DENIED);
                }
                found_user = true;
            } else if EqualSid(sid, system.as_ptr().cast_mut().cast()) != 0 {
                if found_system {
                    return Err(DENIED);
                }
                found_system = true;
            } else {
                return Err(DENIED);
            }
        }
        if !found_user || !found_system {
            return Err(DENIED);
        }
    }
    Ok(())
}
/// Spawn suspended, enroll in a non-inheritable job, then resume. No child can escape the assignment race.
pub(crate) struct EngineJob {
    _handle: Handle,
}
impl EngineJob {
    pub(crate) fn attach(child: &Child) -> Result<Self> {
        unsafe {
            let raw = CreateJobObjectW(null(), null());
            if raw.is_null() {
                return Err("engine_job_unavailable");
            }
            let job = Handle(raw);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
                || AssignProcessToJobObject(job.0, child.as_raw_handle().cast()) == 0
            {
                return Err("engine_job_unavailable");
            }
            let snapshot = Handle(CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0));
            if snapshot.0 == INVALID_HANDLE_VALUE {
                return Err("engine_job_unavailable");
            }
            let mut entry: THREADENTRY32 = zeroed();
            entry.dwSize = size_of::<THREADENTRY32>() as u32;
            let mut next = Thread32First(snapshot.0, &mut entry);
            while next != 0 {
                if entry.th32OwnerProcessID == child.id() {
                    let thread = Handle(OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID));
                    if thread.0.is_null() || ResumeThread(thread.0) != 1 {
                        return Err("engine_job_unavailable");
                    }
                    return Ok(Self { _handle: job });
                }
                next = Thread32Next(snapshot.0, &mut entry);
            }
            Err("engine_job_unavailable")
        }
    }
}
