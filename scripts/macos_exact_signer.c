/* Build-only exact-identity signer. This helper must never enter an artifact. */
#include <CoreFoundation/CoreFoundation.h>
#include <CommonCrypto/CommonDigest.h>
#include <Security/Security.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mach-o/dyld.h>
#include <sys/stat.h>
#include <unistd.h>

/* Apple OSS SecCodeSigner SPI is exported by Security.framework but omitted
 * from the public SDK umbrella headers. Every SPI symbol is resolved at runtime. */
typedef struct __SecCodeSigner *SecCodeSignerRef;
typedef OSStatus (*SignerCreateFn)(CFDictionaryRef, SecCSFlags, SecCodeSignerRef *);
typedef OSStatus (*SignerAddFn)(SecCodeSignerRef, SecStaticCodeRef, SecCSFlags, CFErrorRef *);

struct SignerSPI {
    void *handle;
    SignerCreateFn create;
    SignerAddFn add;
    CFStringRef flags;
    CFStringRef identifier;
    CFStringRef identity;
    CFStringRef requirements;
    CFStringRef signing_time;
    CFStringRef require_timestamp;
    CFStringRef preserve_metadata;
};

static struct SignerSPI spi;

static const char *identifier;
static Boolean preserve_engine_policy;
static Boolean is_directory;
static dev_t expected_target_device;
static ino_t expected_target_inode;
struct BundlePayloadHashes {
    unsigned char values[3][CC_SHA256_DIGEST_LENGTH];
};
enum {
    kSecCodeSignerPreserveEntitlements = 1 << 2,
    kSecCodeSignerPreserveLibraryConstraints = 1 << 10,
};
static SecKeychainRef active_keychain;
static unsigned char *active_keychain_password;
static size_t active_keychain_password_length;
static unsigned char *active_p12_password;
static size_t active_p12_password_length;

static void fail(const char *message, OSStatus status) {
    if (active_keychain) SecKeychainLock(active_keychain);
    if (active_keychain_password) {
        memset(active_keychain_password, 0, active_keychain_password_length);
        free(active_keychain_password);
        active_keychain_password = NULL;
    }
    if (active_p12_password) {
        memset(active_p12_password, 0, active_p12_password_length);
        free(active_p12_password);
        active_p12_password = NULL;
    }
    if (status) fprintf(stderr, "%s: OSStatus=%d\n", message, (int)status);
    else fprintf(stderr, "%s\n", message);
    exit(1);
}

static unsigned char *read_secret_fd(const char *text, size_t *length) {
    char *end = NULL;
    long parsed = strtol(text, &end, 10);
    if (!text[0] || !end || *end || parsed < 3 || parsed > 1024) fail("invalid secret fd", 0);
    int fd = (int)parsed;
    size_t capacity = 256;
    unsigned char *value = calloc(capacity, 1);
    if (!value) fail("allocation failed", 0);
    *length = 0;
    for (;;) {
        if (*length == capacity) {
            if (capacity >= 4096) fail("secret too large", 0);
            capacity *= 2;
            unsigned char *changed = realloc(value, capacity);
            if (!changed) fail("allocation failed", 0);
            value = changed;
        }
        ssize_t count = read(fd, value + *length, capacity - *length);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) fail("secret read failed", 0);
        if (count == 0) break;
        *length += (size_t)count;
    }
    close(fd);
    if (*length == 0) fail("empty secret", 0);
    return value;
}

static CFDataRef read_regular_file(const char *path, off_t limit) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0) fail("bounded file open failed", 0);
    struct stat status;
    if (fstat(fd, &status) || !S_ISREG(status.st_mode) || status.st_nlink != 1 ||
        status.st_uid != geteuid() || status.st_size <= 0 || status.st_size > limit)
        fail("bounded file metadata failed", 0);
    unsigned char *bytes = malloc((size_t)status.st_size);
    if (!bytes) fail("allocation failed", 0);
    size_t offset = 0;
    while (offset < (size_t)status.st_size) {
        ssize_t count = read(fd, bytes + offset, (size_t)status.st_size - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) fail("bounded file read failed", 0);
        offset += (size_t)count;
    }
    close(fd);
    CFDataRef value = CFDataCreate(NULL, bytes, (CFIndex)status.st_size);
    memset(bytes, 0, (size_t)status.st_size);
    free(bytes);
    if (!value) fail("bounded file data failed", 0);
    return value;
}

static void *required_symbol(void *handle, const char *name) {
    dlerror();
    void *symbol = dlsym(handle, name);
    if (!symbol || dlerror()) fail("SecCodeSigner SPI unavailable", errSecUnimplemented);
    return symbol;
}

