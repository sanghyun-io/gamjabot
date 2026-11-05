#[cfg(all(target_os="macos",target_arch="aarch64"))]
unsafe extern "C" {
    fn gamja_notifications_init();
    fn gamja_notification_run(action:*const std::ffi::c_char,id:*const std::ffi::c_char,kind:*const std::ffi::c_char)->*mut std::ffi::c_char;
}
pub fn initialize() {
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    unsafe { gamja_notifications_init() }
}
fn uuid(v:&str)->bool {v.len()==36&&v.bytes().enumerate().all(|(i,c)|if [8,13,18,23].contains(&i){c==b'-'}else{c.is_ascii_hexdigit()&&(!c.is_ascii_alphabetic()||c.is_ascii_lowercase())})}
#[tauri::command]
pub async fn local_notification(window:tauri::Window,action:String,delivery_id:Option<String>,kind:Option<String>)->Result<String,String>{
    if window.label()!="main"||!matches!(action.as_str(),"status"|"request"|"deliver"|"take")||action=="request"&&!window.is_focused().unwrap_or(false)||action=="deliver"&&(!delivery_id.as_deref().is_some_and(uuid)||!matches!(kind.as_deref(),Some("completion"|"failure"|"approval")))||action!="deliver"&&(delivery_id.is_some()||kind.is_some()){return Err("notification_unavailable".into())}
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    return tauri::async_runtime::spawn_blocking(move||{
        let a=std::ffi::CString::new(action).map_err(|_|"notification_unavailable".to_string())?;
        let id=std::ffi::CString::new(delivery_id.unwrap_or_default()).map_err(|_|"notification_unavailable".to_string())?;
        let k=std::ffi::CString::new(kind.unwrap_or_default()).map_err(|_|"notification_unavailable".to_string())?;
        let raw=unsafe{gamja_notification_run(a.as_ptr(),id.as_ptr(),k.as_ptr())};if raw.is_null(){return Err("notification_unavailable".into())}
        let bytes=unsafe{std::ffi::CStr::from_ptr(raw).to_bytes().to_vec()};unsafe{libc::free(raw.cast())};String::from_utf8(bytes).map_err(|_|"notification_unavailable".into())
    }).await.map_err(|_|"notification_unavailable".to_string())?;
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
    {let _=(delivery_id,kind);Err("notification_unavailable".into())}
}
