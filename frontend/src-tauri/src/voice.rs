use serde_json::Value;

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
unsafe extern "C" {
    fn gamja_voice_action(action: *const std::ffi::c_char, token: *const std::ffi::c_char, language: *const std::ffi::c_char);
    fn gamja_voice_snapshot(token: *const std::ffi::c_char) -> *mut std::ffi::c_char;
}
fn valid(token: &str) -> bool { token.len() == 36 && token.bytes().all(|c| c.is_ascii_hexdigit() || c == b'-') }
fn eligible(window: &tauri::Window) -> bool { window.label() == "main" }

#[tauri::command]
pub fn voice_transport(window: tauri::Window) -> Result<&'static str, String> {
    if !eligible(&window) { return Err("voice_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))] { Ok("native") }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))] { Ok("unavailable") }
}
#[tauri::command]
pub async fn voice_action(window: tauri::Window, action: String, token: String, language: Option<String>) -> Result<(), String> {
    if !eligible(&window) || !valid(&token) || !matches!(action.as_str(), "start" | "start_call" | "check_call" | "resume_call" | "stop" | "cancel") || language.as_ref().is_some_and(|s| s.len() > 35 || !s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')) || (action == "start" || action == "start_call" || action == "resume_call") && !window.is_focused().unwrap_or(false) { return Err("voice_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return tauri::async_runtime::spawn_blocking(move || {
        let action = std::ffi::CString::new(action).map_err(|_| "voice_unavailable".to_string())?;
        let token = std::ffi::CString::new(token).map_err(|_| "voice_unavailable".to_string())?;
        let language = std::ffi::CString::new(language.unwrap_or_default()).map_err(|_| "voice_unavailable".to_string())?;
        unsafe { gamja_voice_action(action.as_ptr(), token.as_ptr(), language.as_ptr()) }; Ok(())
    }).await.map_err(|_| "voice_unavailable".to_string())?;
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Err("voice_unavailable".into())
}
#[tauri::command]
pub async fn voice_snapshot(window: tauri::Window, token: String) -> Result<Value, String> {
    if !eligible(&window) || !valid(&token) { return Err("voice_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return tauri::async_runtime::spawn_blocking(move || {
        let token = std::ffi::CString::new(token).map_err(|_| "voice_unavailable".to_string())?;
        let raw = unsafe { gamja_voice_snapshot(token.as_ptr()) };
        if raw.is_null() { return Err("voice_unavailable".into()); }
        let value = unsafe { std::ffi::CStr::from_ptr(raw).to_bytes().to_vec() };
        unsafe { libc::free(raw.cast()) };
        serde_json::from_slice(&value).map_err(|_| "voice_unavailable".into())
    }).await.map_err(|_| "voice_unavailable".to_string())?;
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Err("voice_unavailable".into())
}
