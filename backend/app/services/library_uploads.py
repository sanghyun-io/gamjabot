"""Owner uploaded bytes, private to an owned Bot; not model ingestion."""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from app.services.generated_files import GeneratedFiles, GeneratedFileUnavailable

MAX_BYTES = 20 * 1024 * 1024
HEADER_LIMIT = 4096

class LibraryUploadUnavailable(Exception):
    def __init__(self): super().__init__('Upload unavailable')
class LibraryUploadGone(LibraryUploadUnavailable): pass
class LibraryUploadMissing(LibraryUploadUnavailable): pass
class LibraryUploadConflict(Exception):
    def __init__(self): super().__init__('Upload conflicts with existing record')

def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value: raise ValueError()
    except ValueError: raise LibraryUploadUnavailable() from None
    return value

def _positive(value):
    if type(value) is not int or value <= 0: raise LibraryUploadUnavailable()
    return value

def _name(value, upload_id):
    fallback = f'upload-{upload_id}'
    if not isinstance(value, str): return fallback
    name = value.replace('\\', '/').rsplit('/', 1)[-1]
    try: name.encode('utf-8')
    except UnicodeError: return fallback
    if not name or name in ('.', '..') or len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name): return fallback
    return name

class LibraryUploads(GeneratedFiles):
    def __init__(self, app_data):
        self.root = Path(app_data) / 'library-uploads' if app_data is not None else None

    def _record(self, directory, upload_id):
        try: fd = os.open(upload_id + '.upload', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError: raise LibraryUploadMissing() from None
        try:
            info = os.fstat(fd); self._check(info)
            if info.st_size > MAX_BYTES + HEADER_LIMIT + 1: raise LibraryUploadUnavailable()
            header = b''
            while len(header) <= HEADER_LIMIT:
                b = os.read(fd, 1)
                if not b: raise LibraryUploadUnavailable()
                if b == b'\n': break
                header += b
            else: raise LibraryUploadUnavailable()
            meta = json.loads(header)
            if not isinstance(meta, dict): raise LibraryUploadUnavailable()
            data = bytearray()
            while len(data) <= MAX_BYTES:
                part = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
                if not part: break
                data.extend(part)
            if len(data) > MAX_BYTES: raise LibraryUploadUnavailable()
            return meta, bytes(data)
        except (ValueError, UnicodeError): raise LibraryUploadUnavailable() from None
        finally: os.close(fd)

    def _verified(self, directory, owner, bot, upload_id):
        meta, data = self._record(directory, upload_id)
        required = {'owner_id','bot_id','upload_id','filename','size_bytes','sha256','created_at','deleted'}
        if (set(meta) != required or type(meta['owner_id']) is not int or meta['owner_id'] != owner
            or type(meta['bot_id']) is not int or meta['bot_id'] != bot or meta['upload_id'] != upload_id
            or not isinstance(meta['filename'], str) or _name(meta['filename'], upload_id) != meta['filename']
            or type(meta['size_bytes']) is not int or not 0 <= meta['size_bytes'] <= MAX_BYTES
            or not isinstance(meta['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', meta['sha256'])
            or not isinstance(meta['created_at'], str) or type(meta['deleted']) is not bool): raise LibraryUploadUnavailable()
        try:
            stamp = datetime.fromisoformat(meta['created_at'])
            if stamp.tzinfo is None: raise ValueError()
        except (ValueError, TypeError): raise LibraryUploadUnavailable() from None
        if meta['deleted']:
            if data: raise LibraryUploadUnavailable()
        elif len(data) != meta['size_bytes'] or hashlib.sha256(data).hexdigest() != meta['sha256']:
            raise LibraryUploadUnavailable()
        return meta, data

    @staticmethod
    def _public(meta):
        return {k:meta[k] for k in ('upload_id','bot_id','filename','size_bytes','sha256','created_at')} | {'media_type':'application/octet-stream','origin':'owner_upload','removed':meta['deleted']}

    def _write(self, directory, upload_id, meta, data, *, replace=False):
        temp = '.' + uuid.uuid4().hex + '.tmp'; fd = None
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            header = json.dumps(meta, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
            if len(header) > HEADER_LIMIT: raise LibraryUploadUnavailable()
            for chunk in (header, data):
                view = memoryview(chunk)
                while view: view = view[os.write(fd, view):]
            os.fsync(fd)
            if replace:
                os.replace(temp, upload_id + '.upload', src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(temp, upload_id + '.upload', src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        finally:
            if fd is not None: os.close(fd)
            try: os.unlink(temp, dir_fd=directory)
            except FileNotFoundError: pass
        os.fsync(directory)

    def put(self, owner_id, bot_id, upload_id, filename, content, sha256):
        owner = _positive(owner_id); bot = _positive(bot_id); upload = _id(upload_id)
        if (type(content) is not bytes or len(content) > MAX_BYTES or not isinstance(sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', sha256) or hashlib.sha256(content).hexdigest() != sha256): raise LibraryUploadUnavailable()
        name = _name(filename, upload)
        try:
            with self._locked(bot) as directory:
                try: meta, data = self._verified(directory, owner, bot, upload)
                except LibraryUploadUnavailable:
                    # Only a genuinely absent entry can be created; malformed or another owner's entry fails closed.
                    try: os.stat(upload + '.upload', dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError: pass
                    else: raise
                else:
                    if meta['deleted']: raise LibraryUploadGone()
                    if meta['filename'] != name or meta['sha256'] != sha256 or data != content: raise LibraryUploadConflict()
                    return self._public(meta), False
                meta = {'owner_id':owner,'bot_id':bot,'upload_id':upload,'filename':name,'size_bytes':len(content),
                        'sha256':sha256,'created_at':datetime.now(timezone.utc).isoformat(),'deleted':False}
                self._write(directory,upload,meta,content)
                return self._public(meta), True
        except (GeneratedFileUnavailable, OSError): raise LibraryUploadUnavailable() from None

    def detail(self, owner_id, bot_id, upload_id):
        owner, bot, upload = _positive(owner_id), _positive(bot_id), _id(upload_id)
        try:
            with self._locked(bot) as directory: return self._public(self._verified(directory,owner,bot,upload)[0])
        except (GeneratedFileUnavailable, OSError): raise LibraryUploadUnavailable() from None

    def read(self, owner_id, bot_id, upload_id):
        owner, bot, upload = _positive(owner_id), _positive(bot_id), _id(upload_id)
        try:
            with self._locked(bot) as directory:
                meta, data = self._verified(directory,owner,bot,upload)
                if meta['deleted']: raise LibraryUploadGone()
                return data
        except (GeneratedFileUnavailable, OSError): raise LibraryUploadUnavailable() from None

    def delete(self, owner_id, bot_id, upload_id):
        owner, bot, upload = _positive(owner_id), _positive(bot_id), _id(upload_id)
        try:
            with self._locked(bot) as directory:
                meta, _ = self._verified(directory,owner,bot,upload)
                if not meta['deleted']:
                    meta['deleted'] = True
                    self._write(directory,upload,meta,b'',replace=True)
                return self._public(meta)
        except (GeneratedFileUnavailable, OSError): raise LibraryUploadUnavailable() from None

    def list(self, owner_id, bot_id, *, after=None, limit=50):
        owner, bot = _positive(owner_id), _positive(bot_id)
        if after is not None: _id(after)
        if type(limit) is not int or not 1 <= limit <= 50: raise LibraryUploadUnavailable()
        try:
            with self._locked(bot) as directory:
                ids = sorted(n[:-7] for n in os.listdir(directory) if n.endswith('.upload'))
                ids = [n for n in ids if after is None or n > after]
                items = []
                for upload in ids:
                    _id(upload)
                    meta, _ = self._verified(directory, owner, bot, upload)
                    items.append(self._public(meta))
                    if len(items) > limit: break
                return {'items':items[:limit], 'next_cursor':items[limit-1]['upload_id'] if len(items)>limit else None}
        except (GeneratedFileUnavailable, OSError): raise LibraryUploadUnavailable() from None
