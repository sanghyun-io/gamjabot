"""Explicit operator-provided public trust profiles; never learned remotely."""
from dataclasses import dataclass
from hashlib import sha256
import re
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from app.services.team_authority_adapter import _url

@dataclass(frozen=True)
class TeamTrustProfile:
    profile_id: str
    display_name: str
    mode: str
    base_url: str
    issuer: str
    audience: str
    public_key: str
    key_fingerprint: str

    def public(self):
        return {'profile_id':self.profile_id,'display_name':self.display_name,'base_url':self.base_url,
                'issuer':self.issuer,'key_fingerprint':self.key_fingerprint,'ready':True,'status':'configured'}

def build_registry(entries):
    if not isinstance(entries,(tuple,list)) or len(entries)>16:raise ValueError('Invalid team profiles')
    registry={}
    for entry in entries:
        if not isinstance(entry,dict) or set(entry)!={'profile_id','display_name','mode','base_url','issuer','audience','public_key'}:
            raise ValueError('Invalid team profile')
        profile_id=entry['profile_id'];name=entry['display_name'];audience=entry['audience']
        if (not isinstance(profile_id,str) or re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',profile_id) is None
                or profile_id in registry or not isinstance(name,str) or not 1<=len(name)<=100 or not name.isprintable()
                or not isinstance(audience,str) or not 1<=len(audience)<=200):raise ValueError('Invalid team profile')
        try:
            base=_url(entry['base_url'],entry['mode']);issuer=_url(entry['issuer'],entry['mode'])
            public=entry['public_key'];key=serialization.load_pem_public_key(public.encode('ascii'))
            if base!=issuer or not isinstance(key,rsa.RSAPublicKey) or key.key_size<2048:raise ValueError()
            der=key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        except Exception:raise ValueError('Invalid team trust') from None
        registry[profile_id]=TeamTrustProfile(profile_id,name,entry['mode'],base,issuer,audience,public,sha256(der).hexdigest())
    return registry
