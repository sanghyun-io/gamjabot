"""Owner-scoped non-secret contacts and separate command receipts."""
VERSION=55
REQUIRED_COLUMNS={
    'vault_contacts':{'owner_id','item_id','name','email','phone','address_line1','address_line2','city','region','postal_code','country','nickname','hint','revision','is_active','created_at','updated_at'},
    'vault_contact_commands':{'owner_id','command_key','request_digest','response'},
}
def upgrade(db):
    db.execute('''CREATE TABLE vault_contacts (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_id TEXT NOT NULL CHECK(length(item_id)=32 AND item_id NOT GLOB '*[^0-9a-f]*'),
        name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, address_line1 TEXT NOT NULL,
        address_line2 TEXT NOT NULL, city TEXT NOT NULL, region TEXT NOT NULL, postal_code TEXT NOT NULL,
        country TEXT NOT NULL, nickname TEXT NOT NULL, hint TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>0), is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(owner_id,item_id))''')
    db.execute('''CREATE TABLE vault_contact_commands (
        owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        command_key TEXT NOT NULL, request_digest TEXT NOT NULL, response TEXT NOT NULL,
        PRIMARY KEY(owner_id,command_key))''')
