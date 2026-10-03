"""Create secret files with access restricted before any bytes are written."""
from __future__ import annotations

import os
import stat
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


def restrict(path: Path, directory=False) -> None:
    _reject_stream(path)
    if path.is_symlink():
        raise PermissionError("secret storage cannot be a symbolic link")
    if os.name == "nt":
        import win32security
        acl = _attributes(directory).SECURITY_DESCRIPTOR.GetSecurityDescriptorDacl()
        owner = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                                  win32security.OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner()
        if owner != _attributes().SECURITY_DESCRIPTOR.GetSecurityDescriptorOwner():
            raise PermissionError("secret storage must belong to the gateway user")
        win32security.SetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        actual = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                                  win32security.DACL_SECURITY_INFORMATION)
        if not actual.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED:
            raise PermissionError("secret storage permissions could not be restricted")
        got = actual.GetSecurityDescriptorDacl()
        if got is None or got.GetAceCount() != 1 or got.GetAce(0) != acl.GetAce(0):
            raise PermissionError("secret storage permissions could not be restricted")
    else:
        info = path.stat()
        if info.st_uid != os.geteuid():
            raise PermissionError("secret storage must belong to the gateway user")
        mode = 0o700 if directory else 0o600
        path.chmod(mode)
        if stat.S_IMODE(path.stat().st_mode) != mode:
            raise PermissionError("secret storage permissions could not be restricted")


def private_directory(path: Path) -> None:
    _reject_stream(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import win32file
            win32file.CreateDirectory(str(path), _attributes(True))
        else:
            path.mkdir(mode=0o700)
    restrict(path, directory=True)


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
