"""Keep a small secret on disk where only this Windows user can read it.

The Proxic Arena device token is a credential: it lets whoever holds it
download Arena's deck rules as the player who linked this computer. It is kept
in the portable data folder, which can be copied, synced or shared, so the bytes
written there are sealed with Windows' Data Protection API for the signed-in
Windows user (``CryptProtectData``). A copy of the folder carried to another
account or another computer cannot unseal it, and the player simply links again.

On a system without DPAPI (the source package run on Linux or macOS) the token
is written unsealed, in a file only its owner may read. ``sealed()`` says which
happened, so the connection dialog can say so rather than imply a protection
that is not there.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys

# Marks a sealed file, so an unsealed one written on another system is never
# handed to DPAPI, and a sealed one is never read as the token itself.
_SEALED = b"DPAPI1\n"
_PLAIN = b"PLAIN1\n"


def sealed():
    """True when secrets are sealed to the Windows user on this system."""
    return sys.platform == "win32"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(function, data):
    source, keep = _blob(data)
    result = _Blob()
    # CRYPTPROTECT_UI_FORBIDDEN: never prompt; fail instead.
    flags = 0x1
    if not function(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(result)):
        raise OSError("Windows could not protect or unprotect the stored secret.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)
        del keep


def write_secret(path, secret):
    """Write ``secret`` (text) to ``path``, sealed where the system allows."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = str(secret).encode("utf-8")
    if sealed():
        payload = _SEALED + _dpapi(ctypes.windll.crypt32.CryptProtectData, data)
    else:
        payload = _PLAIN + data
    temporary = target.with_name(target.name + ".tmp")
    # Owner-only from the start on systems that honour the mode.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
    os.replace(temporary, target)


def read_secret(path):
    """Return the secret stored at ``path``, or None when absent or unreadable.

    A file sealed for another Windows user or computer, a damaged file, or a
    file written in the other form reads as no secret at all: the caller treats
    the computer as not linked, which is true from here.
    """
    try:
        payload = Path(path).read_bytes()
    except OSError:
        return None
    try:
        if payload.startswith(_SEALED) and sealed():
            data = _dpapi(ctypes.windll.crypt32.CryptUnprotectData, payload[len(_SEALED):])
        elif payload.startswith(_PLAIN) and not sealed():
            data = payload[len(_PLAIN):]
        else:
            return None
        return data.decode("utf-8") or None
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def delete_secret(path):
    """Remove a stored secret; nothing to remove is not an error."""
    Path(path).unlink(missing_ok=True)
