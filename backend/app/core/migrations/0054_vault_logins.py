"""Owner-scoped Vault login metadata and metadata command receipts; no secrets."""
VERSION = 54
REQUIRED_COLUMNS = {
    'vault_logins': {'owner_id', 'item_id', 'website', 'username', 'nickname', 'hint', 'revision', 'is_active', 'created_at', 'updated_at'},
    'vault_login_commands': {'owner_id', 'command_key', 'request_digest', 'response'},
}


def upgrade(db):
    db.execute('''CREATE TABLE vault_logins (
        owner_id INTEGER NOT NULL REFERENCES users(id),
        item_id TEXT NOT NULL CHECK(length(item_id)=32 AND item_id NOT GLOB '*[^0-9a-f]*'),
        website TEXT NOT NULL, username TEXT NOT NULL, nickname TEXT NOT NULL, hint TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(owner_id,item_id))''')
    db.execute('''CREATE TABLE vault_login_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id), command_key TEXT NOT NULL,
        request_digest TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(owner_id,command_key))''')
