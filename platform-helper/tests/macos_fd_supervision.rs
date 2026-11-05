//! Real native helper boundary probe; no desktop action or credential access.
#![cfg(all(target_os = "macos", target_arch = "aarch64"))]
use gamja_platform_helper::{CapabilityStatus, PlatformHelper};
use std::{
    fs::{self, File},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    os::unix::net::UnixStream,
    path::PathBuf,
    process::Command,
};

struct Fixture(PathBuf);
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
struct Environment(Option<std::ffi::OsString>);
impl Drop for Environment {
    fn drop(&mut self) {
        match self.0.take() {
            Some(v) => std::env::set_var("GAMJA_TEST_EXCLUDED", v),
            None => std::env::remove_var("GAMJA_TEST_EXCLUDED"),
        }
    }
}
fn extra(fd: i32) -> OwnedFd {
    let copy = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 200) };
    assert!(copy >= 200);
    let owned = unsafe { OwnedFd::from_raw_fd(copy) };
    assert_eq!(unsafe { libc::fcntl(copy, libc::F_SETFD, 0) }, 0);
    owned
}
#[test]
fn helper_excludes_read_write_socket_sentinels_and_preserves_stdio() {
    let mut nonce = [0; 16];
    getrandom::fill(&mut nonce).unwrap();
    let dir = std::env::temp_dir().join(format!(
        "gamja-helper-fd-{}",
        gamja_platform_helper::protocol::hex(&nonce)
    ));
    fs::create_dir(&dir).unwrap();
    let fixture = Fixture(dir);
    let source = fixture.0.join("probe.c");
    fs::write(
        &source,
        r#"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/stat.h>
int main(void) {
  const char *s = getenv("GAMJA_TEST_EXCLUDED");
  int a, b, c;
  if (!s || sscanf(s, "%d,%d,%d", &a, &b, &c) != 3) return 1;
  int excluded[] = {a,b,c};
  for (int i=0;i<3;i++) {
    errno=0;
    if (fcntl(excluded[i],F_GETFD) != -1 || errno != EBADF) return 2;
  }
  struct stat st;
  for(int i=0;i<3;i++) {
    if (fstat(i,&st)) return 3;
    if(i<2 && !S_ISFIFO(st.st_mode)) return 4;
    if(i==2 && !S_ISCHR(st.st_mode)) return 5;
  }
  char line[1024], endpoint[256];
  if(!fgets(line,sizeof(line),stdin)) return 6;
  char *p=strstr(line,"\"endpoint\":\"");
  if(!p) return 7;
  p+=strlen("\"endpoint\":\"");
  char *e=strchr(p,'"');
  if(!e || e-p>=sizeof(endpoint)) return 8;
  memcpy(endpoint,p,e-p); endpoint[e-p]=0;
  printf("{\"version\":1,\"endpoint\":\"%s\",\"ready\":true}\n",endpoint);
  fflush(stdout);
  while(fgetc(stdin)!=EOF) {}
  return 0;
}
"#,
    )
    .unwrap();
    let executable = fixture.0.join("probe");
    assert!(Command::new("/usr/bin/cc")
        .arg(&source)
        .arg("-o")
        .arg(&executable)
        .status()
        .unwrap()
        .success());
    // Construct deliberately inheritable sentinels only after compilation.
    let input = File::open("/dev/null").unwrap();
    let output = File::options().write(true).open("/dev/null").unwrap();
    let (socket, _peer) = UnixStream::pair().unwrap();
    let sentinels = [
        extra(input.as_raw_fd()),
        extra(output.as_raw_fd()),
        extra(socket.as_raw_fd()),
    ];
    let _environment = Environment(std::env::var_os("GAMJA_TEST_EXCLUDED"));
    std::env::set_var(
        "GAMJA_TEST_EXCLUDED",
        sentinels
            .iter()
            .map(|f| f.as_raw_fd().to_string())
            .collect::<Vec<_>>()
            .join(","),
    );
    let mut helper = PlatformHelper::start(Some(&executable));
    assert_eq!(helper.capability_status(), CapabilityStatus::Available);
    assert!(helper.credentials().is_some());
    helper.stop();
    assert!(helper.credentials().is_none());
    for sentinel in sentinels {
        assert_eq!(
            unsafe { libc::fcntl(sentinel.as_raw_fd(), libc::F_GETFD) },
            0
        );
    }
}