static void load_spi(void) {
    spi.handle = dlopen(
        "/System/Library/Frameworks/Security.framework/Versions/A/Security",
        RTLD_NOW | RTLD_LOCAL);
    if (!spi.handle) fail("Security framework unavailable", errSecUnimplemented);
    void *symbol = required_symbol(spi.handle, "SecCodeSignerCreate");
    memcpy(&spi.create, &symbol, sizeof(spi.create));
    symbol = required_symbol(spi.handle, "SecCodeSignerAddSignatureWithErrors");
    memcpy(&spi.add, &symbol, sizeof(spi.add));
#define LOAD_KEY(field, name) do { \
    CFStringRef *address = (CFStringRef *)required_symbol(spi.handle, name); \
    spi.field = address ? *address : NULL; \
    if (!spi.field) fail("SecCodeSigner SPI constant unavailable", errSecUnimplemented); \
} while (0)
    LOAD_KEY(flags, "kSecCodeSignerFlags");
    LOAD_KEY(identifier, "kSecCodeSignerIdentifier");
    LOAD_KEY(identity, "kSecCodeSignerIdentity");
    LOAD_KEY(requirements, "kSecCodeSignerRequirements");
    LOAD_KEY(signing_time, "kSecCodeSignerSigningTime");
    LOAD_KEY(require_timestamp, "kSecCodeSignerRequireTimestamp");
    LOAD_KEY(preserve_metadata, "kSecCodeSignerPreserveMetadata");
#undef LOAD_KEY
}

static void verify_self_hash(const char *expected) {
    if (strlen(expected) != CC_SHA256_DIGEST_LENGTH * 2) fail("invalid helper SHA256", 0);
    for (size_t index = 0; expected[index]; index++)
        if (!((expected[index] >= '0' && expected[index] <= '9') ||
              (expected[index] >= 'a' && expected[index] <= 'f')))
            fail("invalid helper SHA256", 0);
    uint32_t length = 0;
    _NSGetExecutablePath(NULL, &length);
    char *reported = calloc((size_t)length + 1, 1);
    if (!reported || _NSGetExecutablePath(reported, &length)) fail("helper path failed", 0);
    char resolved[PATH_MAX];
    if (!realpath(reported, resolved)) fail("helper path failed", 0);
    free(reported);
    int fd = open(resolved, O_RDONLY | O_NOFOLLOW);
    struct stat status;
    if (fd < 0 || fstat(fd, &status) || !S_ISREG(status.st_mode) || status.st_nlink != 1 ||
        status.st_uid != geteuid() || (status.st_mode & 022) != 0)
        fail("unsafe helper binary", 0);
    CC_SHA256_CTX context;
    CC_SHA256_Init(&context);
    unsigned char buffer[64 * 1024];
    for (;;) {
        ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) fail("helper hash read failed", 0);
        if (count == 0) break;
        CC_SHA256_Update(&context, buffer, (CC_LONG)count);
    }
    close(fd);
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &context);
    char actual[CC_SHA256_DIGEST_LENGTH * 2 + 1];
    for (size_t index = 0; index < sizeof(digest); index++)
        snprintf(actual + index * 2, 3, "%02x", digest[index]);
    if (memcmp(actual, expected, sizeof(actual)) != 0) fail("helper hash mismatch", 0);
}

static void verify_self_adhoc_seal(void) {
    SecCodeRef self = NULL;
    OSStatus status = SecCodeCopySelf(kSecCSDefaultFlags, &self);
    if (!status) status = SecCodeCheckValidity(self, kSecCSStrictValidate, NULL);
    CFDictionaryRef information = NULL;
    if (!status) status = SecCodeCopySigningInformation(self, kSecCSSigningInformation, &information);
    CFNumberRef flags = information ? CFDictionaryGetValue(information, kSecCodeInfoFlags) : NULL;
    uint32_t value = 0;
    if (status || !flags || !CFNumberGetValue(flags, kCFNumberSInt32Type, &value) ||
        (value & kSecCodeSignatureAdhoc) == 0)
        fail("helper ad-hoc seal invalid", status);
    CFRelease(information);
    CFRelease(self);
}

static void require_no_ui(void) {
    OSStatus status = SecKeychainSetUserInteractionAllowed(false);
    Boolean allowed = true;
    if (!status) status = SecKeychainGetUserInteractionAllowed(&allowed);
    if (status || allowed) fail("keychain UI could not be disabled", status);
}

