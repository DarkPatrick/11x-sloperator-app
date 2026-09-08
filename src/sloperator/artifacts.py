"""Content-based identities for delivered analysis archives."""

import hashlib
import json
import zipfile
from pathlib import Path


def artifact_fingerprint(path: Path) -> str:
    """Ignore ZIP timestamps, compression and ordering; compare named file contents."""
    if not zipfile.is_zipfile(path):
        with path.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    entries = []
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            if not entry.is_dir():
                with archive.open(entry) as source:
                    content_hash = hashlib.sha256()
                    while chunk := source.read(64 * 1024):
                        content_hash.update(chunk)
                    digest = content_hash.hexdigest()
                entries.append((entry.filename, digest))
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()
