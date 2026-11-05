"""Versioned, non-secret avatar recipes. No rendering data or external URLs."""
import secrets

PART_COUNTS = {'head': 10, 'hair': 12, 'eyes': 12, 'nose': 8, 'mouth': 12, 'skin': 12, 'accent': 10}

def random_avatar():
    return {'version': 1, **{part: secrets.randbelow(count) for part, count in PART_COUNTS.items()}}
