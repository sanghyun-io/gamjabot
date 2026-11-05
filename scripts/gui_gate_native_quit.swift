// Source-only operator helper. Public polite Quit only; never a cleanup kill.
import AppKit
import Foundation
import Darwin

func fail() -> Never { print("{\"code\":\"gui_identity_unproven\"}"); exit(2) }
guard CommandLine.arguments.count == 6,
      let pid = Int32(CommandLine.arguments[1]), pid > 0,
      let seconds = UInt64(CommandLine.arguments[2]),
      let micros = UInt64(CommandLine.arguments[3]), micros < 1_000_000 else { fail() }
let bundle = CommandLine.arguments[4]
let executable = CommandLine.arguments[5]
func canonical(_ path: String) -> String? {
    guard let raw = realpath(path, nil) else { return nil }
    defer { free(raw) }
    return String(cString: raw)
}
func sameKernelIdentity() -> Bool {
    var info = proc_bsdinfo()
    let size = Int32(MemoryLayout<proc_bsdinfo>.size)
    return size == 136 && proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size &&
        info.pbi_pid == UInt32(pid) && info.pbi_start_tvsec == seconds && info.pbi_start_tvusec == micros
}
guard bundle.hasPrefix("/"), canonical(bundle) == bundle,
      canonical(executable) == executable, executable == bundle + "/Contents/MacOS/gamja-bot",
      sameKernelIdentity(), let app = NSRunningApplication(processIdentifier: pid),
      let url = app.bundleURL, let exe = app.executableURL,
      canonical(url.path) == bundle, canonical(exe.path) == executable,
      !app.isTerminated, sameKernelIdentity() else { fail() }
var stamp = timespec()
guard clock_gettime(CLOCK_UPTIME_RAW, &stamp) == 0 else { fail() }
let requestTime = UInt64(stamp.tv_sec) * 1_000_000_000 + UInt64(stamp.tv_nsec)
let requested = app.terminate()
let receipt: [String: Any] = ["requested": requested, "request_monotonic_ns": requestTime,
                            "launch_date_available": app.launchDate != nil]
let encoded = try JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys])
print(String(data: encoded, encoding: .utf8)!)
exit(requested ? 0 : 3)
