"""Create secret files with access restricted before any bytes are written."""
from __future__ import annotations

import os
from pathlib import Path


def _reject_stream(path):
    if os.name == "nt" and ":" in str(path)[len(path.drive):]:
        raise ValueError("alternate data streams cannot store gateway secrets")


def _attributes(directory=False):
    import win32security
    import win32api
    import win32con
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    flags = "OICI" if directory else ""
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{win32security.ConvertSidToStringSid(sid)}D:P(A;{flags};FA;;;{win32security.ConvertSidToStringSid(sid)})", 1)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def private_directory(path: Path) -> None:
    if not path.exists():
        _reject_stream(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import win32file
            win32file.CreateDirectory(str(path), _attributes(True))
        else:
            path.mkdir(mode=0o700)


def create_private(path: Path):
    """Exclusive binary writer. Existing files, permission failures and races fail closed."""
    _reject_stream(path)
    if os.name == "nt":
        import msvcrt
        import win32file
        import win32con
        handle = win32file.CreateFile(str(path), win32con.GENERIC_WRITE, 0, _attributes(),
                                      win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_NORMAL, None)
        try:
            fd = msvcrt.open_osfhandle(handle.Detach(), os.O_WRONLY | os.O_BINARY)
        except BaseException:
            handle.Close()
            raise
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")
