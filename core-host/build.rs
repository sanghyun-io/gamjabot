use std::{env, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-changed=src/upgrade_native.c");
    println!("cargo:rerun-if-changed=src/speech_memo_native.m");
    if env::var("TARGET").as_deref() != Ok("aarch64-apple-darwin") {
        return;
    }
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("missing build directory"));
    let object = out.join("upgrade_native.o");
    let archive = out.join("libgamja_upgrade_native.a");
    assert!(Command::new("/usr/bin/xcrun")
        .args([
            "clang",
            "-arch",
            "arm64",
            "-c",
            "-Wall",
            "-Wextra",
            "-Werror",
            "src/upgrade_native.c",
            "-o"
        ])
        .arg(&object)
        .status()
        .expect("macOS SDK compiler unavailable")
        .success());
    assert!(Command::new("/usr/bin/ar")
        .arg("rcs")
        .arg(&archive)
        .arg(&object)
        .status()
        .expect("archive builder unavailable")
        .success());
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=gamja_upgrade_native");
    println!("cargo:rustc-link-lib=sqlite3");
    println!("cargo:rustc-link-lib=framework=Security");
    println!("cargo:rustc-link-lib=framework=CoreFoundation");
    let speech_object = out.join("speech_memo_native.o");
    let speech_archive = out.join("libgamja_speech_memo.a");
    assert!(Command::new("/usr/bin/xcrun").args(["clang", "-arch", "arm64", "-fobjc-arc", "-fblocks", "-c", "src/speech_memo_native.m", "-o"]).arg(&speech_object).status().expect("macOS SDK compiler unavailable").success());
    assert!(Command::new("/usr/bin/ar").arg("rcs").arg(&speech_archive).arg(&speech_object).status().expect("archive builder unavailable").success());
    println!("cargo:rustc-link-lib=static=gamja_speech_memo");
    println!("cargo:rustc-link-lib=framework=Foundation");
    println!("cargo:rustc-link-lib=framework=AVFoundation");
    println!("cargo:rustc-link-lib=framework=AVFAudio");
    println!("cargo:rustc-link-lib=objc");
}
