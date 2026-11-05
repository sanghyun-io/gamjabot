use super::{Driver, Request, Response};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use gamja_platform_helper::protocol::{PermissionState, PermissionStatus, SettingsPanel};
use serde_json::Value;
use std::ffi::c_void;

pub struct MacDriver;

#[repr(C)]
#[derive(Clone, Copy)]
struct Point {
    x: f64,
    y: f64,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct Size {
    width: f64,
    height: f64,
}
#[repr(C)]
#[derive(Clone, Copy)]
struct Rect {
    origin: Point,
    size: Size,
}

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    fn CGPreflightScreenCaptureAccess() -> bool;
    fn CGMainDisplayID() -> u32;
    fn CGDisplayBounds(display: u32) -> Rect;
    fn CGDisplayCreateImage(display: u32) -> *const c_void;
    fn CGImageGetWidth(image: *const c_void) -> usize;
    fn CGImageGetHeight(image: *const c_void) -> usize;
    fn CGColorSpaceCreateDeviceRGB() -> *const c_void;
    fn CGBitmapContextCreate(
        data: *mut c_void,
        width: usize,
        height: usize,
        bits_per_component: usize,
        bytes_per_row: usize,
        color_space: *const c_void,
        bitmap_info: u32,
    ) -> *const c_void;
    fn CGContextDrawImage(context: *const c_void, rect: Rect, image: *const c_void);
    fn CGEventCreateMouseEvent(
        source: *const c_void,
        kind: u32,
        point: Point,
        button: u32,
    ) -> *const c_void;
    fn CGEventCreateScrollWheelEvent(
        source: *const c_void,
        units: u32,
        wheels: u32,
        ...
    ) -> *const c_void;
    fn CGEventCreateKeyboardEvent(source: *const c_void, keycode: u16, down: bool)
        -> *const c_void;
    fn CGEventKeyboardSetUnicodeString(event: *const c_void, length: usize, text: *const u16);
    fn CGEventSetFlags(event: *const c_void, flags: u64);
    fn CGEventPost(tap: u32, event: *const c_void);
}
#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXIsProcessTrusted() -> bool;
}
#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFRelease(object: *const c_void);
}

impl Driver for MacDriver {
    fn permission_status(&self) -> PermissionStatus {
        PermissionStatus {
            screen_recording: if screen_recording_preflight() {
                PermissionState::Granted
            } else {
                PermissionState::NotGranted
            },
            accessibility: if unsafe { AXIsProcessTrusted() } {
                PermissionState::Granted
            } else {
                PermissionState::NotGranted
            },
        }
    }
    fn open_settings(&self, panel: SettingsPanel) -> Response {
        #[cfg(not(target_arch = "aarch64"))]
        use std::{
            process::{Command, Stdio},
            thread,
            time::{Duration, Instant},
        };
        let target = match panel {
            SettingsPanel::ScreenRecording => {
                "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
            }
            SettingsPanel::Accessibility => {
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
            }
        };
        #[cfg(target_arch = "aarch64")]
        return open_settings_native(target);
        #[cfg(not(target_arch = "aarch64"))]
        {
            let Ok(mut child) = Command::new("/usr/bin/open")
                .arg(target)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            else {
                return Response::fail(
                    "native_settings_unavailable",
                    "System Settings could not be opened.",
                );
            };
            let deadline = Instant::now() + Duration::from_millis(500);
            loop {
                match child.try_wait() {
                    Ok(Some(status)) if status.success() => {
                        return Response::ok("System Settings opened.")
                    }
                    Ok(Some(_)) => {
                        return Response::fail(
                            "native_settings_unavailable",
                            "System Settings could not be opened.",
                        )
                    }
                    Err(_) => {
                        return Response::uncertain(
                            "native_settings_unconfirmed",
                            "System Settings launch could not be confirmed.",
                        )
                    }
                    Ok(None) if Instant::now() >= deadline => {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Response::uncertain(
                            "native_settings_unconfirmed",
                            "System Settings launch could not be confirmed.",
                        );
                    }
                    _ => thread::sleep(Duration::from_millis(10)),
                }
            }
        }
    }

