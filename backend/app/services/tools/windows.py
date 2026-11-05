"""Windows 11 handle-based process and file effects. Imported only on Windows."""

import ctypes as c
import os
import subprocess
import uuid
from ctypes import wintypes as w

kernel = c.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
ntdll = c.WinDLL("ntdll") if os.name == "nt" else None


def _check(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


if os.name == "nt":
    import _winapi
    import msvcrt

    HANDLE = w.HANDLE
    ULONG_PTR = c.c_size_t

    class BasicLimit(c.Structure):
        _fields_ = [("PerProcessUserTimeLimit", c.c_int64), ("PerJobUserTimeLimit", c.c_int64),
                    ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", c.c_size_t),
                    ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ULONG_PTR), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

    class IoCounters(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimit(c.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", c.c_size_t), ("JobMemoryLimit", c.c_size_t),
                    ("PeakProcessMemoryUsed", c.c_size_t), ("PeakJobMemoryUsed", c.c_size_t)]

    kernel.CreateJobObjectW.restype = HANDLE
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    kernel.TerminateJobObject.argtypes = [HANDLE, w.UINT]
    kernel.CloseHandle.argtypes = [HANDLE]
    kernel.ResumeThread.argtypes = [HANDLE]
    kernel.ResumeThread.restype = w.DWORD
    kernel.CreateFileW.restype = HANDLE
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p,
                                   w.DWORD, w.DWORD, HANDLE]

    class UnicodeString(c.Structure):
        _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPWSTR)]

    class ObjectAttributes(c.Structure):
        _fields_ = [("Length", w.ULONG), ("RootDirectory", HANDLE),
                    ("ObjectName", c.POINTER(UnicodeString)), ("Attributes", w.ULONG),
                    ("SecurityDescriptor", c.c_void_p), ("SecurityQualityOfService", c.c_void_p)]

    class IoStatus(c.Structure):
        _fields_ = [("Status", c.c_size_t), ("Information", c.c_size_t)]

    ntdll.NtCreateFile.argtypes = [c.POINTER(HANDLE), w.DWORD, c.POINTER(ObjectAttributes),
                                   c.POINTER(IoStatus), c.c_void_p, w.ULONG, w.ULONG,
                                   w.ULONG, w.ULONG, c.c_void_p, w.ULONG]
    ntdll.NtCreateFile.restype = c.c_long
    ntdll.RtlNtStatusToDosError.argtypes = [c.c_long]
    ntdll.RtlNtStatusToDosError.restype = w.ULONG
    kernel.GetFileInformationByHandle.argtypes = [HANDLE, c.c_void_p]
    kernel.SetFileInformationByHandle.argtypes = [HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.ReadFile.argtypes = [HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    kernel.WriteFile.argtypes = [HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    kernel.FlushFileBuffers.argtypes = [HANDLE]

    class ByHandleInfo(c.Structure):
        _fields_ = [("FileAttributes", w.DWORD), ("CreationTime", w.FILETIME),
                    ("LastAccessTime", w.FILETIME), ("LastWriteTime", w.FILETIME),
                    ("VolumeSerialNumber", w.DWORD), ("FileSizeHigh", w.DWORD),
                    ("FileSizeLow", w.DWORD), ("NumberOfLinks", w.DWORD),
                    ("FileIndexHigh", w.DWORD), ("FileIndexLow", w.DWORD)]

    class RenameInfo(c.Structure):
        _fields_ = [("ReplaceIfExists", w.DWORD), ("RootDirectory", HANDLE),
                    ("FileNameLength", w.DWORD), ("FileName", w.WCHAR * 1)]

    class DispositionInfo(c.Structure):
        _fields_ = [("DeleteFile", w.BOOLEAN)]


class JobProcess:
    def __init__(self, hp, ht, job, pid, stdout):
        self.hp, self.ht, self.job, self.pid, self.stdout = hp, ht, job, pid, stdout
        self.returncode = None

    def poll(self):
        if self.returncode is None and _winapi.WaitForSingleObject(self.hp, 0) == _winapi.WAIT_OBJECT_0:
            self.returncode = _winapi.GetExitCodeProcess(self.hp)
        return self.returncode

    def wait(self):
        _winapi.WaitForSingleObject(self.hp, _winapi.INFINITE)
        return self.poll()

    def communicate(self):
        data = self.stdout.read()
        self.wait()
        return data, None

    def terminate_job(self):
        if self.job:
            kernel.TerminateJobObject(self.job, 1)
            kernel.CloseHandle(self.job)
            self.job = None
        self.wait()

    def close(self):
        self.terminate_job()
        self.stdout.close()
        kernel.CloseHandle(self.ht)
        kernel.CloseHandle(self.hp)


def launch_job_process(executable, argv, cwd, env, lifetime):
    """Create suspended, assign a kill-on-close Job, then resume."""
    read, write = _winapi.CreatePipe(None, 0)
    nul_fd = os.open(os.devnull, os.O_RDONLY)
    nul = msvcrt.get_osfhandle(nul_fd)
    job = hp = ht = None
    try:
        os.set_handle_inheritable(read, False)
        os.set_handle_inheritable(write, True)
        os.set_handle_inheritable(nul, True)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= _winapi.STARTF_USESTDHANDLES
        si.hStdInput, si.hStdOutput, si.hStdError = nul, write, write
        si.lpAttributeList = {"handle_list": [int(nul), int(write)]}
        job = _check(kernel.CreateJobObjectW(None, None))
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))
        hp, ht, pid, _ = _winapi.CreateProcess(
            executable, subprocess.list2cmdline([executable, *argv]), None, None, True,
            0x00000004 | 0x08000000 | 0x00080000, env, cwd, si)
        try:
            _check(kernel.AssignProcessToJobObject(job, hp))
            if kernel.ResumeThread(ht) == 0xFFFFFFFF:
                raise c.WinError(c.get_last_error())
        except BaseException:
            _winapi.TerminateProcess(hp, 1)
            raise
        _winapi.CloseHandle(write)
        write = None
        os.close(nul_fd)
        nul = None
        stdout = os.fdopen(msvcrt.open_osfhandle(int(read), os.O_RDONLY), "rb", buffering=0)
        read = None
        return JobProcess(hp, ht, job, pid, stdout), None
    except BaseException:
        if hp:
            _winapi.CloseHandle(hp)
        if ht:
            _winapi.CloseHandle(ht)
        if job:
            kernel.CloseHandle(job)
        raise
    finally:
        if read:
            _winapi.CloseHandle(read)
        if write:
            _winapi.CloseHandle(write)
        if nul:
            os.close(nul_fd)


def _open_relative(parent, name, access, disposition=1, directory=False):
    if not name or name in (".", "..") or "\\" in name or "/" in name:
        raise ValueError("Invalid path component")
    buffer = c.create_unicode_buffer(name)
    u = UnicodeString(len(name.encode("utf-16-le")), len(name.encode("utf-16-le")) + 2, buffer)
    attrs = ObjectAttributes(c.sizeof(ObjectAttributes), parent, c.pointer(u), 0x40, None, None)
    result = HANDLE()
    ios = IoStatus()
    options = 0x20 | 0x200000 | (0x1 if directory else 0x40)
    status = ntdll.NtCreateFile(c.byref(result), access | 0x100000, c.byref(attrs),
                                c.byref(ios), None, 0, 0x7, disposition, options, None, 0)
    if status < 0:
        raise OSError(int(ntdll.RtlNtStatusToDosError(status)), "Windows handle-relative open failed")
    info = ByHandleInfo()
    try:
        _check(kernel.GetFileInformationByHandle(result, c.byref(info)))
        if info.FileAttributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise ValueError("Reparse point denied")
        return result
    except BaseException:
        kernel.CloseHandle(result)
        raise


def _parent(path, write=False):
    import ntpath
    if (not ntpath.isabs(path) or "\0" in path or path.startswith(("\\\\", "\\?", "\\."))
            or any(part in (".", "..", "") for part in path.split("\\")[1:])):
        raise ValueError("Invalid Windows path")
    drive, tail = ntpath.splitdrive(path)
    if len(drive) != 2 or drive[1] != ":" or not tail.startswith("\\"):
        raise ValueError("Unsupported Windows path")
    parts = [part for part in tail.split("\\") if part]
    if not parts:
        raise ValueError("Cannot use volume root")
    root_access = 0x80 | 0x20 | (0x2 if write and len(parts) == 1 else 0)
    root = kernel.CreateFileW(drive + "\\", root_access, 0x7, None, 3,
                              0x02000000 | 0x00200000, None)
    if root == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    fd = root
    try:
        for index, part in enumerate(parts[:-1]):
            access = 0x80 | 0x20 | (0x2 if write and index == len(parts) - 2 else 0)
            next_fd = _open_relative(fd, part, access, directory=True)
            kernel.CloseHandle(fd)
            fd = next_fd
        return fd, parts[-1]
    except BaseException:
        kernel.CloseHandle(fd)
        raise


def _rename(handle, parent, name):
    data = name.encode("utf-16-le")
    size = RenameInfo.FileName.offset + len(data)
    buffer = c.create_string_buffer(size)
    info = c.cast(buffer, c.POINTER(RenameInfo)).contents
    info.ReplaceIfExists = 1
    info.RootDirectory = parent
    info.FileNameLength = len(data)
    c.memmove(c.addressof(buffer) + RenameInfo.FileName.offset, data, len(data))
    _check(kernel.SetFileInformationByHandle(handle, 3, buffer, size))


def _read(handle):
    data = bytearray()
    while len(data) <= 1_000_000:
        chunk = c.create_string_buffer(min(65536, 1_000_001 - len(data)))
        count = w.DWORD()
        _check(kernel.ReadFile(handle, chunk, len(chunk), c.byref(count), None))
        if not count.value:
            break
        data.extend(chunk.raw[:count.value])
    if len(data) > 1_000_000:
        raise ValueError("File exceeds one-megabyte read limit")
    return data.decode("utf-8", "replace")


def _write(handle, content):
    data = content.encode()
    offset = 0
    while offset < len(data):
        count = w.DWORD()
        part = data[offset:offset + 65536]
        _check(kernel.WriteFile(handle, part, len(part), c.byref(count), None))
        offset += count.value
    _check(kernel.FlushFileBuffers(handle))


def file_effect(family, args):
    """Walk one component at a time under retained handles; never reopen a checked path."""
    if family == "move_file":
        if args["source"] == args["destination"]:
            return "Source and destination are the same file."
        src_parent, src_name = _parent(args["source"])
        try:
            dst_parent, dst_name = _parent(args["destination"], write=True)
            try:
                source = _open_relative(src_parent, src_name, 0x10000 | 0x80)
                try:
                    try:
                        existing = _open_relative(dst_parent, dst_name, 0x80)
                    except OSError as error:
                        if error.errno not in (2, 3):
                            raise
                    else:
                        kernel.CloseHandle(existing)
                    _rename(source, dst_parent, dst_name)
                    return "Moved file."
                finally:
                    kernel.CloseHandle(source)
            finally:
                kernel.CloseHandle(dst_parent)
        finally:
            kernel.CloseHandle(src_parent)
    parent, name = _parent(args["path"], write=family == "write_file")
    try:
        if family == "read_file":
            handle = _open_relative(parent, name, 0x1 | 0x80)
            try:
                return _read(handle)
            finally:
                kernel.CloseHandle(handle)
        if family == "delete_file":
            handle = _open_relative(parent, name, 0x10000 | 0x80)
            try:
                disposition = DispositionInfo(1)
                _check(kernel.SetFileInformationByHandle(handle, 4, c.byref(disposition), c.sizeof(disposition)))
                return "Deleted file."
            finally:
                kernel.CloseHandle(handle)
        temp_name = ".gamja-" + uuid.uuid4().hex + ".tmp"
        temp = _open_relative(parent, temp_name, 0x2 | 0x10000 | 0x80, disposition=2)
        renamed = False
        try:
            _write(temp, args["content"])
            try:
                existing = _open_relative(parent, name, 0x80)
            except OSError as error:
                if error.errno not in (2, 3):
                    raise
            else:
                kernel.CloseHandle(existing)
            _rename(temp, parent, name)
            renamed = True
            return "Wrote file."
        finally:
            if not renamed:
                disposition = DispositionInfo(1)
                kernel.SetFileInformationByHandle(temp, 4, c.byref(disposition), c.sizeof(disposition))
            kernel.CloseHandle(temp)
    finally:
        kernel.CloseHandle(parent)
