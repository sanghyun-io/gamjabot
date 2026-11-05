//! Windows 11 x64 native driver and current-user-only named-pipe transport.
//! Runtime acceptance on a genuine Windows install belongs to Task 13.

use super::{effect_lock_path, Driver, LeaseState, Request, Response, MAX_REQUEST, MAX_RESPONSE};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde_json::Value;
use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::os::windows::io::AsRawHandle;
use std::path::Path;
use std::process::Child;
use std::ptr::{null, null_mut};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::time::{Duration, Instant};

type Handle = *mut c_void;
const INVALID_HANDLE: Handle = -1isize as Handle;
const PIPE_NOWAIT: u32 = 0x00000001;
const PIPE_REJECT_REMOTE_CLIENTS: u32 = 0x00000008;
const ERROR_PIPE_CONNECTED: u32 = 535;
const ERROR_PIPE_LISTENING: u32 = 536;

#[repr(C)]
struct SecurityAttributes {
    length: u32,
    descriptor: *mut c_void,
    inherit: i32,
}
#[repr(C)]
struct Overlapped {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    event: Handle,
}
#[repr(C)]
struct SidAndAttributes {
    sid: *mut c_void,
    attributes: u32,
}
#[repr(C)]
struct TokenUser {
    user: SidAndAttributes,
}

#[link(name = "kernel32")]
extern "system" {
    fn GetCurrentProcess() -> Handle;
    fn CloseHandle(handle: Handle) -> i32;
    fn GetLastError() -> u32;
    fn LocalFree(memory: Handle) -> Handle;
    fn CreateNamedPipeW(
        name: *const u16,
        open_mode: u32,
        pipe_mode: u32,
        max_instances: u32,
        outbound_size: u32,
        inbound_size: u32,
        timeout: u32,
        security: *const SecurityAttributes,
    ) -> Handle;
    fn ConnectNamedPipe(pipe: Handle, overlapped: *mut Overlapped) -> i32;
    fn DisconnectNamedPipe(pipe: Handle) -> i32;
    fn ReadFile(
        file: Handle,
        buffer: *mut c_void,
        length: u32,
        read: *mut u32,
        overlapped: *mut Overlapped,
    ) -> i32;
    fn WriteFile(
        file: Handle,
        buffer: *const c_void,
        length: u32,
        written: *mut u32,
        overlapped: *mut Overlapped,
    ) -> i32;
    fn LockFileEx(
        file: Handle,
        flags: u32,
        reserved: u32,
        low: u32,
        high: u32,
        overlapped: *mut Overlapped,
    ) -> i32;
}
#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: Handle, access: u32, token: *mut Handle) -> i32;
    fn GetTokenInformation(
        token: Handle,
        information_class: u32,
        information: *mut c_void,
        information_length: u32,
        return_length: *mut u32,
    ) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        text: *const u16,
        revision: u32,
        descriptor: *mut *mut c_void,
        size: *mut u32,
    ) -> i32;
}
#[link(name = "advapi32")]
extern "system" {
    fn ConvertSidToStringSidW(sid: *const c_void, text: *mut *mut u16) -> i32;
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

unsafe fn user_pipe_security() -> Result<*mut c_void, String> {
    let mut token = null_mut();
    if OpenProcessToken(GetCurrentProcess(), 0x0008, &mut token) == 0 {
        return Err("Cannot read current Windows user".into());
    }
    let mut size = 0u32;
    GetTokenInformation(token, 1, null_mut(), 0, &mut size);
    let mut buffer = vec![
        0usize;
        (size as usize + std::mem::size_of::<usize>() - 1)
            / std::mem::size_of::<usize>()
    ];
    let ok = GetTokenInformation(token, 1, buffer.as_mut_ptr().cast(), size, &mut size) != 0;
    CloseHandle(token);
    if !ok || buffer.len() * std::mem::size_of::<usize>() < std::mem::size_of::<TokenUser>() {
        return Err("Cannot read current Windows user SID".into());
    }
    let user = &*(buffer.as_ptr() as *const TokenUser);
    let mut sid_text = null_mut();
    if ConvertSidToStringSidW(user.user.sid, &mut sid_text) == 0 {
        return Err("Cannot format current Windows user SID".into());
    }
    let mut length = 0usize;
    while *sid_text.add(length) != 0 {
        length += 1;
    }
    let sid = String::from_utf16_lossy(std::slice::from_raw_parts(sid_text, length));
    LocalFree(sid_text.cast());
    // Explicit user + SYSTEM DACL. Default named-pipe DACL can admit Everyone.
    let sddl = wide(&format!("D:P(A;;GA;;;{sid})(A;;GA;;;SY)"));
    let mut descriptor = null_mut();
    if ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl.as_ptr(),
        1,
        &mut descriptor,
        null_mut(),
    ) == 0
    {
        return Err("Cannot create user-restricted pipe DACL".into());
    }
    Ok(descriptor)
}

