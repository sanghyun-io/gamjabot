fn main() {
    match gamja_core_host::cli::run(std::env::args_os().skip(1)) {
        Ok(code) => std::process::exit(code),
        Err(code) => {
            eprintln!("{}", serde_json::json!({"code":code}));
            std::process::exit(1);
        }
    }
}
