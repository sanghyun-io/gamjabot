//! Windows owned-helper cleanup; reused from the standalone core job adapter.
use std::{
    mem::{size_of, zeroed},
    os::windows::io::AsRawHandle,
    process::Child,
    ptr::null,
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE},
    System::{Diagnostics::ToolHelp::*, JobObjects::*, Threading::*},
};
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
/// Spawn suspended, enroll in a non-inheritable job, then resume. No child can escape the assignment race.
pub(crate) struct HelperJob {
    _handle: Handle,
}
impl HelperJob {
    pub(crate) fn attach(child: &Child) -> Result<Self, &'static str> {
        unsafe {
            let raw = CreateJobObjectW(null(), null());
            if raw.is_null() {
                return Err("native_helper_start_failed");
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
                return Err("native_helper_start_failed");
            }
            let snapshot = Handle(CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0));
            if snapshot.0 == INVALID_HANDLE_VALUE {
                return Err("native_helper_start_failed");
            }
            let mut entry: THREADENTRY32 = zeroed();
            entry.dwSize = size_of::<THREADENTRY32>() as u32;
            let mut next = Thread32First(snapshot.0, &mut entry);
            while next != 0 {
                if entry.th32OwnerProcessID == child.id() {
                    let thread = Handle(OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID));
                    if thread.0.is_null() || ResumeThread(thread.0) != 1 {
                        return Err("native_helper_start_failed");
                    }
                    return Ok(Self { _handle: job });
                }
                next = Thread32Next(snapshot.0, &mut entry);
            }
            Err("native_helper_start_failed")
        }
    }
}
