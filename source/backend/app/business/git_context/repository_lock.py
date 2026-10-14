from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def repository_lock(*, lock_root: Path, repository_path: Path) -> Iterator[None]:
    resolved_repository_path = repository_path.resolve()
    lock_key = hashlib.sha256(str(resolved_repository_path).encode("utf-8")).hexdigest()[:16]
    lock_path = lock_root.resolve() / f"repository__{lock_key}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
