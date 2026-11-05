"""Separate package provenance from completed-request Skill sources."""
VERSION = 41
REQUIRED_COLUMNS = {'owner_skills': {'package_provenance'}}

def upgrade(db):
    db.execute('ALTER TABLE owner_skills ADD COLUMN package_provenance JSON')
