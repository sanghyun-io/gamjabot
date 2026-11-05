//! Narrow caller-buffer endpoint ABI; never writes a capability to a transport.

use crate::{capability, MAX_CONTROL_BYTES};
use std::{
    mem,
    panic::{catch_unwind, AssertUnwindSafe},
    path::PathBuf,
    slice,
};

const MAX_PATH_UNITS: usize = 32767;
const INVALID_REQUEST: u32 = 1;
const UNAVAILABLE: u32 = 2;
const INVALID_ENDPOINT: u32 = 3;
const BUFFER_TOO_SMALL: u32 = 4;
const READER_UNAVAILABLE: u32 = 5;

fn range(pointer: usize, length: usize) -> Option<(usize, usize)> {
    Some((pointer, pointer.checked_add(length)?))
}
fn overlaps(left: (usize, usize), right: (usize, usize)) -> bool {
    left.0 < right.1 && right.0 < left.1
}

/// Read the core's private `owner-cli.json` from an absolute app-data directory.
/// `path_units` counts UTF-16 units without a terminator; interior NUL is invalid.
/// Output is canonical versioned UTF-8 JSON followed by NUL; `written` excludes
/// NUL. Capacity must be 1..=MAX_CONTROL_BYTES+1. Codes: 0 success, 1 invalid
/// request, 2 unavailable/private validation failed, 3 invalid endpoint,
/// 4 insufficient buffer, 5 reader failure. No paths or OS errors are returned.
///
/// # Safety
/// The caller must supply live, properly aligned/readable path storage and
/// writable output/length storage of the stated sizes, without aliasing.
/// The ABI checks null, alignment, bounded lengths, arithmetic overflow and
/// overlap; it cannot prove that arbitrary non-null caller addresses are mapped.
/// Invalid pointer/length requests leave output unchanged; other errors zero it.
#[cfg_attr(windows, no_mangle)]
pub unsafe extern "C" fn gamja_core_read_endpoint(
    path: *const u16,
    path_units: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    catch_unwind(AssertUnwindSafe(|| unsafe {
        read_endpoint(path, path_units, output, capacity, written)
    }))
    .unwrap_or(READER_UNAVAILABLE)
}

