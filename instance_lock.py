"""Per-folder single-instance lock, compatible with existing desktop.lock files."""
import errno
import os
from pathlib import Path


def acquire_instance_lock(path):
    """Return an owned file handle, or None if another process holds the lock.

    Never read or write the locked byte before ownership: on Windows even a
    read raises PermissionError while another instance owns that byte.
    Closing the handle (including process termination) releases the lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b', buffering=0)
    try:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            try:
                # Windows permits locking beyond EOF, so this also safely
                # handles two first launches racing on an empty lock file.
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                stream.close()
                return None
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'0')
        return stream
    except BaseException:
        stream.close()
        raise
