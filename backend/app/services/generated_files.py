"""Private immutable copies of approved write payloads, not the source files."""
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 4_000_000
_KEYS = {'version', 'ref', 'filename', 'size_bytes', 'sha256', 'media_type', 'capture_kind'}
_CREDENTIAL_NAME = re.compile(r'(?i)^(?:\.env.*|id_rsa|id_ed25519|auth\.json|credentials\.json|secrets\.json)$|\.(?:pem|key|p12|pfx)$')
_CREDENTIAL_CONTENT = re.compile(r'(?i)-----BEGIN [^-]*PRIVATE KEY-----|\bBearer\s+\S+|\b(?:token|api[_-]?key|password|secret)\s*[:=]\s*["\']?[^\s"\']+')

class GeneratedFileUnavailable(Exception):
    def __init__(self): super().__init__('Generated file unavailable')

class GeneratedFileGone(GeneratedFileUnavailable):
    pass

def _ids(room_id, action_id):
    if type(room_id) is not int or room_id <= 0 or not isinstance(action_id, str): raise GeneratedFileUnavailable()
    try:
        if str(uuid.UUID(action_id)) != action_id: raise ValueError()
    except ValueError: raise GeneratedFileUnavailable() from None
    return str(room_id), action_id

def _filename(path, action_id):
    if not isinstance(path, str): return f'generated-{action_id}.txt'
    name = path.replace('\\', '/').rsplit('/', 1)[-1]
    if not name or name in ('.', '..') or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return f'generated-{action_id}.txt'
    return name

