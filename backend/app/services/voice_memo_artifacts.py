"""Private, bounded audio records. This module does not authorize synthesis.

The broker may publish records only after a reviewed Core completion.
There is no HTTP write endpoint; this module is not an authority boundary.
"""
import hashlib
import io
import json
import os
import re
import uuid
import wave
from pathlib import Path

from app.services.generated_files import GeneratedFiles, GeneratedFileUnavailable

MAX_BYTES = 10 * 1024 * 1024
MAX_DURATION_MS = 300_000
MAX_TEXT_BYTES = 4000
HEADER_LIMIT = 16_384

class VoiceMemoUnavailable(Exception):
    pass

class VoiceMemoGone(VoiceMemoUnavailable):
    pass

class VoiceMemoConflict(VoiceMemoUnavailable):
    pass


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise VoiceMemoUnavailable() from None
    return value


def _positive(value):
    if type(value) is not int or value <= 0:
        raise VoiceMemoUnavailable()
    return value


def _metadata(meta, owner_id, room_id, action_id):
    required = {'version', 'owner_id', 'room_id', 'agent_id', 'trigger_event_id', 'action_id',
                'media_type', 'size_bytes', 'sha256', 'duration_ms', 'transcript',
                'voice_id', 'locale', 'rate_milli', 'text_sha256', 'review_id', 'grant_id', 'generation', 'deleted'}
    if not isinstance(meta, dict) or set(meta) != required or type(meta['version']) is not int or meta['version'] != 1:
        raise VoiceMemoUnavailable()
    if (meta['owner_id'], meta['room_id'], meta['action_id']) != (owner_id, room_id, action_id):
        raise VoiceMemoUnavailable()
    _positive(meta['owner_id']); _positive(meta['room_id']); _positive(meta['agent_id'])
    _id(meta['action_id']); _id(meta['trigger_event_id']); _id(meta['review_id'])
    _positive(meta['grant_id']); _positive(meta['generation'])
    if (meta['media_type'] != 'audio/wav' or type(meta['size_bytes']) is not int
            or not 1 <= meta['size_bytes'] <= MAX_BYTES or type(meta['duration_ms']) is not int
            or not 1 <= meta['duration_ms'] <= MAX_DURATION_MS or type(meta['deleted']) is not bool
            or not isinstance(meta['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', meta['sha256'])
            or not isinstance(meta['transcript'], str) or not meta['transcript'].strip()
            or len(meta['transcript'].encode('utf-8')) > MAX_TEXT_BYTES
            or not isinstance(meta['voice_id'], str) or not 1 <= len(meta['voice_id']) <= 200
            or not isinstance(meta['locale'], str) or not 1 <= len(meta['locale']) <= 64
            or type(meta['rate_milli']) is not int or not 1 <= meta['rate_milli'] <= 1000
            or not isinstance(meta['text_sha256'], str)
            or hashlib.sha256(meta['transcript'].encode('utf-8')).hexdigest() != meta['text_sha256']):
        raise VoiceMemoUnavailable()
    return meta


def _wav_duration(raw):
    try:
        with wave.open(io.BytesIO(raw), 'rb') as stream:
            channels, width, rate, frames = stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()
            if (stream.getcomptype() != 'NONE' or channels not in (1, 2) or width not in (1, 2, 3, 4)
                    or channels != 1 or width != 2 or rate != 22050 or frames <= 0):
                raise VoiceMemoUnavailable()
            if len(stream.readframes(frames)) != frames * channels * width:
                raise VoiceMemoUnavailable()
            duration = frames * 1000 // rate
            if not 1 <= duration <= MAX_DURATION_MS:
                raise VoiceMemoUnavailable()
            return duration
    except (wave.Error, EOFError, OSError, ValueError):
        raise VoiceMemoUnavailable() from None


class VoiceMemoArtifacts(GeneratedFiles):
    def __init__(self, app_data):
        self.root = Path(app_data) / 'voice-memos' if app_data is not None else None

    def _load_record(self, directory, owner, room, action):
        try:
            fd = os.open(action + '.memo', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            raise VoiceMemoUnavailable() from None
        try:
            st = os.fstat(fd); self._check(st)
            if st.st_size > MAX_BYTES + HEADER_LIMIT + 1:
                raise VoiceMemoUnavailable()
            header = bytearray()
            while len(header) <= HEADER_LIMIT:
                char = os.read(fd, 1)
                if not char: raise VoiceMemoUnavailable()
                if char == b'\n': break
                header.extend(char)
            else: raise VoiceMemoUnavailable()
            meta = _metadata(json.loads(header), owner, room, action)
            data = bytearray()
            while len(data) <= MAX_BYTES:
                part = os.read(fd, min(65536, MAX_BYTES + 1 - len(data)))
                if not part: break
                data.extend(part)
            if meta['deleted']:
                if data: raise VoiceMemoUnavailable()
                return meta, b''
            raw = bytes(data)
            if (len(raw) != meta['size_bytes'] or hashlib.sha256(raw).hexdigest() != meta['sha256']
                    or _wav_duration(raw) != meta['duration_ms']):
                raise VoiceMemoUnavailable()
            return meta, raw
        except (ValueError, UnicodeError, OSError, GeneratedFileUnavailable):
            raise VoiceMemoUnavailable() from None
        finally:
            os.close(fd)

    def _write_record(self, directory, action, meta, raw, *, replace=False):
        temporary = '.' + uuid.uuid4().hex + '.tmp'
        fd = None
        try:
            header = json.dumps(meta, ensure_ascii=True, separators=(',', ':')).encode() + b'\n'
            if len(header) > HEADER_LIMIT: raise VoiceMemoUnavailable()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            for chunk in (header, raw):
                view = memoryview(chunk)
                while view: view = view[os.write(fd, view):]
            os.fsync(fd)
            if replace: os.replace(temporary, action + '.memo', src_dir_fd=directory, dst_dir_fd=directory)
            else: os.link(temporary, action + '.memo', src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.fsync(directory)
        finally:
            if fd is not None: os.close(fd)
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass

    def capture(self, meta, raw):
        """Store a caller-verified Core result; not itself a receipt or authorization check."""
        if not isinstance(meta, dict): raise VoiceMemoUnavailable()
        owner = _positive(meta.get('owner_id')); room = _positive(meta.get('room_id')); action = _id(meta.get('action_id'))
        _metadata(meta, owner, room, action)
        if (meta['deleted'] or type(raw) is not bytes or len(raw) != meta['size_bytes']
                or hashlib.sha256(raw).hexdigest() != meta['sha256'] or _wav_duration(raw) != meta['duration_ms']):
            raise VoiceMemoUnavailable()
        try:
            with self._locked(room) as directory:
                try: old, saved = self._load_record(directory, owner, room, action)
                except VoiceMemoUnavailable:
                    try: os.stat(action + '.memo', dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError: pass
                    else: raise
                else:
                    if old['deleted']: raise VoiceMemoGone()
                    if old != meta or saved != raw: raise VoiceMemoConflict()
                    return dict(old), False
                self._write_record(directory, action, meta, raw)
                return dict(meta), True
        except (GeneratedFileUnavailable, OSError):
            raise VoiceMemoUnavailable() from None

    def read(self, owner_id, room_id, action_id):
        owner = _positive(owner_id); room = _positive(room_id); action = _id(action_id)
        try:
            with self._locked(room) as directory:
                meta, raw = self._load_record(directory, owner, room, action)
                if meta['deleted']: raise VoiceMemoGone()
                return meta, raw
        except (GeneratedFileUnavailable, OSError):
            raise VoiceMemoUnavailable() from None

    def delete(self, owner_id, room_id, action_id, expected):
        owner = _positive(owner_id); room = _positive(room_id); action = _id(action_id)
        try:
            with self._locked(room) as directory:
                meta, _ = self._load_record(directory, owner, room, action)
                if {**meta, 'deleted':False} != expected: raise VoiceMemoUnavailable()
                if not meta['deleted']:
                    meta = {**meta, 'deleted':True}
                    self._write_record(directory, action, meta, b'', replace=True)
                return meta
        except (GeneratedFileUnavailable, OSError):
            raise VoiceMemoUnavailable() from None
