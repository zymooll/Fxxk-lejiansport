"""Windows-only encrypted recovery points for local credential rotation."""
import ctypes
import json
import os
import threading
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

_locks = {}
_locks_guard = threading.Lock()


def credential_lock(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data, decrypt=False):
    if os.name != 'nt':
        raise OSError('Credential checkpoints require Windows DPAPI')
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    if decrypt:
        method = crypt.CryptUnprotectData
        method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        method = crypt.CryptProtectData
        method.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    ok = method(ctypes.byref(source), None if decrypt else 'Run MVP credentials', None, None, None,
                1, ctypes.byref(result))
    if not ok:
        raise OSError('Windows credential encryption failed')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def checkpoint(credential_file, data, phase):
    encrypted = dpapi(data)
    if dpapi(encrypted, decrypt=True) != data:
        raise OSError('Credential checkpoint verification failed')
    directory = Path(credential_file).parent/'credential-checkpoints'
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    path = directory/(stamp+'-'+uuid.uuid4().hex+'-'+phase+'.dpapi')
    with path.open('xb') as output:
        output.write(encrypted)
        output.flush()
        os.fsync(output.fileno())
    return path


def record_refresh_event(credential_file, outcome, code=None, http_status=None):
    # Never log the request URL, response text, headers, or credential values.
    entry = {'at': datetime.now(timezone.utc).isoformat(), 'outcome': outcome}
    if code is not None: entry['code'] = code
    if http_status is not None: entry['httpStatus'] = http_status
    try:
        with (Path(credential_file).parent/'credential-refresh-events.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(entry)+'\n')
    except OSError:
        pass  # A failed diagnostic write must not mask the credential error.
