import hashlib
import uuid
import pytest
from app.services.library_uploads import LibraryUploads, LibraryUploadUnavailable, LibraryUploadGone, LibraryUploadConflict

def digest(data): return hashlib.sha256(data).hexdigest()

def test_upload_binary_identity_replay_list_and_tombstone(tmp_path):
    store = LibraryUploads(tmp_path); uid = str(uuid.uuid4()); data = b'%PDF-1.7\x00\xff'
    assert not store.root.exists()
    public, created = store.put(1, 2, uid, 'report.pdf', data, digest(data))
    assert created and public['origin'] == 'owner_upload' and not public['removed']
    assert store.read(1, 2, uid) == data
    assert store.put(1, 2, uid, 'report.pdf', data, digest(data)) == (public, False)
    with pytest.raises(LibraryUploadConflict): store.put(1, 2, uid, 'other.pdf', data, digest(data))
    with pytest.raises(LibraryUploadUnavailable): store.read(3, 2, uid)
    with pytest.raises(LibraryUploadUnavailable): store.read(1, 3, uid)
    assert store.list(1, 2)['items'] == [public]
    assert store.delete(1, 2, uid)['removed']
    assert store.delete(1, 2, uid)['removed']
    with pytest.raises(LibraryUploadGone): store.read(1, 2, uid)
    with pytest.raises(LibraryUploadGone): store.put(1, 2, uid, 'report.pdf', data, digest(data))

def test_integrity_symlink_and_empty(tmp_path):
    store = LibraryUploads(tmp_path); uid = str(uuid.uuid4())
    public, _ = store.put(1, 2, uid, '../empty', b'', digest(b''))
    assert public['filename'] == 'empty' and store.read(1, 2, uid) == b''
    file = store.root / '2' / (uid + '.upload')
    with file.open('ab') as out: out.write(b'bad')
    with pytest.raises(LibraryUploadUnavailable): store.read(1, 2, uid)
    file.unlink(); file.symlink_to('/etc/passwd')
    with pytest.raises(LibraryUploadUnavailable): store.read(1, 2, uid)
    with pytest.raises(LibraryUploadUnavailable): store.put(1, 2, uid, 'empty', b'', digest(b''))

@pytest.mark.asyncio
async def test_file_tools_cannot_access_upload_root(tmp_path):
    from types import SimpleNamespace
    from app.services.tools import LocalExecutor
    executor = LocalExecutor(database_path=str(tmp_path / 'app.sqlite'))
    try:
        for path in (tmp_path, executor.library_uploads.root, executor.library_uploads.root / 'child'):
            result = await executor._execute(SimpleNamespace(family='read_file',arguments={'path':str(path)}),None,None)
            assert result.state == 'denied'
    finally: await executor.close()
