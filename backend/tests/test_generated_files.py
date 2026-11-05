import os
import uuid
import pytest
from app.services.generated_files import GeneratedFiles, GeneratedFileUnavailable, GeneratedFileGone, validate_reference

@pytest.fixture
def item(tmp_path):
    return GeneratedFiles(tmp_path), str(uuid.uuid4())

def test_capture_replay_identity_delete_and_rollback(item):
    store, action = item
    first = store.capture(1, 2, action, '/unread/source.txt', 'hello 한글')
    assert first.created and validate_reference(first.public, 2, action)
    assert store.read(1, 2, action, first.public) == 'hello 한글'.encode()
    replay = store.capture(1, 2, action, '/unread/source.txt', 'hello 한글')
    assert not replay.created
    store.discard(replay)
    with pytest.raises(GeneratedFileUnavailable): store.read(3, 2, action, first.public)
    with pytest.raises(GeneratedFileUnavailable): store.read(1, 3, action, first.public)
    with pytest.raises(GeneratedFileUnavailable): store.capture(1, 2, action, '/unread/source.txt', 'changed')
    store.delete(1, 2, action, first.public)
    with pytest.raises(GeneratedFileGone): store.read(1, 2, action, first.public)
    with pytest.raises(GeneratedFileGone): store.capture(1, 2, action, '/unread/source.txt', 'hello 한글')
    store.discard(first)
    with pytest.raises(GeneratedFileGone): store.read(1, 2, action, first.public)
    other = str(uuid.uuid4()); created = store.capture(1, 2, other, 'x', 'text')
    store.discard(created)
    with pytest.raises(GeneratedFileUnavailable): store.read(1, 2, other, created.public)

def test_credential_filter(item):
    store, action = item
    for filename, content in [('.env.production', 'x'), ('id_rsa', 'x'), ('secret.pem', 'x'), ('x', 'password=abc'), ('x', 'Bearer abc'), ('x', '-----BEGIN PRIVATE KEY-----')]:
        assert store.capture(1, 2, action, filename, content) is None

def test_tamper_and_symlink_denied(item):
    store, action = item
    capture = store.capture(1, 2, action, 'x', 'text')
    file = store.root / '2' / (action + '.json')
    file.write_bytes(file.read_bytes().replace(b'dGV4dA==', b'dGV4dAAB'))
    with pytest.raises(GeneratedFileUnavailable): store.read(1, 2, action, capture.public)
    file.unlink(); file.symlink_to('/etc/passwd')
    with pytest.raises(GeneratedFileUnavailable): store.read(1, 2, action, capture.public)
    assert not validate_reference(dict(capture.public, size_bytes=True), 2, action)