    fn execute(&self, request: &Request) -> Response {
        match request.family.as_deref() {
            Some("screenshot") => capture(),
            Some(
                "pointer_click" | "pointer_move" | "pointer_scroll" | "keyboard_text"
                | "keyboard_chord",
            ) => {
                if !unsafe { AXIsProcessTrusted() } {
                    return Response::fail("accessibility_denied", "Accessibility permission denied. Enable Gamja Bot in System Settings > Privacy & Security > Accessibility, then retry.");
                }
                input(request)
            }
            _ => Response::fail("unsupported_native_family", "Unsupported desktop action."),
        }
    }
}

pub(super) fn screen_recording_preflight() -> bool {
    unsafe { CGPreflightScreenCaptureAccess() }
}

fn capture() -> Response {
    if !screen_recording_preflight() {
        return Response::fail("screen_recording_denied", "Screen Recording permission denied. Enable Gamja Bot in System Settings > Privacy & Security > Screen & System Audio Recording, then relaunch the app.");
    }
    unsafe {
        let display = CGMainDisplayID();
        let image = CGDisplayCreateImage(display);
        if image.is_null() {
            return Response::fail(
                "protected_surface",
                "Screen capture is unavailable or the surface is protected.",
            );
        }
        // A Retina CGImage is larger than Quartz's logical pointer space.
        // Rasterize to display bounds so screenshot pixels can name clicks.
        let Some((width, height)) = logical_capture_size(
            CGDisplayBounds(display),
            CGImageGetWidth(image),
            CGImageGetHeight(image),
        ) else {
            CFRelease(image);
            return Response::fail(
                "capture_too_large",
                "Screen dimensions exceed the supported capture size.",
            );
        };
        let pixels = render_rgba(image, width, height);
        CFRelease(image);
        let Some(pixels) = pixels else {
            return Response::fail("capture_failed", "Could not create a screen image buffer.");
        };
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
                "Screen image could not be encoded within the size limit.",
            );
        }
        Response {
            state: "completed",
            text: "Main display captured; other displays and protected content may be omitted."
                .into(),
            code: None,
            png_base64: Some(STANDARD.encode(png)),
            width: Some(width as u32),
            height: Some(height as u32),
            permissions: None,
        }
    }
}

fn logical_capture_size(
    bounds: Rect,
    image_width: usize,
    image_height: usize,
) -> Option<(usize, usize)> {
    if !bounds.origin.x.is_finite()
        || !bounds.origin.y.is_finite()
        || !bounds.size.width.is_finite()
        || !bounds.size.height.is_finite()
        || bounds.size.width <= 0.0
        || bounds.size.height <= 0.0
        || bounds.size.width.fract() != 0.0
        || bounds.size.height.fract() != 0.0
        || image_width == 0
        || image_height == 0
        || image_width.checked_mul(image_height)? > 25_000_000
    {
        return None;
    }
    let width = bounds.size.width as usize;
    let height = bounds.size.height as usize;
    if width == 0 || height == 0 || width.checked_mul(height)? > 25_000_000 {
        return None;
    }
    Some((width, height))
}

unsafe fn render_rgba(image: *const c_void, width: usize, height: usize) -> Option<Vec<u8>> {
    let mut pixels = vec![0u8; width.checked_mul(height)?.checked_mul(4)?];
    let color_space = CGColorSpaceCreateDeviceRGB();
    if color_space.is_null() {
        return None;
    }
    // RGBA, premultiplied last, 32-bit big endian.
    let context = CGBitmapContextCreate(
        pixels.as_mut_ptr().cast(),
        width,
        height,
        8,
        width * 4,
        color_space,
        1 | (4 << 12),
    );
    if context.is_null() {
        CFRelease(color_space);
        return None;
    }
    CGContextDrawImage(
        context,
        Rect {
            origin: Point { x: 0.0, y: 0.0 },
            size: Size {
                width: width as f64,
                height: height as f64,
            },
        },
        image,
    );
    CFRelease(context);
    CFRelease(color_space);
    Some(pixels)
}

fn number(args: &Value, name: &str) -> Option<f64> {
    args.get(name)?
        .as_f64()
        .filter(|value| value.is_finite() && (-100_000.0..=100_000.0).contains(value))
}

