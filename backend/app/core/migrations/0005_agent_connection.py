"""Bind Agents to exact saved connections while retaining legacy model names."""

VERSION = 5
REQUIRED_COLUMNS = {"agents": {"connection_id", "model_id"}}


def upgrade(db):
    db.execute("ALTER TABLE agents ADD COLUMN connection_id INTEGER REFERENCES provider_connections(id)")
    db.execute("ALTER TABLE agents ADD COLUMN model_id VARCHAR")
    db.execute("UPDATE agents SET model_id=model")
    db.execute("CREATE INDEX ix_agents_connection_id ON agents(connection_id)")