static void verify_signing_acl(SecKeyRef private_key, SecTrustedApplicationRef expected) {
    SecAccessRef access = NULL;
    CFArrayRef list = NULL;
    CFDataRef expected_data = NULL;
    OSStatus status = SecKeychainItemCopyAccess((SecKeychainItemRef)private_key, &access);
    if (!status) status = SecAccessCopyACLList(access, &list);
    if (!status) status = SecTrustedApplicationCopyData(expected, &expected_data);
    if (status || !access || !list || !expected_data) fail("signing ACL read failed", status);
    CFIndex signing_count = 0;
    for (CFIndex index = 0; index < CFArrayGetCount(list); index++) {
        SecACLRef acl = (SecACLRef)CFArrayGetValueAtIndex(list, index);
        CFArrayRef authorizations = SecACLCopyAuthorizations(acl);
        if (!authorizations) fail("ACL authorization read failed", 0);
        Boolean allows_any = CFArrayContainsValue(
            authorizations, CFRangeMake(0, CFArrayGetCount(authorizations)),
            kSecACLAuthorizationAny);
        Boolean allows_sign = CFArrayContainsValue(
            authorizations, CFRangeMake(0, CFArrayGetCount(authorizations)),
            kSecACLAuthorizationSign);
        CFRelease(authorizations);
        if (allows_any) fail("allow-all signing ACL rejected", 0);
        if (!allows_sign) continue;
        signing_count++;
        CFArrayRef applications = NULL;
        CFStringRef description = NULL;
        CSSM_ACL_KEYCHAIN_PROMPT_SELECTOR prompt;
        memset(&prompt, 0, sizeof(prompt));
        status = SecACLCopySimpleContents(acl, &applications, &description, &prompt);
        if (status || !applications || CFArrayGetCount(applications) != 1 || prompt.flags != 0)
            fail("signing ACL is not finite and nonprompting", status);
        SecTrustedApplicationRef actual =
            (SecTrustedApplicationRef)CFArrayGetValueAtIndex(applications, 0);
        if (CFGetTypeID(actual) != SecTrustedApplicationGetTypeID())
            fail("signing ACL application type mismatch", 0);
        CFDataRef actual_data = NULL;
        status = SecTrustedApplicationCopyData(actual, &actual_data);
        if (status || !actual_data || !CFEqual(actual_data, expected_data))
            fail("signing ACL application mismatch", status);
        CFRelease(actual_data);
        if (description) CFRelease(description);
        CFRelease(applications);
    }
    if (signing_count != 1) fail("signing ACL count mismatch", 0);
    CFRelease(expected_data);
    CFRelease(list);
    CFRelease(access);
}

static CFStringRef certificate_sha1(SecCertificateRef certificate) {
    CFDataRef data = SecCertificateCopyData(certificate);
    if (!data) fail("certificate data failed", 0);
    unsigned char digest[CC_SHA1_DIGEST_LENGTH];
    CC_SHA1(CFDataGetBytePtr(data), (CC_LONG)CFDataGetLength(data), digest);
    char hex[CC_SHA1_DIGEST_LENGTH * 2 + 1];
    for (size_t index = 0; index < sizeof(digest); index++)
        snprintf(hex + index * 2, 3, "%02X", digest[index]);
    CFRelease(data);
    return CFStringCreateWithCString(NULL, hex, kCFStringEncodingASCII);
}

