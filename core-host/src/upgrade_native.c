/* macOS SDK metadata only: no Security credential APIs are called here. */
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <pwd.h>
#include <Security/Security.h>
#include <CoreFoundation/CoreFoundation.h>

int gamja_upgrade_process(int pid, uint64_t *sec, uint64_t *usec, int *parent) {
    struct proc_bsdinfo b = {0};
    if (pid <= 1 || !sec || !usec || !parent ||
        proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &b, sizeof b) != sizeof b ||
        b.pbi_pid != (uint32_t)pid || b.pbi_uid != geteuid() ||
        b.pbi_ruid != getuid() || (b.pbi_flags & (PROC_FLAG_INEXIT | PROC_FLAG_TRACED))) return 1;
    *sec = b.pbi_start_tvsec; *usec = b.pbi_start_tvusec; *parent = b.pbi_ppid;
    return 0;
}

int gamja_upgrade_file(int pid, uint64_t device, uint64_t inode) {
    struct proc_fdinfo fds[1024] = {0};
    int n = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, fds, sizeof fds);
    if (n <= 0 || n >= (int)sizeof fds || n % sizeof(*fds)) return 1;
    for (int i = 0; i < n/(int)sizeof(*fds); ++i) {
        if (fds[i].proc_fdtype != PROX_FDTYPE_VNODE) continue;
        struct vnode_fdinfo info = {0};
        if (proc_pidfdinfo(pid, fds[i].proc_fd, PROC_PIDFDVNODEINFO, &info, sizeof info) != sizeof info) continue;
        if (info.pvi.vi_stat.vst_dev == device && info.pvi.vi_stat.vst_ino == inode) return 0;
    }
    return 7;
}

/* Verify the full established reverse tuple in expected process, not a listener. */
int gamja_upgrade_peer(int fd, int pid, uint64_t sec, uint64_t usec, int parent) {
    struct sockaddr_in local = {0}, peer = {0};
    socklen_t len = sizeof local;
    if (getsockname(fd, (struct sockaddr *)&local, &len) || len != sizeof local ||
        local.sin_family != AF_INET || local.sin_addr.s_addr != htonl(INADDR_LOOPBACK)) return 2;
    len = sizeof peer;
    if (getpeername(fd, (struct sockaddr *)&peer, &len) || len != sizeof peer ||
        peer.sin_family != AF_INET || peer.sin_addr.s_addr != htonl(INADDR_LOOPBACK)) return 2;
    uint64_t s, u; int p;
    if (gamja_upgrade_process(pid, &s, &u, &p) || s != sec || u != usec || p != parent) return 3;
    int bytes = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, NULL, 0);
    if (bytes <= 0 || bytes > 16384 * (int)sizeof(struct proc_fdinfo)) return 4;
    /* A bounded cushion handles descriptors added during enumeration. */
    int capacity = bytes + 32 * sizeof(struct proc_fdinfo);
    struct proc_fdinfo *fds = calloc(1, capacity);
    if (!fds) return 4;
    int received = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, fds, capacity);
    int matched = 0;
    if (received > 0 && received < capacity && received % sizeof(*fds) == 0) {
        for (int i = 0; i < received / (int)sizeof(*fds); ++i) {
            if (fds[i].proc_fdtype != PROX_FDTYPE_SOCKET) continue;
            struct socket_fdinfo info = {0};
            if (proc_pidfdinfo(pid, fds[i].proc_fd, PROC_PIDFDSOCKETINFO, &info, sizeof info) != sizeof info) continue;
            if (info.psi.soi_kind != SOCKINFO_TCP || info.psi.soi_family != AF_INET ||
                info.psi.soi_protocol != IPPROTO_TCP || info.psi.soi_type != SOCK_STREAM ||
                info.psi.soi_proto.pri_tcp.tcpsi_state != TSI_S_ESTABLISHED) continue;
            struct in_sockinfo *in = &info.psi.soi_proto.pri_tcp.tcpsi_ini;
            if (!(in->insi_vflag & INI_IPV4)) continue;
            if ((uint16_t)in->insi_lport == peer.sin_port && (uint16_t)in->insi_fport == local.sin_port &&
                in->insi_laddr.ina_46.i46a_addr4.s_addr == peer.sin_addr.s_addr &&
                in->insi_faddr.ina_46.i46a_addr4.s_addr == local.sin_addr.s_addr) { matched = 1; break; }
        }
    }
    free(fds);
    if (!matched) return 5;
    if (gamja_upgrade_process(pid, &s, &u, &p) || s != sec || u != usec || p != parent) return 3;
    return 0;
}

