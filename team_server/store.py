"""Separate database schema for canonical team identities and sharing."""
from sqlalchemy import Column, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base
Base=declarative_base()

class Account(Base):
    __tablename__='accounts'
    id=Column(String(36),primary_key=True)
    username=Column(String(200),nullable=False,unique=True)
    password_hash=Column(Text,nullable=False)
    state=Column(String(16),nullable=False,default='active')
    revision=Column(Integer,nullable=False,default=1)
    failed_count=Column(Integer,nullable=False,default=0)
    locked_until=Column(Integer,nullable=False,default=0)

class Session(Base):
    __tablename__='sessions'
    id=Column(String(36),primary_key=True)
    account_id=Column(String(36),nullable=False)
    account_revision=Column(Integer,nullable=False)
    expires_at=Column(Integer,nullable=False)
    revoked=Column(Integer,nullable=False,default=0)

class Team(Base):
    __tablename__='teams'
    id=Column(String(36),primary_key=True)
    owner_id=Column(String(36),nullable=False)
    name=Column(String(200),nullable=False)
    revision=Column(Integer,nullable=False,default=1)
    revocation_epoch=Column(Integer,nullable=False,default=0)

class Member(Base):
    __tablename__='members'
    team_id=Column(String(36),primary_key=True)
    account_id=Column(String(36),primary_key=True)
    role=Column(String(16),nullable=False)
    state=Column(String(16),nullable=False)
    revision=Column(Integer,nullable=False,default=1)

class Invite(Base):
    __tablename__='invites'
    id=Column(String(36),primary_key=True)
    team_id=Column(String(36),nullable=False)
    account_id=Column(String(36),nullable=False)
    role=Column(String(16),nullable=False)
    state=Column(String(16),nullable=False)
    expires_at=Column(Integer,nullable=False)
    token_hash=Column(String(64),nullable=True)
    kind=Column(String(16),nullable=False)

class Publication(Base):
    __tablename__='publications'
    id=Column(String(36),primary_key=True)
    team_id=Column(String(36),nullable=False)
    owner_id=Column(String(36),nullable=False)
    role_text=Column(Text,nullable=False)
    knowledge_text=Column(Text,nullable=False)
    audience=Column(String(16),nullable=False)
    revision=Column(Integer,nullable=False,default=1)
    revocation_epoch=Column(Integer,nullable=False,default=0)
    state=Column(String(16),nullable=False,default='active')

class PublicationMember(Base):
    __tablename__='publication_members'
    publication_id=Column(String(36),primary_key=True)
    account_id=Column(String(36),primary_key=True)
    role=Column(String(16),nullable=False)

class AuthoritySchema(Base):
    __tablename__='authority_schema'
    id=Column(Integer,primary_key=True)
    version=Column(Integer,nullable=False)


def initialize(engine):
    """Refuse an unrelated or partially initialized database."""
    from sqlalchemy import inspect
    from sqlalchemy.orm import sessionmaker
    tables=set(inspect(engine).get_table_names())
    allowed=set(Base.metadata.tables)
    if tables and (tables-allowed or 'authority_schema' not in tables):
        raise RuntimeError('Database is not a team authority database')
    if not tables:
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            db.add(AuthoritySchema(id=1,version=1));db.commit()
    else:
        if tables!=allowed:raise RuntimeError('Incomplete team authority schema')
        with sessionmaker(bind=engine)() as db:
            row=db.get(AuthoritySchema,1)
            if row is None or row.version!=1:raise RuntimeError('Unsupported team authority schema')

class Operation(Base):
    __tablename__='operations'
    account_id=Column(String(36),primary_key=True)
    id=Column(String(36),primary_key=True)
    kind=Column(String(64),nullable=False)
    session_id=Column(String(36),nullable=False)
    team_id=Column(String(36),nullable=False)
    target_id=Column(String(36),nullable=True)
    request_digest=Column(String(64),nullable=False)
    response=Column(Text,nullable=False)
