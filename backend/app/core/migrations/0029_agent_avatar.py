"""Add one saved avatar recipe per Agent without changing prior columns."""
import json
from app.core.agent_avatar import random_avatar

VERSION = 29
REQUIRED_COLUMNS = {'agents': {'id', 'user_id', 'avatar'}}

def upgrade(db):
    db.execute('ALTER TABLE agents ADD COLUMN avatar JSON')
    for (agent_id,) in db.execute('SELECT id FROM agents ORDER BY id').fetchall():
        db.execute('UPDATE agents SET avatar=? WHERE id=?', (json.dumps(random_avatar(), separators=(',', ':')), agent_id))
