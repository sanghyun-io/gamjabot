"""Explicit bootstrap of an account; password is read interactively, never argv."""
import getpass
from uuid import UUID
import bcrypt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from team_server.config import load, prepare_database
from team_server.store import initialize, Account

def main():
    cfg=load()
    prepare_database(cfg)
    account_id=input('Account UUID: ').strip()
    if str(UUID(account_id))!=account_id:raise SystemExit('Invalid UUID')
    username=input('Username: ').strip()
    password=getpass.getpass('Password: ')
    raw=password.encode('utf-8')
    if not username or not username.isprintable() or username.startswith('pending:') or not 12<=len(raw)<=72:
        raise SystemExit('Invalid username or password length')
    engine=create_engine(cfg.database_url,future=True);initialize(engine)
    with sessionmaker(bind=engine)() as db:
        if db.get(Account,account_id) or db.query(Account).filter_by(username=username).first():raise SystemExit('Account already exists')
        db.add(Account(id=account_id,username=username,password_hash=bcrypt.hashpw(raw,bcrypt.gensalt()).decode('ascii'),
                       state='active',revision=1,failed_count=0,locked_until=0));db.commit()
    print('Account provisioned')

if __name__=='__main__':main()