// x/y are screenshot-local logical coordinates. Off-screen offsets can still
// address other displays after translation into Quartz global coordinates.
fn pointer_global_from_main(x: f64, y: f64, bounds: Rect) -> Option<Point> {
    if !x.is_finite()
        || !y.is_finite()
        || !(-100_000.0..=100_000.0).contains(&x)
        || !(-100_000.0..=100_000.0).contains(&y)
        || !bounds.origin.x.is_finite()
        || !bounds.origin.y.is_finite()
        || !bounds.size.width.is_finite()
        || !bounds.size.height.is_finite()
        || bounds.size.width <= 0.0
        || bounds.size.height <= 0.0
    {
        return None;
    }
    let point = Point {
        x: bounds.origin.x + x,
        y: bounds.origin.y + y,
    };
    if !point.x.is_finite() || !point.y.is_finite() {
        return None;
    }
    Some(point)
}

fn input(request: &Request) -> Response {
    let args = request.arguments.as_ref().unwrap();
    match request.family.as_deref().unwrap_or_default() {
        "pointer_click" | "pointer_move" | "pointer_scroll" => {
            let (Some(x), Some(y)) = (number(args, "x"), number(args, "y")) else {
                return Response::fail("invalid_coordinates", "Invalid pointer coordinates.");
            };
            // The screenshot is rasterized at one pixel per logical display
            // coordinate. Keep pointer-only actions independent of capture access.
            let Some(point) =
                pointer_global_from_main(x, y, unsafe { CGDisplayBounds(CGMainDisplayID()) })
            else {
                return Response::fail("invalid_coordinates", "Invalid pointer coordinates.");
            };
            let family = request.family.as_deref().unwrap();
            unsafe {
                if family == "pointer_scroll" {
                    let Some(delta) = args
                        .get("delta")
                        .and_then(Value::as_i64)
                        .filter(|delta| delta.abs() <= 100_000)
                    else {
                        return Response::fail("invalid_scroll", "Invalid scroll amount.");
                    };
                    if !post_mouse(5, point) {
                        return Response::fail("input_failed", "Could not move pointer.");
                    }
                    let event = CGEventCreateScrollWheelEvent(std::ptr::null(), 0, 1, delta as i32);
                    if event.is_null() {
                        return Response::uncertain(
                            "partial_input",
                            "Pointer moved but scroll event could not be created.",
                        );
                    }
                    CGEventPost(0, event);
                    CFRelease(event);
                } else if family == "pointer_move" {
                    if !post_mouse(5, point) {
                        return Response::fail("input_failed", "Could not move pointer.");
                    }
                } else {
                    return send_click(point, |kind, point| post_mouse(kind, point));
                }
            }
            Response::ok("Pointer action sent.")
        }
        "keyboard_text" => {
            let Some(text) = args
                .get("text")
                .and_then(Value::as_str)
                .filter(|text| !text.is_empty() && text.encode_utf16().count() <= 4096)
            else {
                return Response::fail("text_limit", "Text must contain 1–4096 UTF-16 units.");
            };
            send_text_chunks(&unicode_chunks(text), |chunk| {
                unsafe {
                    let down = CGEventCreateKeyboardEvent(std::ptr::null(), 0, true);
                    let up = CGEventCreateKeyboardEvent(std::ptr::null(), 0, false);
                    if down.is_null() || up.is_null() {
                        if !down.is_null() {
                            CFRelease(down);
                        }
                        if !up.is_null() {
                            CFRelease(up);
                        }
                        return false;
                    }
                    CGEventKeyboardSetUnicodeString(down, chunk.len(), chunk.as_ptr());
                    CGEventKeyboardSetUnicodeString(up, chunk.len(), chunk.as_ptr());
                    CGEventPost(0, down);
                    CGEventPost(0, up);
                    CFRelease(down);
                    CFRelease(up);
                }
                true
            })
        }
        "keyboard_chord" => {
            let Some(keys) = args
                .get("keys")
                .and_then(Value::as_array)
                .filter(|keys| !keys.is_empty() && keys.len() <= 8)
            else {
                return Response::fail("invalid_chord", "Invalid keyboard chord.");
            };
            let mut codes = Vec::new();
            for key in keys {
                let Some(code) = key.as_str().and_then(keycode) else {
                    return Response::fail(
                        "unsupported_key",
                        "A key in this chord is unsupported on macOS.",
                    );
                };
                codes.push(code);
            }
            unsafe {
                let mut flags = 0u64;
                let mut pressed = Vec::new();
                for &code in &codes {
                    flags |= modifier_flag(code);
                    if !post_key(code, true, flags) {
                        for &held in pressed.iter().rev() {
                            flags &= !modifier_flag(held);
                            let _ = post_key(held, false, flags);
                        }
                        return if pressed.is_empty() {
                            Response::fail("input_failed", "Could not press keyboard chord.")
                        } else {
                            Response::uncertain(
                                "partial_input",
                                "Keyboard chord was partially sent.",
                            )
                        };
                    }
                    pressed.push(code);
                }
                let mut release_failed = false;
                for &code in codes.iter().rev() {
                    flags &= !modifier_flag(code);
                    if !post_key(code, false, flags) {
                        let _ = post_key(code, false, flags);
                        release_failed = true;
                    }
                }
                if release_failed {
                    return Response::uncertain(
                        "partial_input",
                        "A keyboard chord release was uncertain.",
                    );
                }
            }
            Response::ok("Keyboard chord sent.")
        }
        _ => Response::fail("unsupported_native_family", "Unsupported desktop action."),
    }
}

