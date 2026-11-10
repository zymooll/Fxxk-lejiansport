"""Offline LeGym 3.10.3 login protocol, verified against two real captures.

No network requests, credential storage, or Android runtime dependencies.
"""
import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

LOGIN_URL = 'https://cpes.legym.cn/authorization/user/v2/manage/login'
BASE_URL = 'https://cpes.legym.cn/'
_DYNAMIC_SUFFIX = '402881ea7c39c5d5017c39d143a8062b'
_SIGNATURE_SUFFIX = 'itauVfnexHiRigZ6'


def derive_key(timestamp):
    """NativeLib.getDyKey -> getDynamicKey -> first 16 UTF-8 bytes."""
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError('timestamp must be an integer number of milliseconds')
    text = str(timestamp)
    if len(text) != 13 or not text.isascii() or not text.isdigit():
        raise ValueError('expected the observed 13-digit Unix millisecond timestamp')
    number = abs(int(text[2:5]) - int(text[4:8])) << int(text[-1])
    return (str(number) + _DYNAMIC_SUFFIX).encode('utf-8')[:16]


def login_signature(phone, password, entrance='1'):
    return hashlib.sha1((phone + password + entrance + _SIGNATURE_SUFFIX).encode('utf-8')).hexdigest()


def gson_json(value):
    """Match compact Gson output, including its default HTML-safe escaping."""
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    for source, replacement in (('&', r'\u0026'), ('<', r'\u003c'), ('>', r'\u003e'),
                                ('=', r'\u003d'), ("'", r'\u0027'),
                                ('\u2028', r'\u2028'), ('\u2029', r'\u2029')):
        text = text.replace(source, replacement)
    return text


def encrypt_bytes(plaintext, timestamp):
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(derive_key(timestamp)), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    # Android Base64.DEFAULT: line wrap at 76 characters and a final LF.
    return base64.encodebytes(ciphertext).decode('ascii')


def decrypt_bytes(envelope):
    if not isinstance(envelope, dict) or not isinstance(envelope.get('pyd'), str):
        raise ValueError('missing pyd envelope')
    encoded = ''.join(envelope['pyd'].split())
    ciphertext = base64.b64decode(encoded, validate=True)
    decryptor = Cipher(algorithms.AES(derive_key(envelope['t'])), modes.ECB()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def build_login_payload(phone, password):
    if not isinstance(phone, str) or not phone or not isinstance(password, str) or not password:
        raise ValueError('phone and password must be nonempty strings')
    return {'entrance': '1', 'password': password, 'phone': phone,
            'signDigital': login_signature(phone, password), 'userName': phone,
            'authType': 'Bearer', 'baseUrl': BASE_URL, 'page': '', 'type': ''}


def build_login_request(phone, password, timestamp=None):
    timestamp = time.time_ns() // 1_000_000 if timestamp is None else timestamp
    payload = gson_json(build_login_payload(phone, password)).encode('utf-8')
    return {'t': timestamp, 'pyd': encrypt_bytes(payload, timestamp)}


def extract_token_pair(response):
    """Decrypt the response using its own t; discard all personal profile fields."""
    if not isinstance(response, dict) or type(response.get('code')) is not int or response['code'] != 0:
        raise ValueError('login did not return business code 0')
    decoded = json.loads(decrypt_bytes(response['data']))
    if not all(isinstance(decoded.get(k), str) and decoded[k] for k in ('accessToken', 'refreshToken')):
        raise ValueError('login response has no token pair')
    return {k: decoded[k] for k in ('accessToken', 'refreshToken', 'tokenType', 'accessTokenExpires')
            if k in decoded}
