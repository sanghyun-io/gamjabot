pub mod protocol;
pub use protocol::*;
#[cfg(unix)]
pub mod auth_broker;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod broker_runtime;
pub mod capability;
pub mod cli;
pub mod control;
#[cfg(unix)]
pub mod credential;
pub mod platform;
pub mod provider_transport;
pub mod reporting;
pub mod runtime;
pub mod runtime_install;
#[cfg(unix)]
pub mod upgrade_login;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod stable_broker;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub(crate) mod vault;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub(crate) mod vault_browser;
#[cfg(all(target_os="macos",target_arch="aarch64"))]
pub(crate) mod vault_metadata_diagnostic;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub(crate) mod vault_proxy_pump;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub(crate) mod vault_profile;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod vault_card;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub(crate) mod vault_csv;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod vault_csv_import;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod vault_control;

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod macos_spawn;

// Errors are stable allowlisted codes. Raw OS errors/process output never cross the owner boundary.
pub type Result<T> = std::result::Result<T, &'static str>;

#[cfg(any(windows, test))]
pub mod ffi;

pub mod mcp_transport;

#[cfg(unix)]
pub(crate) mod speech_memo;

#[cfg(unix)]
pub(crate) mod notion_oauth;

pub(crate) mod mcp_schema;

#[cfg(all(target_os="macos",target_arch="aarch64"))]
pub(crate) mod slack_transport;