fn send_click(point: Point, mut post: impl FnMut(u32, Point) -> bool) -> Response {
    if !post(1, point) {
        return Response::fail("input_failed", "Could not press pointer button.");
    }
    if !post(2, point) {
        // A down event was posted. Try to avoid leaving the button held, but
        // the action's extent is unknown even if the paired release succeeds.
        let _ = post(2, point);
        return Response::uncertain("partial_input", "Pointer button release was uncertain.");
    }
    Response::ok("Pointer action sent.")
}

fn send_text_chunks(chunks: &[Vec<u16>], mut post: impl FnMut(&[u16]) -> bool) -> Response {
    let mut posted = false;
    for chunk in chunks {
        if !post(chunk) {
            return if posted {
                Response::uncertain("partial_input", "Text was partially entered.")
            } else {
                Response::fail("input_failed", "Could not create text event.")
            };
        }
        posted = true;
    }
    Response::ok("Text input sent.")
}

fn unicode_chunks(text: &str) -> Vec<Vec<u16>> {
    let units: Vec<u16> = text.encode_utf16().collect();
    let mut chunks = Vec::new();
    let mut start = 0;
    while start < units.len() {
        let mut end = (start + 32).min(units.len());
        if end < units.len() && (0xD800..=0xDBFF).contains(&units[end - 1]) {
            end -= 1;
        }
        chunks.push(units[start..end].to_vec());
        start = end;
    }
    chunks
}

unsafe fn post_mouse(kind: u32, point: Point) -> bool {
    let event = CGEventCreateMouseEvent(std::ptr::null(), kind, point, 0);
    if event.is_null() {
        return false;
    }
    CGEventPost(0, event);
    CFRelease(event);
    true
}

unsafe fn post_key(keycode: u16, down: bool, flags: u64) -> bool {
    let event = CGEventCreateKeyboardEvent(std::ptr::null(), keycode, down);
    if event.is_null() {
        return false;
    }
    CGEventSetFlags(event, flags);
    CGEventPost(0, event);
    CFRelease(event);
    true
}

fn modifier_flag(keycode: u16) -> u64 {
    match keycode {
        55 => 1 << 20, // Command
        56 => 1 << 17, // Shift
        58 => 1 << 19, // Option
        59 => 1 << 18, // Control
        _ => 0,
    }
}

