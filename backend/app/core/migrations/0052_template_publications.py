"""Owner publication configuration and durable remote command journal."""
VERSION=52
REQUIRED_COLUMNS={
    'template_publisher_configs':{'owner_id','registry_id','origin','publisher_id','revision','secret_ref'},
    'template_publisher_secret_cleanup':{'owner_id','secret_ref'},
    'template_publications':{'owner_id','bot_id','registry_id','publisher_id','origin','template_id','revision','revoked','snapshot_sha256'},
    'template_publication_commands':{'owner_id','bot_id','command_key','request_digest','payload','registry_id','publisher_id','origin','state','response'},
}

def upgrade(db):
    db.execute('''CREATE TABLE template_publisher_configs(owner_id INTEGER PRIMARY KEY REFERENCES users(id), registry_id INTEGER NOT NULL UNIQUE, origin TEXT NOT NULL, publisher_id TEXT NOT NULL, revision INTEGER NOT NULL, secret_ref TEXT NOT NULL)''')
    db.execute('''CREATE TABLE template_publisher_secret_cleanup(owner_id INTEGER NOT NULL REFERENCES users(id), secret_ref TEXT NOT NULL, PRIMARY KEY(owner_id,secret_ref))''')
    db.execute('''CREATE TABLE template_publications(owner_id INTEGER NOT NULL REFERENCES users(id), bot_id INTEGER NOT NULL REFERENCES agents(id), registry_id INTEGER NOT NULL, publisher_id TEXT NOT NULL, origin TEXT NOT NULL, template_id TEXT NOT NULL, revision INTEGER NOT NULL, revoked INTEGER NOT NULL, snapshot_sha256 TEXT, PRIMARY KEY(owner_id,bot_id))''')
    db.execute('''CREATE TABLE template_publication_commands(owner_id INTEGER NOT NULL REFERENCES users(id), bot_id INTEGER NOT NULL REFERENCES agents(id), command_key TEXT NOT NULL, request_digest TEXT NOT NULL, payload TEXT NOT NULL, registry_id INTEGER NOT NULL, publisher_id TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL, response TEXT, PRIMARY KEY(owner_id,command_key))''')
    db.execute("CREATE UNIQUE INDEX template_publication_pending ON template_publication_commands(owner_id,bot_id) WHERE state IN ('prepared','unknown','not_found')")
