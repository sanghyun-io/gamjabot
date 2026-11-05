"""Owner MCP configuration, discovery ledger, and Bot-specific bindings."""
VERSION=42
REQUIRED_COLUMNS={
 'mcp_servers':{'server_id','owner_id','revision','name','enabled','transport','credential_ref','created_at','updated_at'},
 'mcp_tools':{'tool_id','server_id','server_revision','name','description','input_schema','schema_canonical','schema_sha256'},
 'mcp_bindings':{'owner_id','agent_id','server_id','revision','enabled','tool_ids'},
 'mcp_commands':{'owner_id','command_key','request_digest','response'},
 'tool_actions':{'mcp_binding'},
 'mcp_discoveries':{'discovery_id','owner_id','server_id','server_revision','preview_id','transport','executable_review','state','failure_code','created_at','expires_at','completed_at'}}
def upgrade(db):
    db.execute('ALTER TABLE tool_actions ADD COLUMN mcp_binding JSON')
    db.execute('''CREATE TABLE mcp_servers (server_id VARCHAR(36) PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),revision INTEGER NOT NULL,name VARCHAR(120) NOT NULL,enabled BOOLEAN NOT NULL,transport JSON NOT NULL,credential_ref VARCHAR(200),created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL)''')
    db.execute('''CREATE TABLE mcp_tools (tool_id VARCHAR(64) PRIMARY KEY,server_id VARCHAR(36) NOT NULL REFERENCES mcp_servers(server_id),server_revision INTEGER NOT NULL,name VARCHAR(200) NOT NULL,description TEXT NOT NULL,input_schema JSON NOT NULL,schema_canonical TEXT NOT NULL,schema_sha256 VARCHAR(64) NOT NULL,UNIQUE(server_id,server_revision,name))''')
    db.execute('''CREATE TABLE mcp_bindings (owner_id INTEGER NOT NULL REFERENCES users(id),agent_id INTEGER NOT NULL REFERENCES agents(id),server_id VARCHAR(36) NOT NULL REFERENCES mcp_servers(server_id),revision INTEGER NOT NULL,enabled BOOLEAN NOT NULL,tool_ids JSON,PRIMARY KEY(owner_id,agent_id,server_id))''')
    db.execute('''CREATE TABLE mcp_commands (owner_id INTEGER NOT NULL REFERENCES users(id),command_key VARCHAR(100) NOT NULL,request_digest VARCHAR(64) NOT NULL,response JSON NOT NULL,PRIMARY KEY(owner_id,command_key))''')
    db.execute('''CREATE TABLE mcp_discoveries (discovery_id VARCHAR(36) PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),server_id VARCHAR(36) NOT NULL REFERENCES mcp_servers(server_id),server_revision INTEGER NOT NULL,preview_id VARCHAR(36) NOT NULL UNIQUE,transport JSON NOT NULL,executable_review JSON,state VARCHAR(20) NOT NULL,failure_code VARCHAR(64),created_at DATETIME NOT NULL,expires_at DATETIME NOT NULL,completed_at DATETIME)''')
