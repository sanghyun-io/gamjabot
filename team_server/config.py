"""Explicit authority configuration; keys are supplied by the operator."""
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import os
import stat
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

@dataclass(frozen=True)
class Config:
    issuer: str
    audience: str
    database_url: str
    private_key: str
    public_key: str
    mode: str


def load():
    mode=os.environ.get('TEAM_AUTH_MODE','')
    issuer=os.environ.get('TEAM_AUTH_ISSUER','').rstrip('/')
    audience=os.environ.get('TEAM_AUTH_AUDIENCE','')
    database=os.environ.get('TEAM_AUTH_DATABASE_URL','')
    private_path=os.environ.get('TEAM_AUTH_PRIVATE_KEY_FILE','')
    public_path=os.environ.get('TEAM_AUTH_PUBLIC_KEY_FILE','')
    parsed=urlsplit(issuer)
    if (mode not in ('development','production') or not issuer or not audience or len(audience)>200
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/') or not parsed.hostname
            or not database.startswith('sqlite:///') or database=='sqlite:///:memory:' or '?' in database or '#' in database
            or not private_path or not public_path):
        raise RuntimeError('Invalid team authority configuration')
    if mode=='production' and parsed.scheme!='https': raise RuntimeError('Production issuer must use HTTPS')
    if mode=='development' and (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','::1')):
        raise RuntimeError('Development issuer must be loopback HTTP')
    if not Path(private_path).is_absolute() or not Path(public_path).is_absolute() or not database.startswith('sqlite:////'):
        raise RuntimeError('Absolute paths required')
    def key_file(path, private):
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size>32768 or info.st_size<1:
                raise RuntimeError('Invalid key file')
            if private and (info.st_uid!=os.geteuid() or info.st_mode & 0o077 or info.st_nlink!=1):
                raise RuntimeError('Private key file must be owner-only')
            data=b''
            while len(data)<=32768:
                part=os.read(fd,32769-len(data))
                if not part:break
                data+=part
            if len(data)>32768:raise RuntimeError('Invalid key file')
            return data.decode('ascii')
        finally:os.close(fd)
    private=key_file(private_path,True);public=key_file(public_path,False)
    try:
        pub=serialization.load_pem_public_key(public.encode('ascii'))
        prv=serialization.load_pem_private_key(private.encode('ascii'),password=None)
    except (ValueError,TypeError):raise RuntimeError('Invalid authority keys') from None
    if not isinstance(pub,rsa.RSAPublicKey) or not isinstance(prv,rsa.RSAPrivateKey) or pub.key_size<2048:
        raise RuntimeError('RSA key of at least 2048 bits required')
    # Fail closed for a wrong key pair without generating keys or persisting claims.
    probe=jwt.encode({'iss':issuer,'aud':audience,'sub':'configuration-check'},private,algorithm='RS256')
    jwt.decode(probe,public,algorithms=['RS256'],issuer=issuer,audience=audience)
    return Config(issuer,audience,database,private,public,mode)


def prepare_database(config):
    """Open only a private, dedicated absolute SQLite file; never chmod others."""
    path=Path(config.database_url[len('sqlite:///'):])
    if not path.is_absolute() or not path.name:raise RuntimeError('Invalid database path')
    parent=os.stat(path.parent,follow_symlinks=False)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode & 0o077:
        raise RuntimeError('Database directory must be owner-only')
    flags=os.O_RDWR|os.O_NOFOLLOW
    try: fd=os.open(path,flags|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError: fd=os.open(path,flags)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077 or info.st_nlink!=1:
            raise RuntimeError('Database must be owner-only')
    finally:os.close(fd)
