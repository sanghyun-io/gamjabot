"""Owner-submitted login metadata only: secrets are never accepted here."""
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator
from urllib.parse import urlsplit
import ipaddress
import re
import unicodedata


class Metadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    website: StrictStr = Field(min_length=1, max_length=2048)
    username: StrictStr = Field(min_length=1, max_length=512)
    nickname: StrictStr = Field(default='', max_length=200)
    hint: StrictStr = Field(default='', max_length=2000)

    @field_validator('website')
    @classmethod
    def origin(cls, value):
        if value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError('invalid origin')
        try:
            u = urlsplit(value)
            if u.scheme != 'https' or not u.hostname or u.username is not None or u.password is not None or u.path not in ('', '/') or u.query or u.fragment:
                raise ValueError('invalid origin')
            host = u.hostname.encode('idna').decode('ascii').lower()
            port = u.port
            try:
                address = ipaddress.ip_address(host)
                host = f'[{host}]' if address.version == 6 else host
            except ValueError:
                if not host or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in host.split('.')):
                    raise ValueError('invalid origin')
            if port == 0:
                raise ValueError('invalid origin')
            return 'https://' + host + (f':{port}' if port not in (None, 443) else '')
        except (UnicodeError, ValueError):
            raise ValueError('invalid origin') from None

    @field_validator('username', 'nickname', 'hint')
    @classmethod
    def safe_text(cls, value, info):
        limit = {'username': 512, 'nickname': 200, 'hint': 2000}[info.field_name]
        if len(value.encode('utf-8')) > limit or any(unicodedata.category(c) == 'Cc' for c in value):
            raise ValueError('invalid metadata')
        return value


class CreateLogin(Metadata):
    pass


class UpdateLogin(Metadata):
    expected_revision: StrictInt = Field(ge=1)


class DeleteLogin(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expected_revision: StrictInt = Field(ge=1)


class ImportReservation(CreateLogin):
    draft_id: StrictStr = Field(pattern=r'^[0-9a-f]{32}$')
    row: StrictInt = Field(ge=2, le=5001)