static SecIdentityRef exact_identity(SecKeychainRef keychain, CFDataRef expected_der,
                                     CFStringRef *leaf_sha1) {
    const void *search_values[] = {keychain};
    CFArrayRef search = CFArrayCreate(NULL, search_values, 1, &kCFTypeArrayCallBacks);
    const void *keys[] = {kSecClass, kSecMatchSearchList, kSecReturnRef, kSecMatchLimit};
    const void *values[] = {kSecClassIdentity, search, kCFBooleanTrue, kSecMatchLimitAll};
    CFDictionaryRef query = CFDictionaryCreate(NULL, keys, values, 4,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFTypeRef result = NULL;
    OSStatus status = SecItemCopyMatching(query, &result);
    if (status || !result || CFGetTypeID(result) != CFArrayGetTypeID() ||
        CFArrayGetCount((CFArrayRef)result) != 1)
        fail("custom keychain must contain exactly one identity", status);
    SecIdentityRef identity = (SecIdentityRef)CFArrayGetValueAtIndex((CFArrayRef)result, 0);
    if (CFGetTypeID(identity) != SecIdentityGetTypeID()) fail("identity type mismatch", 0);
    CFRetain(identity);
    SecCertificateRef certificate = NULL;
    SecKeyRef private_key = NULL;
    status = SecIdentityCopyCertificate(identity, &certificate);
    if (!status) status = SecIdentityCopyPrivateKey(identity, &private_key);
    if (status || !certificate || !private_key) fail("identity pair incomplete", status);
    CFDataRef actual_der = SecCertificateCopyData(certificate);
    if (!actual_der || !CFEqual(actual_der, expected_der)) fail("identity leaf DER mismatch", 0);
    *leaf_sha1 = certificate_sha1(certificate);
    if (!*leaf_sha1) fail("identity leaf digest failed", 0);
    CFRelease(actual_der);
    CFRelease(private_key);
    CFRelease(certificate);
    CFRelease(result);
    CFRelease(query);
    CFRelease(search);
    return identity;
}

static void verify_regular_target(const char *path, Boolean require_executable) {
    struct stat status;
    if (lstat(path, &status) || !S_ISREG(status.st_mode) || S_ISLNK(status.st_mode) ||
        status.st_nlink != 1 || status.st_uid != geteuid() || (status.st_mode & 022) != 0 ||
        (require_executable && (status.st_mode & 0111) == 0))
        fail("unsafe signing target", 0);
}

static unsigned long long parse_unsigned_identity(const char *value, const char *message) {
    if (!value[0] || value[0] == '+' || value[0] == '-') fail(message, 0);
    errno = 0;
    char *end = NULL;
    unsigned long long parsed = strtoull(value, &end, 10);
    if (errno || !end || *end) fail(message, 0);
    return parsed;
}

static void bundle_path(const char *root, const char *relative, char output[PATH_MAX]) {
    int count = snprintf(output, PATH_MAX, "%s/%s", root, relative);
    if (count <= 0 || count >= PATH_MAX) fail("unsafe signing bundle path", 0);
}

static void verify_owned_bundle_directory(const char *root, const char *relative) {
    char path[PATH_MAX], resolved[PATH_MAX];
    bundle_path(root, relative, path);
    struct stat status;
    if (lstat(path, &status) || !S_ISDIR(status.st_mode) || S_ISLNK(status.st_mode) ||
        status.st_uid != geteuid() || (status.st_mode & 022) != 0 ||
        !realpath(path, resolved) || strcmp(path, resolved) != 0)
        fail("unsafe signing bundle directory", 0);
}

static void require_bundle_descendant(const char *root, const char *path) {
    char resolved[PATH_MAX];
    size_t root_length = strlen(root);
    if (!realpath(path, resolved) || strncmp(resolved, root, root_length) != 0 ||
        resolved[root_length] != '/' || strcmp(resolved, path) != 0)
        fail("bundle path escapes target", 0);
}

static void verify_bundle_target(const char *path) {
    size_t length = strlen(path);
    char resolved[PATH_MAX];
    if (length <= 4 || length >= PATH_MAX || path[0] != '/' ||
        strcmp(path + length - 4, ".app") != 0 || !realpath(path, resolved) ||
        strcmp(path, resolved) != 0)
        fail("unsafe signing bundle path", 0);
    struct stat status;
    if (lstat(path, &status) || !S_ISDIR(status.st_mode) || S_ISLNK(status.st_mode) ||
        status.st_uid != geteuid() || (status.st_mode & 022) != 0 ||
        status.st_dev != expected_target_device || status.st_ino != expected_target_inode)
        fail("signing target identity mismatch", 0);

    const char *directories[] = {
        "Contents", "Contents/MacOS", "Contents/Resources",
        "Contents/Resources/gamja-core", "Contents/Resources/gamja-core/bin",
        "Contents/Resources/gamja-core/libexec",
        "Contents/Resources/gamja-core/libexec/gamja",
    };
    for (size_t index = 0; index < sizeof(directories) / sizeof(directories[0]); index++)
        verify_owned_bundle_directory(path, directories[index]);

    char info_path[PATH_MAX];
    bundle_path(path, "Contents/Info.plist", info_path);
    require_bundle_descendant(path, info_path);
    CFDataRef info_data = read_regular_file(info_path, 1024 * 1024);
    CFErrorRef property_error = NULL;
    CFPropertyListRef property = CFPropertyListCreateWithData(
        NULL, info_data, kCFPropertyListImmutable, NULL, &property_error);
    if (property_error) CFRelease(property_error);
    if (!property || CFGetTypeID(property) != CFDictionaryGetTypeID())
        fail("unsafe signing bundle metadata", 0);
    CFDictionaryRef info = (CFDictionaryRef)property;
    CFStringRef bundle_identifier = CFDictionaryGetValue(info, CFSTR("CFBundleIdentifier"));
    CFStringRef executable_name = CFDictionaryGetValue(info, CFSTR("CFBundleExecutable"));
    if (!bundle_identifier || CFGetTypeID(bundle_identifier) != CFStringGetTypeID() ||
        !CFEqual(bundle_identifier, CFSTR("top.gamja.bot")) || !executable_name ||
        CFGetTypeID(executable_name) != CFStringGetTypeID())
        fail("unsafe signing bundle metadata", 0);
    char executable[NAME_MAX + 1];
    if (!CFStringGetCString(executable_name, executable, sizeof(executable),
                            kCFStringEncodingUTF8) || strcmp(executable, "gamja-bot") != 0)
        fail("unsafe signing bundle executable", 0);
    char main_path[PATH_MAX];
    char main_relative[PATH_MAX];
    int count = snprintf(main_relative, sizeof(main_relative), "Contents/MacOS/%s", executable);
    if (count <= 0 || (size_t)count >= sizeof(main_relative))
        fail("unsafe signing bundle path", 0);
    bundle_path(path, main_relative, main_path);
    require_bundle_descendant(path, main_path);
    verify_regular_target(main_path, true);
    CFRelease(property);
    CFRelease(info_data);
}

static void verify_target_metadata(const char *path) {
    if (is_directory) verify_bundle_target(path);
    else verify_regular_target(path, true);
}

static void hash_regular_payload(const char *path,
                                 unsigned char digest[CC_SHA256_DIGEST_LENGTH]) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    struct stat status;
    if (fd < 0 || fstat(fd, &status) || !S_ISREG(status.st_mode) ||
        status.st_nlink != 1 || status.st_uid != geteuid() || (status.st_mode & 022) != 0)
        fail("unsafe signing bundle payload", 0);
    CC_SHA256_CTX context;
    CC_SHA256_Init(&context);
    unsigned char buffer[64 * 1024];
    for (;;) {
        ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) fail("signing bundle payload read failed", 0);
        if (count == 0) break;
        CC_SHA256_Update(&context, buffer, (CC_LONG)count);
    }
    close(fd);
    CC_SHA256_Final(digest, &context);
}

