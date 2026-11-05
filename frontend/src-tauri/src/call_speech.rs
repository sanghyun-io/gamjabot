use serde_json::Value;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
unsafe extern "C" { fn gamja_call_speech(input: *const std::ffi::c_char) -> *mut std::ffi::c_char; }
#[tauri::command]
pub async fn call_speech(window: tauri::Window, input: Value) -> Result<Value, String> {
    if window.label() != "main" { return Err("speech_unavailable".into()); }
    let action = input.get("action").and_then(Value::as_str).ok_or("speech_unavailable")?;
    if !matches!(action, "voices" | "start" | "cancel" | "snapshot") || action == "start" && !window.is_focused().unwrap_or(false) { return Err("speech_unavailable".into()); }
    if action != "voices" && !input.get("token").and_then(Value::as_str).is_some_and(|s| s.len() == 36 && s.bytes().all(|c| c.is_ascii_hexdigit() || c == b'-')) { return Err("speech_unavailable".into()); }
    if action == "start" && !input.get("text").and_then(Value::as_str).is_some_and(|s| !s.is_empty() && s.len() <= 65536) { return Err("speech_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return tauri::async_runtime::spawn_blocking(move || {
        let json = std::ffi::CString::new(input.to_string()).map_err(|_| "speech_unavailable".to_string())?;
        let ptr = unsafe { gamja_call_speech(json.as_ptr()) };
        if ptr.is_null() { return Err("speech_unavailable".into()); }
        let bytes = unsafe { std::ffi::CStr::from_ptr(ptr).to_bytes().to_vec() }; unsafe { libc::free(ptr.cast()) };
        serde_json::from_slice(&bytes).map_err(|_| "speech_unavailable".into())
    }).await.map_err(|_| "speech_unavailable".to_string())?;
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Err("speech_unavailable".into())
}
