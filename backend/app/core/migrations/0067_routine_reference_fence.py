"""Durable, content-free rejection receipts for routine-reference messages."""
VERSION=67
REQUIRED_COLUMNS={'routine_reference_fences':{'owner_id','room_id','idempotency_key','request_digest','code','created_at'}}
def upgrade(db):
 db.execute('CREATE TABLE routine_reference_fences (owner_id INTEGER NOT NULL REFERENCES users(id),room_id INTEGER NOT NULL REFERENCES conversations(id),idempotency_key VARCHAR(200) NOT NULL,request_digest VARCHAR(64) NOT NULL,code VARCHAR(80) NOT NULL,created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(owner_id,room_id,idempotency_key),CHECK(length(idempotency_key) BETWEEN 1 AND 200),CHECK(length(request_digest)=64))')
