"""Per-action Owner image disclosure choice; legacy rows remain unknown."""
VERSION = 43
REQUIRED_COLUMNS = {"tool_actions": {"send_image_to_model"}}

def upgrade(db):
    db.execute("ALTER TABLE tool_actions ADD COLUMN send_image_to_model BOOLEAN")