pub fn serve(
    endpoint: &str,
    key: [u8; 32],
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Child>>,
    app: tauri::AppHandle,
) {
    let mut state = LeaseState::with_lock_path(effect_lock_path());
    state.on_native_failure = Some(Arc::new(move || {
        let _ = crate::reporting::record_typed_failure(
            &app,
            crate::reporting::Component::Native,
            crate::reporting::Stage::Tool,
            crate::reporting::Code::NativeActionFailed,
        );
    }));
    let driver = WindowsDriver;
    let name = wide(endpoint);
    while !should_stop(&stop, &child) {
        let Ok(descriptor) = (unsafe { user_pipe_security() }) else {
            break;
        };
        let security = SecurityAttributes {
            length: std::mem::size_of::<SecurityAttributes>() as u32,
            descriptor,
            inherit: 0,
        };
        let pipe = unsafe {
            CreateNamedPipeW(
                name.as_ptr(),
                0x00000003 | 0x00080000,
                PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                64 * 1024,
                64 * 1024,
                0,
                &security,
            )
        };
        unsafe {
            LocalFree(descriptor);
        }
        if pipe == INVALID_HANDLE {
            break;
        }
        let connected = loop {
            if should_stop(&stop, &child) {
                break false;
            }
            let result = unsafe { ConnectNamedPipe(pipe, null_mut()) };
            let error = if result == 0 {
                unsafe { GetLastError() }
            } else {
                0
            };
            match connect_progress(result != 0, error) {
                ConnectProgress::Connected => break true,
                ConnectProgress::Failed => break false,
                ConnectProgress::Waiting => thread::sleep(Duration::from_millis(50)),
            }
        };
        if connected {
            let response = (|| {
                let mut header = [0u8; 4];
                read_exact(pipe, &mut header, &stop, &child)?;
                let size = u32::from_be_bytes(header) as usize;
                if size > MAX_REQUEST {
                    return Err("Native frame too large".into());
                }
                let mut input = vec![0u8; size];
                read_exact(pipe, &mut input, &stop, &child)?;
                if should_stop(&stop, &child) {
                    return Ok(Response::fail(
                        "backend_stopped",
                        "Owned backend stopped before desktop dispatch.",
                    ));
                }
                state.dispatch(&key, &input, &driver)
            })()
            .unwrap_or_else(|_| Response::fail("native_request_denied", "Native request denied."));
            if let Ok(bytes) = serde_json::to_vec(&response) {
                if bytes.len() <= MAX_RESPONSE {
                    let _ = write_all(pipe, &(bytes.len() as u32).to_be_bytes(), &stop, &child);
                    let _ = write_all(pipe, &bytes, &stop, &child);
                }
            }
            unsafe {
                DisconnectNamedPipe(pipe);
            }
        }
        unsafe {
            CloseHandle(pipe);
        }
    }
    // A synchronous native effect finishes before state drops its host lock.
    drop(state);
}

