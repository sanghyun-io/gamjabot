//! Synthetic cross-language auth-broker wire fixture.
//!
//! This example accepts only an inherited Unix stream descriptor. It never
//! selects a native credential backend or provider transport and emits no
//! handshake, credential reference, nonce, or token on stdout/stderr.

#[cfg(unix)]
use gamja_core_host::{
    auth_broker::{serve_auth_channel, AuthBroker, UnavailableAuthTransport},
    credential::{CredentialBackend, CredentialBackendError},
};
#[cfg(unix)]
use std::{collections::HashMap, os::fd::FromRawFd, os::unix::net::UnixStream};

#[cfg(unix)]
#[derive(Default)]
struct MemoryCredentialBackend {
    items: HashMap<(String, String), Vec<u8>>,
}

#[cfg(unix)]
impl CredentialBackend for MemoryCredentialBackend {
    fn get(
        &mut self,
        service: &str,
        account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        Ok(self
            .items
            .get(&(service.to_owned(), account.to_owned()))
            .cloned())
    }

    fn put(
        &mut self,
        service: &str,
        account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError> {
        self.items
            .insert((service.to_owned(), account.to_owned()), value.to_vec());
        Ok(())
    }

    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        self.items.remove(&(service.to_owned(), account.to_owned()));
        Ok(())
    }
}

#[cfg(unix)]
fn run() -> Result<(), ()> {
    const DESCRIPTOR_ENV: &str = "GAMJA_AUTH_BROKER_FIXTURE_FD";
    let descriptor = std::env::var(DESCRIPTOR_ENV)
        .ok()
        .and_then(|value| value.parse::<i32>().ok())
        .filter(|value| *value >= 3)
        .ok_or(())?;
    std::env::remove_var(DESCRIPTOR_ENV);
    let stream = unsafe { UnixStream::from_raw_fd(descriptor) };
    let broker = AuthBroker::new(MemoryCredentialBackend::default(), UnavailableAuthTransport);
    serve_auth_channel(stream, broker)
}

#[cfg(unix)]
fn main() {
    if run().is_err() {
        std::process::exit(1);
    }
}

#[cfg(not(unix))]
fn main() {
    std::process::exit(1);
}
