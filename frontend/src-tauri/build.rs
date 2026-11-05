fn main() {
    const R8_MANIFEST: &str =
        "c7db6f970b456e75eb23359dfdbe79e2aa1cb16c4c72eed5649bae3a3a11b27f";
    const R8_CORE: &str =
        "b885e14bbacfdcbe7c68e4ff051db2d33ade88733d69471897fe4ed55da10a31";
    const R8_ENGINE: &str =
        "2f6b534d61ef0212248ec76b6f92b9a9e5dc9e15dae21622c8ab785fa2bfded0";
    for (name, fallback) in [
        ("GAMJA_CORE_MANIFEST_SHA256", R8_MANIFEST),
        ("GAMJA_CORE_SHA256", R8_CORE),
        ("GAMJA_ENGINE_SHA256", R8_ENGINE),
    ] {
        println!("cargo:rerun-if-env-changed={name}");
        let value = std::env::var(name).unwrap_or_else(|_| fallback.to_owned());
        assert!(
            value.len() == 64
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)),
            "invalid verified Core binding: {name}"
        );
        println!("cargo:rustc-env={name}={value}");
    }
    if std::env::var("TARGET").as_deref() == Ok("aarch64-apple-darwin")
        && std::env::var_os("TAURI_CONFIG").is_none()
    {
        // Direct source checks must not depend on legacy ARM sidecars or staged
        // browser payloads. The release recipe supplies the verified core resource
        // through tauri.core.conf.json.
        const SOURCE_CONFIG: &str =
            r#"{"bundle":{"externalBin":[],"resources":{"resources/playwright-browsers/":null}}}"#;
        std::env::set_var("TAURI_CONFIG", SOURCE_CONFIG);
        println!("cargo:rustc-env=TAURI_CONFIG={SOURCE_CONFIG}");
    }
    if std::env::var("TARGET").as_deref() == Ok("aarch64-apple-darwin") {
        println!("cargo:rerun-if-changed=src/voice/voice.m");
        println!("cargo:rerun-if-changed=src/voice/call_speech.m");
        println!("cargo:rerun-if-changed=src/notifications/notifications.m");
        cc::Build::new().file("src/voice/voice.m").file("src/voice/call_speech.m").file("src/notifications/notifications.m").flag("-fobjc-arc").flag("-fblocks").compile("gamja_voice");
        for framework in ["AppKit", "Foundation", "AVFoundation", "Speech", "UserNotifications"] { println!("cargo:rustc-link-lib=framework={framework}"); }
    }
    tauri_build::build()
}
