//! Thin GUI caller for the canonical Core runtime installation authority.

use gamja_core_host::runtime_install::{
    broker_availability as shared_broker_availability, expected_installed, install_runtime,
    installed_executable, validate_runtime, BrokerAvailability, RuntimePins, RuntimeScope,
};
use std::path::{Path, PathBuf};

pub const MANIFEST_SHA256: &str = env!("GAMJA_CORE_MANIFEST_SHA256");
pub const CORE_SHA256: &str = env!("GAMJA_CORE_SHA256");
pub const ENGINE_SHA256: &str = env!("GAMJA_ENGINE_SHA256");

fn pins() -> Result<RuntimePins, &'static str> {
    RuntimePins::new(MANIFEST_SHA256, CORE_SHA256, ENGINE_SHA256)
}

pub fn ensure(embedded: &Path) -> Result<PathBuf, &'static str> {
    let pins = pins()?;
    let expected = expected_installed()?;
    install_runtime(embedded, &pins, expected)?;
    installed_executable()
}

pub fn verify_executable(executable: &Path) -> Result<(), &'static str> {
    if executable != installed_executable()? {
        return Err("core_runtime_mismatch");
    }
    validate_runtime(RuntimeScope::InstalledForCurrentUser, &pins()?).map(|_| ())
}

#[allow(dead_code)]
pub fn broker_availability() -> BrokerAvailability {
    shared_broker_availability()
}