static void snapshot_bundle_payloads(const char *path, struct BundlePayloadHashes *snapshot) {
    const char *relative[] = {
        "Contents/Resources/gamja-core/bin/gamja",
        "Contents/Resources/gamja-core/libexec/gamja/gamja-engine",
        "Contents/Resources/gamja-core/libexec/gamja/gamja-platform-helper",
    };
    for (size_t index = 0; index < sizeof(relative) / sizeof(relative[0]); index++) {
        char payload[PATH_MAX];
        int count = snprintf(payload, sizeof(payload), "%s/%s", path, relative[index]);
        if (count <= 0 || (size_t)count >= sizeof(payload))
            fail("unsafe signing bundle path", 0);
        hash_regular_payload(payload, snapshot->values[index]);
    }
}

static void sign_target(const char *path, SecIdentityRef identity, CFStringRef leaf_sha1) {
    verify_target_metadata(path);
    struct BundlePayloadHashes nested_before, nested_after;
    if (is_directory) snapshot_bundle_payloads(path, &nested_before);
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(
        NULL, (const UInt8 *)path, (CFIndex)strlen(path), is_directory);
    SecStaticCodeRef code = NULL;
    OSStatus status = SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code);
    if (status || !code) fail("static code creation failed", status);
    CFStringRef signer_identifier = CFStringCreateWithCString(NULL, identifier, kCFStringEncodingASCII);
    CFStringRef requirement_expression = CFStringCreateWithFormat(
        NULL, NULL, CFSTR("anchor = H\"%@\" and identifier \"%@\""),
        leaf_sha1, signer_identifier);
    CFStringRef requirement_set = CFStringCreateWithFormat(
        NULL, NULL, CFSTR("designated => %@"), requirement_expression);
    uint32_t runtime = kSecCodeSignatureRuntime;
    CFNumberRef flags = CFNumberCreate(NULL, kCFNumberSInt32Type, &runtime);
    uint32_t preserve_value =
        kSecCodeSignerPreserveEntitlements | kSecCodeSignerPreserveLibraryConstraints;
    CFNumberRef preserve = CFNumberCreate(NULL, kCFNumberSInt32Type, &preserve_value);
    const void *keys[] = {spi.identity, spi.identifier, spi.flags, spi.requirements,
                          spi.require_timestamp, spi.signing_time, spi.preserve_metadata};
    const void *values[] = {identity, signer_identifier, flags, requirement_set,
                            kCFBooleanFalse, kCFNull, preserve};
    CFDictionaryRef parameters = CFDictionaryCreate(NULL, keys, values,
        preserve_engine_policy ? 7 : 6,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    SecCodeSignerRef signer = NULL;
    status = spi.create(parameters, kSecCSDefaultFlags, &signer);
    CFErrorRef error = NULL;
    if (!status) status = spi.add(
        signer, code, kSecCSDefaultFlags, &error);
    if (error) {
        CFRelease(error);
        error = NULL;
    }
    if (status) fail("SPI signing failed", status);

    verify_target_metadata(path);
    if (is_directory) {
        snapshot_bundle_payloads(path, &nested_after);
        if (memcmp(&nested_before, &nested_after, sizeof(nested_before)) != 0)
            fail("signed bundle nested payload changed", 0);
    }

    CFRelease(code);
    code = NULL;
    status = SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code);
    SecRequirementRef exact = NULL;
    if (!status) status = SecRequirementCreateWithString(
        requirement_expression, kSecCSDefaultFlags, &exact);
    if (!status) status = SecStaticCodeCheckValidityWithErrors(
        code, kSecCSStrictValidate | kSecCSCheckAllArchitectures, exact, &error);
    if (error) {
        CFRelease(error);
        error = NULL;
    }
    if (status) fail("signed code exact requirement failed", status);
    SecRequirementRef embedded = NULL;
    CFDataRef embedded_data = NULL;
    CFDataRef exact_data = NULL;
    status = SecCodeCopyDesignatedRequirement(code, kSecCSDefaultFlags, &embedded);
    if (!status) status = SecRequirementCopyData(embedded, kSecCSDefaultFlags, &embedded_data);
    if (!status) status = SecRequirementCopyData(exact, kSecCSDefaultFlags, &exact_data);
    if (status || !embedded || !embedded_data || !exact_data ||
        !CFEqual(embedded_data, exact_data))
        fail("signed code designated requirement mismatch", status);
    CFDictionaryRef information = NULL;
    status = SecCodeCopySigningInformation(code, kSecCSSigningInformation, &information);
    if (status || !information) fail("signed code information failed", status);
    CFStringRef actual_identifier = CFDictionaryGetValue(information, kSecCodeInfoIdentifier);
    CFNumberRef actual_flags = CFDictionaryGetValue(information, kSecCodeInfoFlags);
    uint32_t flag_value = 0;
    if (!actual_identifier || !CFEqual(actual_identifier, signer_identifier) || !actual_flags ||
        !CFNumberGetValue(actual_flags, kCFNumberSInt32Type, &flag_value) ||
        (flag_value & kSecCodeSignatureRuntime) == 0 ||
        CFDictionaryContainsKey(information, kSecCodeInfoTimestamp))
        fail("signed code policy mismatch", 0);

    CFRelease(information);
    CFRelease(exact_data);
    CFRelease(embedded_data);
    CFRelease(embedded);
    CFRelease(exact);
    CFRelease(signer);
    CFRelease(parameters);
    CFRelease(preserve);
    CFRelease(flags);
    CFRelease(requirement_set);
    CFRelease(requirement_expression);
    CFRelease(signer_identifier);
    CFRelease(code);
    CFRelease(url);
}