fn keycode(key: &str) -> Option<u16> {
    let upper = key.to_ascii_uppercase();
    let code = match upper.as_str() {
        "CMD" | "COMMAND" | "META" => 55,
        "SHIFT" => 56,
        "ALT" | "OPTION" => 58,
        "CTRL" | "CONTROL" => 59,
        "ENTER" | "RETURN" => 36,
        "ESC" | "ESCAPE" => 53,
        "TAB" => 48,
        "SPACE" => 49,
        "BACKSPACE" => 51,
        "DELETE" => 117,
        "LEFT" => 123,
        "RIGHT" => 124,
        "DOWN" => 125,
        "UP" => 126,
        "A" => 0,
        "B" => 11,
        "C" => 8,
        "D" => 2,
        "E" => 14,
        "F" => 3,
        "G" => 5,
        "H" => 4,
        "I" => 34,
        "J" => 38,
        "K" => 40,
        "L" => 37,
        "M" => 46,
        "N" => 45,
        "O" => 31,
        "P" => 35,
        "Q" => 12,
        "R" => 15,
        "S" => 1,
        "T" => 17,
        "U" => 32,
        "V" => 9,
        "W" => 13,
        "X" => 7,
        "Y" => 16,
        "Z" => 6,
        "0" => 29,
        "1" => 18,
        "2" => 19,
        "3" => 20,
        "4" => 21,
        "5" => 23,
        "6" => 22,
        "7" => 26,
        "8" => 28,
        "9" => 25,
        _ => return None,
    };
    Some(code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGDataProviderCreateWithData(
            info: *const c_void,
            data: *const c_void,
            size: usize,
            release: *const c_void,
        ) -> *const c_void;
        fn CGImageCreate(
            width: usize,
            height: usize,
            bits_per_component: usize,
            bits_per_pixel: usize,
            bytes_per_row: usize,
            color_space: *const c_void,
            bitmap_info: u32,
            provider: *const c_void,
            decode: *const c_void,
            should_interpolate: bool,
            intent: u32,
        ) -> *const c_void;
    }

    #[test]
    fn screenshot_uses_logical_display_pixels_at_one_and_two_times_backing() {
        let bounds = Rect {
            origin: Point { x: 0.0, y: 0.0 },
            size: Size {
                width: 1440.0,
                height: 900.0,
            },
        };
        assert_eq!(logical_capture_size(bounds, 1440, 900), Some((1440, 900)));
        assert_eq!(logical_capture_size(bounds, 2880, 1800), Some((1440, 900)));
    }

    #[test]
    fn screenshot_rejects_invalid_display_geometry() {
        let mut bounds = Rect {
            origin: Point { x: 0.0, y: 0.0 },
            size: Size {
                width: 1440.0,
                height: 900.0,
            },
        };
        assert_eq!(logical_capture_size(bounds, 0, 900), None);
        bounds.size.width = f64::NAN;
        assert_eq!(logical_capture_size(bounds, 2880, 1800), None);
        bounds.size.width = 1440.0;
        bounds.origin.y = f64::INFINITY;
        assert_eq!(logical_capture_size(bounds, 2880, 1800), None);
    }

    #[test]
    fn pointer_coordinates_follow_main_display_origin_without_clamping() {
        let bounds = Rect {
            origin: Point {
                x: -500.0,
                y: 100.0,
            },
            size: Size {
                width: 1440.0,
                height: 900.0,
            },
        };
        let point = pointer_global_from_main(400.0, 250.0, bounds).unwrap();
        assert_eq!((point.x, point.y), (-100.0, 350.0));
        let outside = pointer_global_from_main(1800.0, -20.0, bounds).unwrap();
        assert_eq!((outside.x, outside.y), (1300.0, 80.0));
        assert!(pointer_global_from_main(f64::NAN, 0.0, bounds).is_none());
        assert!(pointer_global_from_main(100_001.0, 0.0, bounds).is_none());
    }

    #[test]
    fn downsampled_synthetic_image_keeps_corner_colors_and_orientation() {
        unsafe {
            let mut source = vec![0u8; 4 * 4 * 4];
            for y in 0..4 {
                for x in 0..4 {
                    let color = match (x < 2, y < 2) {
                        (true, true) => [255, 0, 0, 255],
                        (false, true) => [0, 255, 0, 255],
                        (true, false) => [0, 0, 255, 255],
                        (false, false) => [255, 255, 0, 255],
                    };
                    source[(y * 4 + x) * 4..(y * 4 + x + 1) * 4].copy_from_slice(&color);
                }
            }
            let color_space = CGColorSpaceCreateDeviceRGB();
            let provider = CGDataProviderCreateWithData(
                std::ptr::null(),
                source.as_ptr().cast(),
                source.len(),
                std::ptr::null(),
            );
            assert!(!provider.is_null());
            let image = CGImageCreate(
                4,
                4,
                8,
                32,
                16,
                color_space,
                1 | (4 << 12),
                provider,
                std::ptr::null(),
                false,
                0,
            );
            assert!(!image.is_null());
            let full_size = render_rgba(image, 4, 4).unwrap();
            assert_eq!(&full_size[0..4], &[255, 0, 0, 255]);
            assert_eq!(&full_size[12..16], &[0, 255, 0, 255]);
            assert_eq!(&full_size[48..52], &[0, 0, 255, 255]);
            assert_eq!(&full_size[60..64], &[255, 255, 0, 255]);
            let output = render_rgba(image, 2, 2).unwrap();
            assert_eq!(output.len(), 16);
            assert_eq!(&output[0..4], &[255, 0, 0, 255]);
            assert_eq!(&output[4..8], &[0, 255, 0, 255]);
            assert_eq!(&output[8..12], &[0, 0, 255, 255]);
            assert_eq!(&output[12..16], &[255, 255, 0, 255]);
            CFRelease(image);
            CFRelease(provider);
            CFRelease(color_space);
        }
    }

    #[test]
    fn unicode_chunks_keep_surrogate_pairs_together() {
        let chunks = unicode_chunks(&format!("{}😀", "a".repeat(31)));
        assert_eq!(chunks.iter().map(Vec::len).collect::<Vec<_>>(), vec![31, 2]);
        assert_eq!(
            chunks.concat(),
            "a".repeat(31)
                .chars()
                .chain("😀".chars())
                .collect::<String>()
                .encode_utf16()
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn chord_modifier_flags_match_command_and_shift() {
        assert_eq!(modifier_flag(55), 1 << 20);
        assert_eq!(modifier_flag(56), 1 << 17);
        assert_eq!(modifier_flag(0), 0);
    }

    #[test]
    fn click_release_creation_failure_is_uncertain_and_retries_button_up() {
        let mut calls = Vec::new();
        let mut failed_up = false;
        let response = send_click(Point { x: 1.0, y: 1.0 }, |kind, _| {
            calls.push(kind);
            if kind == 2 && !failed_up {
                failed_up = true;
                false
            } else {
                true
            }
        });
        assert_eq!(response.state, "uncertain");
        assert_eq!(calls, vec![1, 2, 2]);
    }

    #[test]
    fn later_text_chunk_creation_failure_is_uncertain() {
        let chunks = unicode_chunks(&"x".repeat(40));
        let mut calls = 0;
        let response = send_text_chunks(&chunks, |_| {
            calls += 1;
            calls == 1
        });
        assert_eq!(response.state, "uncertain");
        assert_eq!(calls, 2);
    }
}

#[cfg(target_arch = "aarch64")]
fn open_settings_native(target: &str) -> Response {
    use gamja_platform_helper::macos_spawn::{inherited_environment, OwnedChild, SpawnError};
    use std::{fs::File, os::fd::AsRawFd, time::Duration};
    let child = (|| {
        let input = File::open("/dev/null").map_err(|_| ())?;
        let output = File::options()
            .write(true)
            .open("/dev/null")
            .map_err(|_| ())?;
        let error = File::options()
            .write(true)
            .open("/dev/null")
            .map_err(|_| ())?;
        OwnedChild::spawn(
            "/usr/bin/open",
            &[target],
            inherited_environment().map_err(|_| ())?,
            &[
                (input.as_raw_fd(), 0),
                (output.as_raw_fd(), 1),
                (error.as_raw_fd(), 2),
            ],
            Duration::from_millis(500),
        )
        .map_err(|_| ())
    })();
    let Ok(mut child) = child else {
        return Response::fail(
            "native_settings_unavailable",
            "System Settings could not be opened.",
        );
    };
    match child.wait_timeout(Duration::from_millis(500)) {
        Ok(status) if status.success() => Response::ok("System Settings opened."),
        Ok(_) => Response::fail(
            "native_settings_unavailable",
            "System Settings could not be opened.",
        ),
        Err(SpawnError::Timeout) => Response::uncertain(
            "native_settings_unconfirmed",
            "System Settings launch could not be confirmed.",
        ),
        Err(_) => Response::uncertain(
            "native_settings_unconfirmed",
            "System Settings launch could not be confirmed.",
        ),
    }
}
