"""Owner private skill library with immutable revisions and explicit Bot bindings."""
VERSION=39
REQUIRED_COLUMNS={'owner_skills':{'skill_id','owner_id','revision','name','description','content','active','source','created_at','updated_at'},
 'owner_skill_versions':{'skill_id','revision','snapshot'},'owner_skill_bindings':{'owner_id','agent_id','skill_id','revision','enabled'},
 'owner_skill_commands':{'owner_id','command_key','request_digest','response'}}

def upgrade(db):
    db.execute('''CREATE TABLE owner_skills (skill_id VARCHAR(36) PRIMARY KEY,owner_id INTEGER NOT NULL REFERENCES users(id),
        revision INTEGER NOT NULL CHECK(revision>=1),name VARCHAR(120) NOT NULL,description VARCHAR(500) NOT NULL,
        content TEXT NOT NULL,active BOOLEAN NOT NULL CHECK(active IN (0,1)),source JSON,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL)''')
    db.execute('''CREATE TABLE owner_skill_versions (skill_id VARCHAR(36) NOT NULL REFERENCES owner_skills(skill_id),
        revision INTEGER NOT NULL CHECK(revision>=1),snapshot JSON NOT NULL,PRIMARY KEY(skill_id,revision))''')
    db.execute('''CREATE TABLE owner_skill_bindings (owner_id INTEGER NOT NULL REFERENCES users(id),agent_id INTEGER NOT NULL REFERENCES agents(id),
        skill_id VARCHAR(36) NOT NULL REFERENCES owner_skills(skill_id),revision INTEGER NOT NULL CHECK(revision>=1),
        enabled BOOLEAN NOT NULL CHECK(enabled IN (0,1)),PRIMARY KEY(owner_id,agent_id,skill_id))''')
    db.execute('''CREATE TABLE owner_skill_commands (owner_id INTEGER NOT NULL REFERENCES users(id),command_key VARCHAR(100) NOT NULL,
        request_digest VARCHAR(64) NOT NULL,response JSON NOT NULL,PRIMARY KEY(owner_id,command_key))''')