static void select_identifier(const char *mode) {
    preserve_engine_policy = false;
    is_directory = false;
    if (strcmp(mode, "engine") == 0) {
        identifier = "top.gamja.engine";
        preserve_engine_policy = true;
    } else if (strcmp(mode, "core") == 0) identifier = "top.gamja.credential-core";
    else if (strcmp(mode, "library") == 0) identifier = "top.gamja.engine.library";
    else if (strcmp(mode, "broker") == 0)
        identifier = "top.gamja.credential-broker.v1";
    else if (strcmp(mode, "release-authority") == 0)
        identifier = "top.gamja.credential-release-authority.v1";
    else if (strcmp(mode, "slack-broker") == 0)
        identifier = "top.gamja.slack-credential-broker.v1";
    else if (strcmp(mode, "slack-release-authority") == 0)
        identifier = "top.gamja.slack-credential-release-authority.v1";
    else if (strcmp(mode, "mcp-broker") == 0)
        identifier = "top.gamja.mcp-credential-broker.v1";
    else if (strcmp(mode, "mcp-release-authority") == 0)
        identifier = "top.gamja.mcp-credential-release-authority.v1";
    else if (strcmp(mode, "publisher-broker") == 0)
        identifier = "top.gamja.template-publisher-credential-broker.v1";
    else if (strcmp(mode, "publisher-release-authority") == 0)
        identifier = "top.gamja.template-publisher-credential-release-authority.v1";
    else if (strcmp(mode, "vault-broker") == 0)
        identifier = "top.gamja.vault-credential-broker.v1";
    else if (strcmp(mode, "vault-release-authority") == 0)
        identifier = "top.gamja.vault-credential-release-authority.v1";
    else if (strcmp(mode, "gui") == 0) {
        identifier = "top.gamja.bot";
        is_directory = true;
    }
    else fail("unapproved signing mode", 0);
}

static void validate_keychain_path(const char *path) {
    struct stat status;
    if (lstat(path, &status) || !S_ISREG(status.st_mode) || S_ISLNK(status.st_mode) ||
        status.st_nlink != 1 || status.st_uid != geteuid() || (status.st_mode & 022) != 0)
        fail("unsafe custom keychain", 0);
}

