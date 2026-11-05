"""
Database configuration and session management
"""
from starlette.requests import HTTPConnection
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def get_db(connection: HTTPConnection):
    """Dependency for getting database session"""
    db = connection.app.state.session_factory()
    db.info['app_state'] = connection.app.state
    try:
        yield db
    finally:
        db.close()