unsafe fn read_endpoint(
    path: *const u16,
    path_units: usize,
    output: *mut u8,
    capacity: usize,
    written: *mut usize,
) -> u32 {
    if path.is_null()
        || output.is_null()
        || written.is_null()
        || path_units == 0
        || path_units > MAX_PATH_UNITS
        || capacity == 0
        || capacity > MAX_CONTROL_BYTES + 1
        || !(path as usize).is_multiple_of(mem::align_of::<u16>())
        || !(written as usize).is_multiple_of(mem::align_of::<usize>())
    {
        return INVALID_REQUEST;
    }
    let Some(path_range) = range(path as usize, path_units * mem::size_of::<u16>()) else {
        return INVALID_REQUEST;
    };
    let Some(output_range) = range(output as usize, capacity) else {
        return INVALID_REQUEST;
    };
    let Some(length_range) = range(written as usize, mem::size_of::<usize>()) else {
        return INVALID_REQUEST;
    };
    if overlaps(path_range, output_range)
        || overlaps(path_range, length_range)
        || overlaps(output_range, length_range)
    {
        return INVALID_REQUEST;
    }
    // Only dereference after all structural pointer checks, under the caller's
    // documented allocation/lifetime contract.
    let buffer = unsafe { slice::from_raw_parts_mut(output, capacity) };
    buffer.fill(0);
    unsafe {
        *written = 0;
    }
    let units = unsafe { slice::from_raw_parts(path, path_units) };
    if units.contains(&0) {
        return INVALID_REQUEST;
    }
    let Ok(text) = String::from_utf16(units) else {
        return INVALID_REQUEST;
    };
    let directory = PathBuf::from(text);
    if !directory.is_absolute() {
        return INVALID_REQUEST;
    }
    let endpoint = match capability::read(&directory) {
        Ok(endpoint) => endpoint,
        Err("invalid_endpoint") => return INVALID_ENDPOINT,
        Err(_) => return UNAVAILABLE,
    };
    let Ok(payload) = serde_json::to_vec(&endpoint) else {
        return INVALID_ENDPOINT;
    };
    if payload.len() > MAX_CONTROL_BYTES {
        return INVALID_ENDPOINT;
    }
    if payload.len() >= capacity {
        return BUFFER_TOO_SMALL;
    }
    buffer[..payload.len()].copy_from_slice(&payload);
    unsafe {
        *written = payload.len();
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{platform, EndpointFile};
    use std::{fs, io::Write, path::PathBuf, ptr};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "gamja-ffi-{}-{}",
                std::process::id(),
                crate::capability::random_hex().unwrap()
            ));
            platform::secure_directory(&path).unwrap();
            Self(path)
        }
        fn path(&self) -> Vec<u16> {
            self.0.to_str().unwrap().encode_utf16().collect()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn endpoint() -> EndpointFile {
        EndpointFile {
            version: 1,
            api_url: "http://127.0.0.1:41001".into(),
            control_url: "http://127.0.0.1:41002".into(),
            capability: "a".repeat(64),
            core_version: 1,
            engine_version: 1,
        }
    }
    fn call(path: &[u16], output: &mut [u8], written: &mut usize) -> u32 {
        unsafe {
            gamja_core_read_endpoint(
                path.as_ptr(),
                path.len(),
                output.as_mut_ptr(),
                output.len(),
                written,
            )
        }
    }

    #[test]
    fn validated_endpoint_is_bounded_json_with_nul_termination() {
        let fixture = Fixture::new();
        let _published =
            crate::capability::PublishedEndpoint::publish(&fixture.0, &endpoint()).unwrap();
        let mut output = vec![0x55; 4097];
        let mut written = 0;
        assert_eq!(call(&fixture.path(), &mut output, &mut written), 0);
        assert!(written > 0 && written <= 4096);
        assert_eq!(
            serde_json::from_slice::<EndpointFile>(&output[..written]).unwrap(),
            endpoint()
        );
        assert!(output[written..].iter().all(|byte| *byte == 0));
    }

    #[test]
    fn null_misaligned_and_out_of_bound_parameters_never_touch_output() {
        let fixture = Fixture::new();
        let path = fixture.path();
        let mut output = vec![0x55; 4098];
        let mut written = 77;
        let failures = unsafe {
            [
                gamja_core_read_endpoint(
                    ptr::null(),
                    path.len(),
                    output.as_mut_ptr(),
                    4097,
                    &mut written,
                ),
                gamja_core_read_endpoint(path.as_ptr(), 0, output.as_mut_ptr(), 4097, &mut written),
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    32768,
                    output.as_mut_ptr(),
                    4097,
                    &mut written,
                ),
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    path.len(),
                    ptr::null_mut(),
                    4097,
                    &mut written,
                ),
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    path.len(),
                    output.as_mut_ptr(),
                    0,
                    &mut written,
                ),
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    path.len(),
                    output.as_mut_ptr(),
                    4098,
                    &mut written,
                ),
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    path.len(),
                    output.as_mut_ptr(),
                    4097,
                    ptr::null_mut(),
                ),
                gamja_core_read_endpoint(
                    (path.as_ptr() as *const u8).add(1) as *const u16,
                    path.len(),
                    output.as_mut_ptr(),
                    4097,
                    &mut written,
                ),
            ]
        };
        assert!(failures.iter().all(|code| *code == 1));
        assert_eq!(written, 77);
        assert!(output.iter().all(|byte| *byte == 0x55));
    }

    #[test]
    fn pointer_range_overflow_is_rejected_before_any_dereference() {
        let path: Vec<u16> = "/".encode_utf16().collect();
        let mut output = vec![0x55; 4097];
        let mut written = 77;
        let bad_path = (usize::MAX - 1) as *const u16;
        let bad_output = usize::MAX as *mut u8;
        let bad_written = (usize::MAX - (mem::align_of::<usize>() - 1)) as *mut usize;
        let results = unsafe {
            [
                gamja_core_read_endpoint(bad_path, 2, output.as_mut_ptr(), 4097, &mut written),
                gamja_core_read_endpoint(path.as_ptr(), path.len(), bad_output, 4097, &mut written),
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    path.len(),
                    output.as_mut_ptr(),
                    4097,
                    bad_written,
                ),
            ]
        };
        assert!(results.iter().all(|code| *code == 1));
        assert_eq!(written, 77);
        assert!(output.iter().all(|byte| *byte == 0x55));
    }

    #[test]
    fn overlapping_output_or_length_pointer_is_rejected() {
        let path: Vec<u16> = vec![47; 2050];
        let mut written = 77;
        assert_eq!(
            unsafe {
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    1,
                    path.as_ptr() as *mut u8,
                    4097,
                    &mut written,
                )
            },
            1
        );
        assert_eq!(written, 77);
        let mut output = vec![0x5555555555555555usize; 513];
        assert_eq!(
            unsafe {
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    1,
                    output.as_mut_ptr() as *mut u8,
                    4097,
                    output.as_mut_ptr(),
                )
            },
            1
        );
        assert!(output.iter().all(|word| *word == 0x5555555555555555));
        assert_eq!(
            unsafe {
                gamja_core_read_endpoint(
                    path.as_ptr(),
                    1,
                    output.as_mut_ptr() as *mut u8,
                    4097,
                    (output.as_mut_ptr() as *mut u8).add(1) as *mut usize,
                )
            },
            1
        );
    }

    #[test]
    fn nul_bad_utf16_and_relative_directory_are_invalid_requests() {
        for path in [
            vec![47, 0, 65],
            vec![0xd800],
            "relative".encode_utf16().collect(),
        ] {
            let mut output = vec![0x55; 4097];
            let mut written = 77;
            assert_eq!(call(&path, &mut output, &mut written), 1);
            assert_eq!(written, 0);
            assert!(output.iter().all(|byte| *byte == 0));
        }
    }

    #[test]
    fn bad_security_or_missing_endpoint_is_numeric_unavailable() {
        let fixture = Fixture::new();
        let mut output = vec![0x55; 4097];
        let mut written = 77;
        assert_eq!(call(&fixture.path(), &mut output, &mut written), 2);
        assert_eq!(written, 0);
        assert!(output.iter().all(|byte| *byte == 0));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _published =
                crate::capability::PublishedEndpoint::publish(&fixture.0, &endpoint()).unwrap();
            fs::set_permissions(
                fixture.0.join("owner-cli.json"),
                fs::Permissions::from_mode(0o644),
            )
            .unwrap();
            assert_eq!(call(&fixture.path(), &mut output, &mut written), 2);
        }
    }

    #[test]
    fn malformed_endpoint_and_short_buffer_reveal_no_partial_capability() {
        let fixture = Fixture::new();
        let mut file = platform::create_private(&fixture.0.join("owner-cli.json")).unwrap();
        file.write_all(b"sentinel-secret invalid JSON").unwrap();
        drop(file);
        let mut output = vec![0x55; 4097];
        let mut written = 77;
        assert_eq!(call(&fixture.path(), &mut output, &mut written), 3);
        assert_eq!(written, 0);
        assert!(output.iter().all(|byte| *byte == 0));
        let _published =
            crate::capability::PublishedEndpoint::publish(&fixture.0, &endpoint()).unwrap();
        let mut short = vec![0x55; 1];
        assert_eq!(call(&fixture.path(), &mut short, &mut written), 4);
        assert_eq!(written, 0);
        assert_eq!(short, vec![0]);
    }
}
