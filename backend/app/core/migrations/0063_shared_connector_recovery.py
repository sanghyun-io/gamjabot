"""Fence interrupted global-clear handlers by Engine instance."""
VERSION=63
REQUIRED_COLUMNS={'shared_connector_clear_owners':{'owner_id','issuer','subject','key_fingerprint','operation_id','instance_id','recovery_id','recovery_instance_id'}}
def upgrade(db):
 db.execute("""CREATE TABLE shared_connector_clear_owners (owner_id INTEGER NOT NULL REFERENCES users(id),issuer TEXT NOT NULL,subject TEXT NOT NULL,key_fingerprint TEXT NOT NULL,operation_id TEXT NOT NULL,instance_id TEXT NOT NULL,recovery_id TEXT,recovery_instance_id TEXT,PRIMARY KEY(owner_id,issuer,subject,key_fingerprint))""")
 db.execute("""INSERT INTO shared_connector_clear_owners (owner_id,issuer,subject,key_fingerprint,operation_id,instance_id) SELECT owner_id,issuer,subject,key_fingerprint,operation_id,'legacy' FROM shared_connector_clear_barriers""")