#[derive(PartialEq, Eq, Debug)]
enum ConnectProgress {
    Waiting,
    Connected,
    Failed,
}

fn connect_progress(success: bool, error: u32) -> ConnectProgress {
    // In PIPE_NOWAIT mode, the first successful call only puts a new pipe in
    // listening state. A subsequent ERROR_PIPE_CONNECTED confirms a client.
    if !success && error == ERROR_PIPE_CONNECTED {
        ConnectProgress::Connected
    } else if success || error == ERROR_PIPE_LISTENING {
        ConnectProgress::Waiting
    } else {
        ConnectProgress::Failed
    }
}

fn should_stop(stop: &AtomicBool, child: &Mutex<Child>) -> bool {
    stop.load(Ordering::SeqCst)
        || child.lock().map_or(true, |mut owned| {
            owned.try_wait().map_or(true, |status| status.is_some())
        })
}

fn read_exact(
    pipe: Handle,
    buffer: &mut [u8],
    stop: &AtomicBool,
    child: &Mutex<Child>,
) -> Result<(), String> {
    let mut offset = 0;
    let deadline = Instant::now() + Duration::from_secs(12);
    while offset < buffer.len() {
        if should_stop(stop, child) {
            return Err("Owned backend stopped during native pipe read".into());
        }
        let mut amount = 0u32;
        let ok = unsafe {
            ReadFile(
                pipe,
                buffer[offset..].as_mut_ptr().cast(),
                (buffer.len() - offset) as u32,
                &mut amount,
                null_mut(),
            )
        };
        if ok == 0 || amount == 0 {
            if unsafe { GetLastError() } == 232 && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
                continue;
            }
            return Err("Native pipe read failed".into());
        }
        offset += amount as usize;
    }
    Ok(())
}
fn write_all(
    pipe: Handle,
    buffer: &[u8],
    stop: &AtomicBool,
    child: &Mutex<Child>,
) -> Result<(), String> {
    let mut offset = 0;
    let deadline = Instant::now() + Duration::from_secs(12);
    while offset < buffer.len() {
        if should_stop(stop, child) {
            return Err("Owned backend stopped during native pipe write".into());
        }
        let mut amount = 0u32;
        let ok = unsafe {
            WriteFile(
                pipe,
                buffer[offset..].as_ptr().cast(),
                (buffer.len() - offset) as u32,
                &mut amount,
                null_mut(),
            )
        };
        if ok == 0 || amount == 0 {
            if unsafe { GetLastError() } == 232 && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
                continue;
            }
            return Err("Native pipe write failed".into());
        }
        offset += amount as usize;
    }
    Ok(())
}

#[cfg(test)]
mod pipe_control_tests {
    use super::*;
    use std::io::Write;
    use std::process::Command;
    use std::sync::mpsc;

    fn endpoint() -> String {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        format!(
            r"\\.\pipe\gamja-control-test-{}-{nonce}",
            std::process::id()
        )
    }

    fn live_child() -> Arc<Mutex<Child>> {
        Arc::new(Mutex::new(
            Command::new("cmd")
                .args(["/C", "ping 127.0.0.1 -n 30 > NUL"])
                .spawn()
                .expect("spawn owned test child"),
        ))
    }

    fn spawn_server(
        endpoint: String,
        stop: Arc<AtomicBool>,
        child: Arc<Mutex<Child>>,
    ) -> mpsc::Receiver<()> {
        let (sender, receiver) = mpsc::channel();
        thread::spawn(move || {
            serve(&endpoint, [7; 32], stop, child);
            let _ = sender.send(());
        });
        receiver
    }

    #[test]
    fn nowait_initial_success_is_listening_not_a_client() {
        assert_eq!(connect_progress(true, 0), ConnectProgress::Waiting);
        assert_eq!(
            connect_progress(false, ERROR_PIPE_LISTENING),
            ConnectProgress::Waiting
        );
        assert_eq!(
            connect_progress(false, ERROR_PIPE_CONNECTED),
            ConnectProgress::Connected
        );
    }