static void require_exact_keychain_ref(SecKeychainRef keychain, const char *expected_path) {
    if (!keychain) fail("custom keychain reference missing", 0);
    char actual_path[PATH_MAX];
    UInt32 length = sizeof(actual_path) - 1;
    OSStatus status = SecKeychainGetPath(keychain, &length, actual_path);
    if (status || length == 0 || length >= sizeof(actual_path))
        fail("custom keychain reference path failed", status);
    if (actual_path[length - 1] == '\0') actual_path[length - 1] = '\0';
    else actual_path[length] = '\0';
    char expected_real[PATH_MAX], actual_real[PATH_MAX];
    if (!realpath(expected_path, expected_real) || !realpath(actual_path, actual_real) ||
        strcmp(expected_real, actual_real) != 0)
        fail("custom keychain reference mismatch", 0);
    struct stat expected_status, actual_status;
    if (lstat(expected_real, &expected_status) || lstat(actual_real, &actual_status) ||
        !S_ISREG(expected_status.st_mode) || S_ISLNK(expected_status.st_mode) ||
        expected_status.st_uid != geteuid() || expected_status.st_nlink != 1 ||
        (expected_status.st_mode & 022) != 0 ||
        expected_status.st_dev != actual_status.st_dev ||
        expected_status.st_ino != actual_status.st_ino)
        fail("custom keychain reference inode mismatch", 0);
}

static void finish_success(const char *message) {
    if (!active_keychain) fail("custom keychain reference missing", 0);
    OSStatus status = SecKeychainLock(active_keychain);
    if (status) fail("custom keychain lock failed", status);
    puts(message);
}

