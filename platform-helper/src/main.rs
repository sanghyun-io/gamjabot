//! Signed native adapter executable. Its only owner is the private stdin pipe.
mod server;
use gamja_platform_helper::protocol::{Bootstrap, Ready, VERSION};
use std::io::{BufRead, Read, Write};
use std::sync::atomic::Ordering;

fn main() {
    if run().is_err() {
        std::process::exit(1);
    }
}
fn run() -> Result<(), ()> {
    let mut input = std::io::stdin().lock();
    let mut bootstrap = String::new();
    input
        .by_ref()
        .take(1024)
        .read_line(&mut bootstrap)
        .map_err(|_| ())?;
    if !bootstrap.ends_with('\n') {
        return Err(());
    }
    let bootstrap: Bootstrap = serde_json::from_str(&bootstrap).map_err(|_| ())?;
    if bootstrap.version != VERSION || bootstrap.key.len() != 64 || !bootstrap.key.is_ascii() {
        return Err(());
    }
    let mut key = [0u8; 32];
    for (index, byte) in key.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&bootstrap.key[index * 2..index * 2 + 2], 16).map_err(|_| ())?;
    }
    let mut server = server::DesktopServer::bind(bootstrap.endpoint, key).map_err(|_| ())?;
    server.start().map_err(|_| ())?;
    serde_json::to_writer(
        std::io::stdout().lock(),
        &Ready {
            version: VERSION,
            endpoint: server.endpoint().into(),
            ready: true,
        },
    )
    .map_err(|_| ())?;
    std::io::stdout().write_all(b"\n").map_err(|_| ())?;
    std::io::stdout().flush().map_err(|_| ())?;
    // No owner PID probing, OS credential request, GUI handle, or native action
    // is needed. Loss of the inherited pipe scopes helper lifetime to the core.
    let mut ignored = [0u8; 1];
    while input.read(&mut ignored).map_err(|_| ())? != 0 {}
    server.stop_signal().store(true, Ordering::SeqCst);
    server.stop();
    Ok(())
}
