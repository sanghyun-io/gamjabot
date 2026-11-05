"""Read the operator-installed public trust registry only at Engine startup."""
import json
import os
import stat
from app.services.team_trust_profiles import build_registry


def load_team_trust_file(app_data):
    if app_data is None:return ()
    flags=os.O_RDONLY | os.O_NOFOLLOW | getattr(os,'O_DIRECTORY',0)
    directory=os.open(app_data,flags)
    try:
        info=os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
            raise ValueError('Invalid team trust directory')
        try:fd=os.open('team-trust-profiles.json',os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=directory)
        except FileNotFoundError:return ()
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>262144:
                raise ValueError('Invalid team trust file')
            data=b''
            while len(data)<=262144:
                part=os.read(fd,min(65536,262145-len(data)))
                if not part:break
                data+=part
            if len(data)>262144:raise ValueError('Invalid team trust file')
        finally:os.close(fd)
    finally:os.close(directory)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate team trust key')
            result[key]=value
        return result
    entries=json.loads(data.decode('utf-8'),object_pairs_hook=unique)
    build_registry(entries)
    return tuple(entries)