int main(int argc, char **argv) {
    require_no_ui();
    if (argc < 2 || (strcmp(argv[1], "import") != 0 && strcmp(argv[1], "sign") != 0))
        fail("usage: signer import KEYCHAIN PKCS12 LEAF_DER KEYCHAIN_PASS_FD P12_PASS_FD HELPER_SHA256 | sign MODE KEYCHAIN LEAF_DER KEYCHAIN_PASS_FD HELPER_SHA256 TARGET [TARGET_DEV TARGET_INO]", 0);
    const Boolean importing = strcmp(argv[1], "import") == 0;
    if ((importing && argc != 8) || (!importing && argc != 8 && argc != 10))
        fail("usage: signer import KEYCHAIN PKCS12 LEAF_DER KEYCHAIN_PASS_FD P12_PASS_FD HELPER_SHA256 | sign MODE KEYCHAIN LEAF_DER KEYCHAIN_PASS_FD HELPER_SHA256 TARGET [TARGET_DEV TARGET_INO]", 0);
    if (!importing) {
        select_identifier(argv[2]);
        if ((is_directory && argc != 10) || (!is_directory && argc != 8))
            fail("signing mode argument mismatch", 0);
        if (is_directory) {
            unsigned long long device = parse_unsigned_identity(argv[8], "invalid target device");
            unsigned long long inode = parse_unsigned_identity(argv[9], "invalid target inode");
            expected_target_device = (dev_t)device;
            expected_target_inode = (ino_t)inode;
            if ((unsigned long long)expected_target_device != device ||
                (unsigned long long)expected_target_inode != inode)
                fail("invalid target identity", 0);
        }
    }
    const char *helper_hash = importing ? argv[7] : argv[6];
    verify_self_hash(helper_hash);
    verify_self_adhoc_seal();
    load_spi();

    const char *keychain_path = importing ? argv[2] : argv[3];
    const char *der_path = importing ? argv[4] : argv[4];
    const char *keychain_fd = importing ? argv[5] : argv[5];
    CFDataRef expected_der = read_regular_file(der_path, 64 * 1024);
    size_t keychain_length = 0;
    unsigned char *keychain_password = read_secret_fd(keychain_fd, &keychain_length);
    active_keychain_password = keychain_password;
    active_keychain_password_length = keychain_length;

    OSStatus status;
    SecKeychainRef opened_keychain = NULL;
    if (importing) {
        status = SecKeychainCreate(keychain_path, (UInt32)keychain_length, keychain_password,
                                   false, NULL, &opened_keychain);
        if (!status && chmod(keychain_path, 0600)) fail("custom keychain chmod failed", 0);
        if (!status) validate_keychain_path(keychain_path);
    } else {
        validate_keychain_path(keychain_path);
        status = SecKeychainOpen(keychain_path, &opened_keychain);
    }
    if (status || !opened_keychain) fail("custom keychain open failed", status);
    require_exact_keychain_ref(opened_keychain, keychain_path);
    active_keychain = opened_keychain;
    if (!status) status = SecKeychainUnlock(
        active_keychain, (UInt32)keychain_length, keychain_password, true);
    memset(keychain_password, 0, keychain_length);
    free(keychain_password);
    active_keychain_password = NULL;
    if (status) fail("custom keychain open failed", status);

    SecTrustedApplicationRef self = NULL;
    status = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (status || !self) fail("self application identity failed", status);

    if (importing) {
        size_t p12_length = 0;
        unsigned char *p12_password = read_secret_fd(argv[6], &p12_length);
        active_p12_password = p12_password;
        active_p12_password_length = p12_length;
        CFStringRef passphrase = CFStringCreateWithBytes(
            NULL, p12_password, (CFIndex)p12_length, kCFStringEncodingUTF8, false);
        if (!passphrase) fail("PKCS12 passphrase conversion failed", 0);
        const void *self_values[] = {self};
        CFArrayRef applications = CFArrayCreate(NULL, self_values, 1, &kCFTypeArrayCallBacks);
        SecAccessRef access = NULL;
        status = SecAccessCreate(CFSTR("Gamja build signing identity"), applications, &access);
        if (status || !applications || !access) fail("self-only signing ACL creation failed", status);
        CFDataRef p12 = read_regular_file(argv[3], 1024 * 1024);
        SecExternalFormat format = kSecFormatPKCS12;
        SecExternalItemType type = kSecItemTypeAggregate;
        const void *usage_values[] = {kSecAttrCanSign};
        CFArrayRef usages = CFArrayCreate(NULL, usage_values, 1, &kCFTypeArrayCallBacks);
        const void *attribute_values[] = {kSecAttrIsPermanent, kSecAttrIsSensitive};
        CFArrayRef attributes = CFArrayCreate(NULL, attribute_values, 2, &kCFTypeArrayCallBacks);
        SecItemImportExportKeyParameters parameters;
        memset(&parameters, 0, sizeof(parameters));
        parameters.version = SEC_KEY_IMPORT_EXPORT_PARAMS_VERSION;
        parameters.passphrase = passphrase;
        parameters.accessRef = access;
        parameters.keyUsage = usages;
        parameters.keyAttributes = attributes;
        CFArrayRef items = NULL;
        status = SecItemImport(p12, NULL, &format, &type, 0, &parameters,
                               active_keychain, &items);
        memset(p12_password, 0, p12_length);
        free(p12_password);
        active_p12_password = NULL;
        if (status || !items || CFArrayGetCount(items) != 1 ||
            CFGetTypeID(CFArrayGetValueAtIndex(items, 0)) != SecIdentityGetTypeID())
            fail("PKCS12 exact identity import failed", status);
        CFStringRef leaf_sha1 = NULL;
        SecIdentityRef identity = exact_identity(active_keychain, expected_der, &leaf_sha1);
        SecKeyRef private_key = NULL;
        status = SecIdentityCopyPrivateKey(identity, &private_key);
        if (status || !private_key) fail("private key pairing failed", status);
        verify_signing_acl(private_key, self);
        finish_success("imported-exact-identity:1");
        CFRelease(private_key);
        CFRelease(identity);
        CFRelease(leaf_sha1);
        CFRelease(items);
        CFRelease(attributes);
        CFRelease(usages);
        CFRelease(p12);
        CFRelease(access);
        CFRelease(applications);
        CFRelease(passphrase);
    } else {
        CFStringRef leaf_sha1 = NULL;
        SecIdentityRef identity = exact_identity(active_keychain, expected_der, &leaf_sha1);
        SecKeyRef private_key = NULL;
        status = SecIdentityCopyPrivateKey(identity, &private_key);
        if (status || !private_key) fail("private key pairing failed", status);
        verify_signing_acl(private_key, self);
        sign_target(argv[7], identity, leaf_sha1);
        finish_success(strcmp(argv[2], "engine") == 0 ?
                       "signed-engine-exact-identity:1" :
                       strcmp(argv[2], "core") == 0 ?
                       "signed-core-exact-identity:1" :
                       strcmp(argv[2], "library") == 0 ?
                       "signed-library-exact-identity:1" :
                       strcmp(argv[2], "broker") == 0 ?
                       "signed-broker-exact-identity:1" :
                       strcmp(argv[2], "release-authority") == 0 ?
                       "signed-release-authority-exact-identity:1" :
                       strcmp(argv[2], "slack-broker") == 0 ?
                       "signed-slack-broker-exact-identity:1" :
                       strcmp(argv[2], "slack-release-authority") == 0 ?
                       "signed-slack-release-authority-exact-identity:1" :
                       strcmp(argv[2], "mcp-broker") == 0 ?
                       "signed-mcp-broker-exact-identity:1" :
                       strcmp(argv[2], "mcp-release-authority") == 0 ?
                       "signed-mcp-release-authority-exact-identity:1" :
                       strcmp(argv[2], "publisher-broker") == 0 ?
                       "signed-publisher-broker-exact-identity:1" :
                       strcmp(argv[2], "publisher-release-authority") == 0 ?
                       "signed-publisher-release-authority-exact-identity:1" :
                       strcmp(argv[2], "vault-broker") == 0 ?
                       "signed-vault-broker-exact-identity:1" :
                       strcmp(argv[2], "vault-release-authority") == 0 ?
                       "signed-vault-release-authority-exact-identity:1" :
                       "signed-gui-exact-identity:1");
        CFRelease(private_key);
        CFRelease(identity);
        CFRelease(leaf_sha1);
    }

    CFRelease(self);
    CFRelease(expected_der);
    CFRelease(active_keychain);
    active_keychain = NULL;
    dlclose(spi.handle);
    return 0;
}