def validate_reference(reference, room_id, action_id):
    try:
        room, action = _ids(room_id, action_id)
        if type(reference) is not dict or set(reference) != _KEYS: return False
        name = reference['filename']
        return (type(reference['version']) is int and reference['version'] == 1
            and reference['ref'] == f'generated-files/{room}/{action}.utf8'
            and isinstance(name, str) and name == _filename(name, action) and not _CREDENTIAL_NAME.search(name)
            and type(reference['size_bytes']) is int and 0 <= reference['size_bytes'] <= MAX_BYTES
            and isinstance(reference['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', reference['sha256']) is not None
            and reference['media_type'] == 'text/plain; charset=utf-8'
            and reference['capture_kind'] == 'approved_write_payload')
    except (GeneratedFileUnavailable, TypeError): return False

@dataclass(frozen=True)
class ArtifactCapture:
    public: dict
    created: bool
    owner_id: int
    room_id: int
    action_id: str
    nonce: str
    inode: int

class GeneratedFiles:
    def __init__(self, app_data):
        self.root = Path(app_data) / 'generated-files' if app_data is not None else None

    @staticmethod
    def _check(st, directory=False):
        if st.st_uid != os.geteuid() or (stat.S_IMODE(st.st_mode) & 0o077) or (directory and not stat.S_ISDIR(st.st_mode)) or (not directory and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1)):
            raise GeneratedFileUnavailable()

    @contextmanager
    def _locked(self, room_id):
        if self.root is None: raise GeneratedFileUnavailable()
        fds = []
        try:
            # app_data must already be an owned private directory; never traverse symlinks.
            parent = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fds.append(parent)
            if os.fstat(parent).st_uid != os.geteuid(): raise GeneratedFileUnavailable()
            try: os.mkdir(self.root.name, 0o700, dir_fd=parent)
            except FileExistsError: pass
            root = os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            fds.append(root); self._check(os.fstat(root), True)
            lock = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root)
            fds.append(lock); self._check(os.fstat(lock)); fcntl.flock(lock, fcntl.LOCK_EX)
            try: os.mkdir(str(room_id), 0o700, dir_fd=root)
            except FileExistsError: pass
            room = os.open(str(room_id), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            fds.append(room); self._check(os.fstat(room), True)
            yield room
        except GeneratedFileUnavailable: raise
        except (OSError, ValueError, TypeError, UnicodeError): raise GeneratedFileUnavailable() from None
        finally:
            for fd in reversed(fds): os.close(fd)

    def _load(self, room, action):
        try: fd = os.open(action + '.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=room)
        except FileNotFoundError: return None, None
        try:
            st = os.fstat(fd); self._check(st)
            if st.st_size > MAX_BYTES * 2 + 4096: raise GeneratedFileUnavailable()
            data = b''
            while len(data) <= MAX_BYTES * 2 + 4096:
                part = os.read(fd, min(65536, MAX_BYTES * 2 + 4097 - len(data)))
                if not part: break
                data += part
            if len(data) > MAX_BYTES * 2 + 4096: raise GeneratedFileUnavailable()
            return json.loads(data), st.st_ino
        except (ValueError, UnicodeError): raise GeneratedFileUnavailable() from None
        finally: os.close(fd)

    def _verify(self, record, owner_id, room_id, action_id, reference):
        if not validate_reference(reference, room_id, action_id) or type(owner_id) is not int or owner_id <= 0 or not isinstance(record, dict): raise GeneratedFileUnavailable()
        if record.get('owner') != owner_id or record.get('room') != room_id or record.get('action') != action_id or record.get('public') != reference: raise GeneratedFileUnavailable()
        if record.get('deleted') is True: raise GeneratedFileGone()
        try: raw = base64.b64decode(record['content'], validate=True)
        except (ValueError, KeyError, TypeError): raise GeneratedFileUnavailable() from None
        if len(raw) != reference['size_bytes'] or hashlib.sha256(raw).hexdigest() != reference['sha256']: raise GeneratedFileUnavailable()
        try: raw.decode('utf-8')
        except UnicodeError: raise GeneratedFileUnavailable() from None
        return raw

    def capture(self, owner_id, room_id, action_id, path, content):
        room, action = _ids(room_id, action_id)
        if type(owner_id) is not int or owner_id <= 0 or not isinstance(content, str): raise GeneratedFileUnavailable()
        try: raw = content.encode('utf-8')
        except UnicodeError: return None
        name = _filename(path, action)
        original_name = path.replace('\\', '/').rsplit('/', 1)[-1] if isinstance(path, str) else ''
        if len(raw) > MAX_BYTES or _CREDENTIAL_NAME.search(original_name) or _CREDENTIAL_CONTENT.search(content): return None
        public = {'version':1, 'ref':f'generated-files/{room}/{action}.utf8', 'filename':name, 'size_bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest(), 'media_type':'text/plain; charset=utf-8', 'capture_kind':'approved_write_payload'}
        with self._locked(room) as directory:
            old, inode = self._load(directory, action)
            if old is not None:
                self._verify(old, owner_id, room_id, action_id, public)
                return ArtifactCapture(public, False, owner_id, room_id, action_id, old['nonce'], inode)
            nonce = uuid.uuid4().hex
            record = {'owner':owner_id, 'room':room_id, 'action':action_id, 'public':public, 'nonce':nonce, 'deleted':False, 'content':base64.b64encode(raw).decode('ascii')}
            fd = None
            try:
                fd = os.open(action + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                inode = os.fstat(fd).st_ino
                data = json.dumps(record, separators=(',', ':')).encode()
                while data: data = data[os.write(fd, data):]
                os.fsync(fd)
            except BaseException:
                if fd is not None:
                    try:
                        if os.stat(action + '.json', dir_fd=directory, follow_symlinks=False).st_ino == inode: os.unlink(action + '.json', dir_fd=directory)
                    except OSError: pass
                raise
            finally:
                if fd is not None: os.close(fd)
            return ArtifactCapture(public, True, owner_id, room_id, action_id, nonce, inode)

    def read(self, owner_id, room_id, action_id, reference):
        room, action = _ids(room_id, action_id)
        with self._locked(room) as directory:
            record, _ = self._load(directory, action)
            return self._verify(record, owner_id, room_id, action_id, reference)

    def delete(self, owner_id, room_id, action_id, reference):
        room, action = _ids(room_id, action_id)
        with self._locked(room) as directory:
            record, _ = self._load(directory, action)
            self._verify(record, owner_id, room_id, action_id, reference)
            record['deleted'] = True; record.pop('content')
            temp = '.' + uuid.uuid4().hex + '.tmp'; fd = None
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                data = json.dumps(record, separators=(',', ':')).encode()
                while data: data = data[os.write(fd, data):]
                os.fsync(fd); os.replace(temp, action + '.json', src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                if fd is not None: os.close(fd)
                try: os.unlink(temp, dir_fd=directory)
                except FileNotFoundError: pass

    def discard(self, capture):
        if not isinstance(capture, ArtifactCapture) or not capture.created: return
        room, action = _ids(capture.room_id, capture.action_id)
        with self._locked(room) as directory:
            record, inode = self._load(directory, action)
            if inode != capture.inode or not isinstance(record, dict) or record.get('nonce') != capture.nonce: return
            try: self._verify(record, capture.owner_id, capture.room_id, capture.action_id, capture.public)
            except GeneratedFileUnavailable: return
            os.unlink(action + '.json', dir_fd=directory)