/* Live signature and executable path; no Security credential APIs are called. */
int gamja_upgrade_code(int pid, const char *path, const char *requirement) {
    if (pid <= 1 || !path || !requirement || !*requirement) return 6;
    CFNumberRef number = CFNumberCreate(NULL, kCFNumberIntType, &pid);
    CFStringRef text = CFStringCreateWithCString(NULL, requirement, kCFStringEncodingUTF8);
    const void *keys[] = {kSecGuestAttributePid}, *values[] = {number};
    CFDictionaryRef attributes = number ? CFDictionaryCreate(NULL, keys, values, 1,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks) : NULL;
    SecCodeRef code = NULL; SecRequirementRef rule = NULL; CFURLRef url = NULL, expected_url = NULL;
    SecStaticCodeRef disk = NULL;
    CFDictionaryRef live_info = NULL, disk_info = NULL;
    int result = 6;
    if (!text || !attributes || SecRequirementCreateWithString(text, kSecCSDefaultFlags, &rule) ||
        SecCodeCopyGuestWithAttributes(NULL, attributes, kSecCSDefaultFlags, &code) ||
        SecCodeCheckValidity(code, kSecCSStrictValidate, rule) ||
        SecCodeCopyPath(code, kSecCSDefaultFlags, &url)) goto done;
    char actual[PROC_PIDPATHINFO_MAXSIZE] = {0};
    if (!CFURLGetFileSystemRepresentation(url, true, (UInt8 *)actual, sizeof actual) || strcmp(actual, path)) goto done;
    expected_url = CFURLCreateFromFileSystemRepresentation(NULL, (const UInt8 *)path, strlen(path), false);
    if (!expected_url || SecStaticCodeCreateWithPath(expected_url, kSecCSDefaultFlags, &disk) ||
        SecStaticCodeCheckValidity(disk, kSecCSStrictValidate | kSecCSCheckAllArchitectures, rule) ||
        SecCodeCopySigningInformation(code, kSecCSSigningInformation, &live_info) ||
        SecCodeCopySigningInformation(disk, kSecCSSigningInformation, &disk_info)) goto done;
    CFTypeRef live_unique = CFDictionaryGetValue(live_info, kSecCodeInfoUnique);
    CFTypeRef disk_unique = CFDictionaryGetValue(disk_info, kSecCodeInfoUnique);
    if (!live_unique || !disk_unique || CFGetTypeID(live_unique) != CFDataGetTypeID() ||
        CFGetTypeID(disk_unique) != CFDataGetTypeID() || !CFEqual(live_unique, disk_unique)) goto done;
    result = 0;
done:
    if (disk_info) CFRelease(disk_info); if (live_info) CFRelease(live_info);
    if (disk) CFRelease(disk); if (expected_url) CFRelease(expected_url);
    if (url) CFRelease(url); if (code) CFRelease(code); if (rule) CFRelease(rule);
    if (attributes) CFRelease(attributes); if (text) CFRelease(text); if (number) CFRelease(number);
    return result;
}

#include <sqlite3.h>
struct gamja_upgrade_row {
    int64_t owner;
    int64_t revision;
    char reference[33];
    char hash[65];
    char expiry[65];
};
static int bounded_text(sqlite3_stmt *s, int index, char *out, size_t capacity) {
    if (sqlite3_column_type(s, index) != SQLITE_TEXT) return 0;
    int n = sqlite3_column_bytes(s, index);
    const unsigned char *p = sqlite3_column_text(s, index);
    if (!p || n <= 0 || (size_t)n >= capacity || memchr(p, 0, n)) return 0;
    memcpy(out, p, n); out[n] = 0; return 1;
}
int gamja_upgrade_row_read(const char *path, struct gamja_upgrade_row *out) {
    if (!path || !out) return 8;
    memset(out, 0, sizeof(*out));
    sqlite3 *db = NULL; sqlite3_stmt *s = NULL; int result = 8;
    if (sqlite3_open_v2(path, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, NULL) != SQLITE_OK) goto done;
    sqlite3_busy_timeout(db, 100);
    const char *sql = "SELECT r.owner_id,r.revision,r.secret_ref,r.secret_hash,r.expires_at FROM remembered_login r JOIN users u ON u.id=r.owner_id WHERE r.id=1 AND u.is_active=1 AND julianday(r.expires_at)>julianday('now') AND NOT EXISTS(SELECT 1 FROM room_agent_runs WHERE state='running') AND NOT EXISTS(SELECT 1 FROM room_triggers WHERE state IN ('running','pending')) AND NOT EXISTS(SELECT 1 FROM connection_auth_operations WHERE terminal_at IS NULL)";
    if (sqlite3_prepare_v2(db, sql, -1, &s, NULL) != SQLITE_OK || sqlite3_step(s) != SQLITE_ROW ||
        sqlite3_column_type(s, 0) != SQLITE_INTEGER || sqlite3_column_type(s, 1) != SQLITE_INTEGER) goto done;
    out->owner = sqlite3_column_int64(s, 0); out->revision = sqlite3_column_int64(s, 1);
    if (out->owner <= 0 || out->revision <= 0 || !bounded_text(s, 2, out->reference, sizeof out->reference) ||
        !bounded_text(s, 3, out->hash, sizeof out->hash) || !bounded_text(s, 4, out->expiry, sizeof out->expiry) ||
        sqlite3_step(s) != SQLITE_DONE) goto done;
    result = 0;
 done:
    if (s) sqlite3_finalize(s);
    if (db) sqlite3_close(db);
    if (result) memset(out, 0, sizeof(*out));
    return result;
}
/* Only same-uid immediate children; executable/signature is verified by caller. */
int gamja_upgrade_children(int parent, int *out, int capacity) {
    if (parent <= 1 || !out || capacity <= 0 || capacity > 32) return -1;
    int pids[2048] = {0};
    int bytes = proc_listpids(PROC_UID_ONLY, geteuid(), pids, sizeof pids);
    if (bytes <= 0 || bytes >= (int)sizeof pids || bytes % sizeof(int)) return -1;
    int count = 0;
    for (int i = 0; i < bytes / (int)sizeof(int); ++i) {
        uint64_t sec, usec; int p;
        if (gamja_upgrade_process(pids[i], &sec, &usec, &p) == 0 && p == parent) {
            if (count == capacity) return -1;
            out[count++] = pids[i];
        }
    }
    return count;
}

int gamja_upgrade_home(char *out, int capacity) {
    struct passwd entry, *result = NULL; char scratch[8192];
    if (!out || capacity < 2 || getpwuid_r(geteuid(), &entry, scratch, sizeof scratch, &result) ||
        !result || !result->pw_dir || result->pw_dir[0] != '/') return 9;
    size_t length = strlen(result->pw_dir);
    if (length >= (size_t)capacity) return 9;
    memcpy(out, result->pw_dir, length + 1);
    return 0;
}
