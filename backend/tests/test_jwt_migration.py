"""Verify JWT key selection and required claims with synthetic keys only."""
from datetime import datetime, timedelta, timezone
import json

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.chatgpt_oauth import AUTH, Manager


@pytest.fixture
def signing_material():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid='test-key', alg='RS256', use='sig')
    return private, public


def test_chatgpt_identity_requires_matching_signing_key_and_claims(signing_material):
    private, public = signing_material
    manager = Manager.__new__(Manager)
    manager._json = lambda *args, **kwargs: {'keys': [public]}
    claims = {'iss': AUTH, 'aud': 'test-client', 'sub': 'synthetic-subject',
              'nonce': 'synthetic-nonce', 'exp': datetime.now(timezone.utc) + timedelta(minutes=5)}
    def encode(payload, key=private, kid='test-key'):
        return jwt.encode(payload, key, algorithm='RS256', headers={'kid': kid})
    assert manager._identity(None, encode(claims), 'test-client', 'synthetic-nonce')['sub'] == claims['sub']
    with pytest.raises(ValueError, match='oauth_signing_key'):
        manager._identity(None, encode(claims, kid='unknown'), 'test-client')
    with pytest.raises(jwt.MissingRequiredClaimError):
        manager._identity(None, encode({k: v for k, v in claims.items() if k != 'exp'}), 'test-client')
    with pytest.raises(jwt.InvalidAudienceError):
        manager._identity(None, encode(claims), 'different-client')
    with pytest.raises(ValueError, match='oauth_nonce'):
        manager._identity(None, encode(claims), 'test-client', 'wrong-nonce')
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        manager._identity(None, encode(claims, key=other), 'test-client')
    manager._json = lambda *args, **kwargs: {'keys': [public, public]}
    with pytest.raises(ValueError, match='oauth_signing_key'):
        manager._identity(None, encode(claims), 'test-client')
