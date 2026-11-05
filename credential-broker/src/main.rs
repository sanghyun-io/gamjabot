use std::process::ExitCode;

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use gamja_credential_broker::{
    run_isolated_first_write_proof, serve_broker_with_timeout, MacBindingVerifier,
    MacKeychainBackend,
};
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use std::os::fd::FromRawFd;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use std::os::unix::net::UnixStream;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use std::time::Duration;

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
const CONTROL_FD: i32 = 3;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
const ENGINE_FD: i32 = 4;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
const INTERNAL_FD: i32 = 5;

fn main() -> ExitCode {
    if run().is_ok() { ExitCode::SUCCESS } else { ExitCode::FAILURE }
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn run() -> Result<(), ()> {
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    let proof_mode = args.len() == 1 && args[0] == "--isolated-first-write-proof";
    if !args.is_empty() && !proof_mode { return Err(()); }

    let signer_anchor_sha1 = required_anchor(option_env!("GAMJA_CREDENTIAL_SIGNER_ANCHOR_SHA1"))?;
    let broker_path = std::env::current_exe().map_err(|_| ())?;
    let verifier = MacBindingVerifier::new(
        broker_path,
        signer_anchor_sha1.into(),
    ).map_err(|_| ())?;
    if proof_mode {
        let mut backend = MacKeychainBackend::new().map_err(|_| ())?;
        let receipt = run_isolated_first_write_proof(&mut backend);
        let passed = receipt.passed();
        serde_json::to_writer(std::io::stdout().lock(), &receipt).map_err(|_| ())?;
        return if passed { Ok(()) } else { Err(()) };
    }
    for descriptor in [CONTROL_FD, ENGINE_FD, INTERNAL_FD] {
        if unsafe { libc::fcntl(descriptor, libc::F_GETFD) } < 0 { return Err(()); }
        let mut socket_type: libc::c_int = 0;
        let mut size = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
        if unsafe {
            libc::getsockopt(
                descriptor,
                libc::SOL_SOCKET,
                libc::SO_TYPE,
                &mut socket_type as *mut _ as *mut _,
                &mut size,
            )
        } != 0 || socket_type != libc::SOCK_STREAM { return Err(()); }
    }
    let control = unsafe { UnixStream::from_raw_fd(CONTROL_FD) };
    let engine = unsafe { UnixStream::from_raw_fd(ENGINE_FD) };
    let internal = unsafe { UnixStream::from_raw_fd(INTERNAL_FD) };
    let backend = MacKeychainBackend::new().map_err(|_| ())?;
    serve_broker_with_timeout(
        control,
        engine,
        internal,
        backend,
        verifier,
        Duration::from_secs(10),
    )
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn required_anchor(value: Option<&'static str>) -> Result<&'static str, ()> {
    value.filter(|value| {
        value.len() == 40
            && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }).ok_or(())
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn run() -> Result<(), ()> { Err(()) }