    #[test]
    fn idle_stop_and_owned_child_death_finish_without_another_client() {
        let stop = Arc::new(AtomicBool::new(false));
        let child = live_child();
        let finished = spawn_server(endpoint(), stop.clone(), child.clone());
        assert!(finished.recv_timeout(Duration::from_millis(100)).is_err());
        stop.store(true, Ordering::SeqCst);
        finished
            .recv_timeout(Duration::from_secs(2))
            .expect("idle stop released worker");
        let _ = child.lock().unwrap().kill();
        let _ = child.lock().unwrap().wait();

        let dead_child = Arc::new(Mutex::new(
            Command::new("cmd")
                .args(["/C", "exit /B 0"])
                .spawn()
                .expect("spawn owned exiting child"),
        ));
        spawn_server(endpoint(), Arc::new(AtomicBool::new(false)), dead_child)
            .recv_timeout(Duration::from_secs(2))
            .expect("idle child death released worker");
    }

    #[test]
    fn stop_aborts_an_incomplete_authenticated_frame() {
        let endpoint = endpoint();
        let stop = Arc::new(AtomicBool::new(false));
        let child = live_child();
        let finished = spawn_server(endpoint.clone(), stop.clone(), child.clone());
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut stream = loop {
            if let Ok(stream) = OpenOptions::new().read(true).write(true).open(&endpoint) {
                break stream;
            }
            assert!(Instant::now() < deadline, "test pipe never listened");
            thread::sleep(Duration::from_millis(10));
        };
        stream.write_all(&[0, 0]).expect("write partial frame");
        stop.store(true, Ordering::SeqCst);
        finished
            .recv_timeout(Duration::from_secs(2))
            .expect("partial frame stop released worker");
        drop(stream);
        let _ = child.lock().unwrap().kill();
        let _ = child.lock().unwrap().wait();
    }
}

pub fn try_effect_lock(path: &Path) -> Result<Option<File>, String> {
    std::fs::create_dir_all(path.parent().ok_or("Missing lock directory")?)
        .map_err(|_| "Cannot create desktop lock directory")?;
    if path
        .symlink_metadata()
        .is_ok_and(|metadata| metadata.file_type().is_symlink())
    {
        return Err("Desktop lock path is a symlink".into());
    }
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(path)
        .map_err(|_| "Cannot open desktop effect lock")?;
    file.set_len(1)
        .map_err(|_| "Cannot initialize desktop effect lock")?;
    let mut overlap: Overlapped = unsafe { std::mem::zeroed() };
    let locked = unsafe {
        LockFileEx(
            file.as_raw_handle(),
            0x00000002 | 0x00000001,
            0,
            1,
            0,
            &mut overlap,
        )
    };
    if locked == 0 {
        return Ok(None);
    }
    Ok(Some(file))
}

pub struct WindowsDriver;

#[repr(C)]
struct BitmapInfoHeader {
    size: u32,
    width: i32,
    height: i32,
    planes: u16,
    bit_count: u16,
    compression: u32,
    size_image: u32,
    x_pixels_per_meter: i32,
    y_pixels_per_meter: i32,
    colors_used: u32,
    colors_important: u32,
}
#[repr(C)]
struct BitmapInfo {
    header: BitmapInfoHeader,
    colors: [u32; 3],
}

#[link(name = "user32")]
extern "system" {
    fn GetSystemMetrics(index: i32) -> i32;
    fn GetDC(window: Handle) -> Handle;
    fn ReleaseDC(window: Handle, dc: Handle) -> i32;
    fn SendInput(count: u32, inputs: *const Input, size: i32) -> u32;
}
#[link(name = "gdi32")]
extern "system" {
    fn CreateCompatibleDC(dc: Handle) -> Handle;
    fn DeleteDC(dc: Handle) -> i32;
    fn CreateCompatibleBitmap(dc: Handle, width: i32, height: i32) -> Handle;
    fn SelectObject(dc: Handle, object: Handle) -> Handle;
    fn DeleteObject(object: Handle) -> i32;
    fn BitBlt(
        destination: Handle,
        x: i32,
        y: i32,
        width: i32,
        height: i32,
        source: Handle,
        source_x: i32,
        source_y: i32,
        operation: u32,
    ) -> i32;
    fn GetDIBits(
        dc: Handle,
        bitmap: Handle,
        start: u32,
        lines: u32,
        bits: *mut c_void,
        info: *mut BitmapInfo,
        usage: u32,
    ) -> i32;
}

impl Driver for WindowsDriver {
    fn execute(&self, request: &Request) -> Response {
        match request.family.as_deref() {
            Some("screenshot") => capture(),
            Some(
                "pointer_click" | "pointer_move" | "pointer_scroll" | "keyboard_text"
                | "keyboard_chord",
            ) => input(request),
            _ => Response::fail("unsupported_native_family", "Unsupported desktop action."),
        }
    }
}

fn capture() -> Response {
    unsafe {
        let (x, y, width, height) = (
            GetSystemMetrics(76),
            GetSystemMetrics(77),
            GetSystemMetrics(78),
            GetSystemMetrics(79),
        );
        if width <= 0
            || height <= 0
            || (width as usize)
                .checked_mul(height as usize)
                .map_or(true, |pixels| pixels > 25_000_000)
        {
            return Response::fail(
                "capture_too_large",
                "Desktop dimensions exceed the supported capture size.",
            );
        }
        let screen = GetDC(null_mut());
        if screen.is_null() {
            return Response::fail(
                "protected_surface",
                "Desktop capture unavailable on this Windows surface.",
            );
        }
        let memory = CreateCompatibleDC(screen);
        let bitmap = CreateCompatibleBitmap(screen, width, height);
        if memory.is_null() || bitmap.is_null() {
            if !bitmap.is_null() {
                DeleteObject(bitmap);
            }
            if !memory.is_null() {
                DeleteDC(memory);
            }
            ReleaseDC(null_mut(), screen);
            return Response::fail("capture_failed", "Could not create desktop capture buffer.");
        }
        let previous = SelectObject(memory, bitmap);
        let copied = BitBlt(
            memory,
            0,
            0,
            width,
            height,
            screen,
            x,
            y,
            0x00CC0020 | 0x40000000,
        ) != 0;
        let mut pixels = vec![0u8; width as usize * height as usize * 4];
        let mut info = BitmapInfo {
            header: BitmapInfoHeader {
                size: std::mem::size_of::<BitmapInfoHeader>() as u32,
                width,
                height: -height,
                planes: 1,
                bit_count: 32,
                compression: 0,
                size_image: 0,
                x_pixels_per_meter: 0,
                y_pixels_per_meter: 0,
                colors_used: 0,
                colors_important: 0,
            },
            colors: [0; 3],
        };
        SelectObject(memory, previous);
        let read = if copied {
            GetDIBits(
                memory,
                bitmap,
                0,
                height as u32,
                pixels.as_mut_ptr().cast(),
                &mut info,
                0,
            )
        } else {
            0
        };
        DeleteObject(bitmap);
        DeleteDC(memory);
        ReleaseDC(null_mut(), screen);
        if read != height {
            return Response::fail(
                "protected_surface",
                "Desktop capture failed or a protected surface blocked it.",
            );
        }
        for pixel in pixels.chunks_exact_mut(4) {
            pixel.swap(0, 2);
            pixel[3] = 255;
        }
        let mut png = Vec::new();
        let encoded = (|| -> Result<(), png::EncodingError> {
            let mut encoder = png::Encoder::new(&mut png, width as u32, height as u32);
            encoder.set_color(png::ColorType::Rgba);
            encoder.set_depth(png::BitDepth::Eight);
            let mut writer = encoder.write_header()?;
            writer.write_image_data(&pixels)?;
            Ok(())
        })();
        if encoded.is_err() || png.len() > 10_000_000 {
            return Response::fail(
                "capture_failed",
                "Desktop image exceeds the capture size limit.",
            );
        }
        Response {
            state: "completed",
            text: "Visible desktop captured; protected windows may be omitted.".into(),
            code: None,
            png_base64: Some(STANDARD.encode(png)),
            width: Some(width as u32),
            height: Some(height as u32),
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy)]
struct MouseInput {
    dx: i32,
    dy: i32,
    data: u32,
    flags: u32,
    time: u32,
    extra: usize,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct KeyboardInput {
    vk: u16,
    scan: u16,
    flags: u32,
    time: u32,
    extra: usize,
}
#[repr(C)]
union InputValue {
    mouse: MouseInput,
    keyboard: KeyboardInput,
}
#[repr(C)]
struct Input {
    kind: u32,
    value: InputValue,
}

fn mouse(dx: i32, dy: i32, data: u32, flags: u32) -> Input {
    Input {
        kind: 0,
        value: InputValue {
            mouse: MouseInput {
                dx,
                dy,
                data,
                flags,
                time: 0,
                extra: 0,
            },
        },
    }
}
fn keyboard(vk: u16, scan: u16, flags: u32) -> Input {
    Input {
        kind: 1,
        value: InputValue {
            keyboard: KeyboardInput {
                vk,
                scan,
                flags,
                time: 0,
                extra: 0,
            },
        },
    }
}
fn coordinate(args: &Value, key: &str) -> Option<i32> {
    args.get(key)?
        .as_i64()
        .filter(|value| (-100_000..=100_000).contains(value))
        .map(|value| value as i32)
}
fn virtual_absolute(
    x: i32,
    y: i32,
    left: i32,
    top: i32,
    width: i32,
    height: i32,
) -> Option<(i32, i32)> {
    if width < 2
        || height < 2
        || (x as i64) < left as i64
        || (y as i64) < top as i64
        || (x as i64) >= left as i64 + width as i64
        || (y as i64) >= top as i64 + height as i64
    {
        return None;
    }
    Some((
        (((x as i64 - left as i64) * 65535) / (width - 1) as i64) as i32,
        (((y as i64 - top as i64) * 65535) / (height - 1) as i64) as i32,
    ))
}

#[cfg(test)]
mod virtual_display_tests {
    use super::*;

    #[test]
    fn negative_origin_coordinates_reach_left_and_upper_monitor() {
        assert_eq!(coordinate(&serde_json::json!({"x": -500}), "x"), Some(-500));
        assert_eq!(
            virtual_absolute(-500, -200, -1000, -400, 3000, 1600),
            Some((10926, 8196))
        );
        assert_eq!(virtual_absolute(-1001, -200, -1000, -400, 3000, 1600), None);
    }
}
fn input(request: &Request) -> Response {
    let args = request.arguments.as_ref().unwrap();
    let family = request.family.as_deref().unwrap_or_default();
    let mut steps = Vec::new();
    let mut chord_codes = Vec::new();
    let mut text_units = Vec::new();
    if family.starts_with("pointer_") {
        let (Some(x), Some(y)) = (coordinate(args, "x"), coordinate(args, "y")) else {
            return Response::fail("invalid_coordinates", "Invalid pointer coordinates.");
        };
        let (left, top, width, height) = unsafe {
            (
                GetSystemMetrics(76),
                GetSystemMetrics(77),
                GetSystemMetrics(78),
                GetSystemMetrics(79),
            )
        };
        let Some((dx, dy)) = virtual_absolute(x, y, left, top, width, height) else {
            return Response::fail(
                "invalid_coordinates",
                "Pointer target is outside the visible desktop.",
            );
        };
        steps.push(mouse(dx, dy, 0, 0x0001 | 0x8000 | 0x4000));
        match family {
            "pointer_click" => {
                steps.push(mouse(0, 0, 0, 0x0002));
                steps.push(mouse(0, 0, 0, 0x0004));
            }
            "pointer_scroll" => {
                let Some(delta) = args
                    .get("delta")
                    .and_then(Value::as_i64)
                    .filter(|value| value.abs() <= 100_000)
                else {
                    return Response::fail("invalid_scroll", "Invalid scroll amount.");
                };
                steps.push(mouse(0, 0, delta as i32 as u32, 0x0800));
            }
            _ => {}
        }
    } else if family == "keyboard_text" {
        let Some(text) = args
            .get("text")
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty() && text.encode_utf16().count() <= 4096)
        else {
            return Response::fail("text_limit", "Text must contain 1–4096 UTF-16 units.");
        };
        for unit in text.encode_utf16() {
            text_units.push(unit);
            steps.push(keyboard(0, unit, 0x0004));
            steps.push(keyboard(0, unit, 0x0004 | 0x0002));
        }
    } else if family == "keyboard_chord" {
        let Some(keys) = args
            .get("keys")
            .and_then(Value::as_array)
            .filter(|keys| !keys.is_empty() && keys.len() <= 8)
        else {
            return Response::fail("invalid_chord", "Invalid keyboard chord.");
        };
        for key in keys {
            let Some(code) = key.as_str().and_then(vk) else {
                return Response::fail(
                    "unsupported_key",
                    "A key in this chord is unsupported on Windows.",
                );
            };
            chord_codes.push(code);
        }
        for &code in &chord_codes {
            steps.push(keyboard(code, 0, 0));
        }
        for &code in chord_codes.iter().rev() {
            steps.push(keyboard(code, 0, 0x0002));
        }
    }
    if steps.is_empty() {
        return Response::fail("unsupported_native_family", "Unsupported desktop action.");
    }
    // SendInput is constrained by UIPI and cannot act on UAC/secure desktop.
    // A partial send has an unknown effect and must never be replayed.
    let sent = unsafe {
        SendInput(
            steps.len() as u32,
            steps.as_ptr(),
            std::mem::size_of::<Input>() as i32,
        )
    };
    if sent != steps.len() as u32 {
        let mut release = Vec::new();
        if family == "pointer_click" && sent >= 2 {
            release.push(mouse(0, 0, 0, 0x0004));
        } else if family == "keyboard_text" && sent % 2 == 1 {
            release.push(keyboard(0, text_units[sent as usize / 2], 0x0004 | 0x0002));
        } else if family == "keyboard_chord" {
            for &code in chord_codes[..(sent as usize).min(chord_codes.len())]
                .iter()
                .rev()
            {
                release.push(keyboard(code, 0, 0x0002));
            }
        }
        if !release.is_empty() {
            unsafe {
                SendInput(
                    release.len() as u32,
                    release.as_ptr(),
                    std::mem::size_of::<Input>() as i32,
                );
            }
        }
        return Response::uncertain("input_blocked", "Windows blocked or partly sent input. Elevated, locked, UAC, and secure desktop surfaces cannot be controlled.");
    }
    Response::ok("Desktop input sent.")
}

fn vk(key: &str) -> Option<u16> {
    let upper = key.to_ascii_uppercase();
    let code = match upper.as_str() {
        "CTRL" | "CONTROL" => 0x11,
        "ALT" | "OPTION" => 0x12,
        "SHIFT" => 0x10,
        "META" | "WIN" | "WINDOWS" | "CMD" => 0x5B,
        "ENTER" | "RETURN" => 0x0D,
        "ESC" | "ESCAPE" => 0x1B,
        "TAB" => 0x09,
        "SPACE" => 0x20,
        "BACKSPACE" => 0x08,
        "DELETE" => 0x2E,
        "LEFT" => 0x25,
        "UP" => 0x26,
        "RIGHT" => 0x27,
        "DOWN" => 0x28,
        _ if upper.len() == 1 && upper.bytes().all(|byte| byte.is_ascii_alphanumeric()) => {
            upper.as_bytes()[0] as u16
        }
        _ => return None,
    };
    Some(code)
}
