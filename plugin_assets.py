"""Stage a release without overwriting files outside the template's ownership."""

import json
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


MARKER = ".soha-release-assets.json"
LOCK = ".soha-release-lock"


def assets_path(root, path, writing):
    if path.is_symlink():
        raise ValueError("Release asset directory cannot be a symlink")
    directory = path.resolve()
    if directory.exists() and not directory.is_dir():
        raise ValueError("Release assets need a directory")
    if writing:
        sources = (root / name for name in (".git", "backend", "web", "schemas", "starter"))
        if root.is_relative_to(directory) or any(directory.is_relative_to(source.resolve()) for source in sources):
            raise ValueError("Use a release asset directory outside the source directories")
    return directory


@contextmanager
def release_stage(root, directory):
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / MARKER
    if not marker.exists() and not marker.is_symlink() and any(directory.iterdir()):
        raise ValueError("Use a new, empty or template-managed release asset directory")

    lock = directory / LOCK
    lock.mkdir()
    try:
        owned = read_ownership(root, directory, marker, lock)
        with tempfile.TemporaryDirectory(dir=directory, prefix=".soha-stage-") as temporary:
            yield Path(temporary), owned
    finally:
        lock.rmdir()


def read_ownership(root, directory, marker, lock):
    if not marker.exists() and not marker.is_symlink():
        if any(path != lock for path in directory.iterdir()):
            raise ValueError("Use a new, empty or template-managed release asset directory")
        return set()
    if marker.is_symlink() or not marker.is_file():
        raise ValueError("Invalid release directory ownership marker")
    value = json.loads(marker.read_bytes())
    if not isinstance(value, dict) or value.get("authorRoot") != str(root):
        raise ValueError("Release asset directory belongs to a different author checkout")
    entries = value.get("files")
    if not isinstance(entries, list) or not all(
        isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name)
        for name in entries
    ):
        raise ValueError("Invalid release directory ownership marker")
    return set(entries)


def publish_assets(root, directory, stage, names, owned, finish=None):
    ownership = {"authorRoot": str(root), "files": sorted(owned.union(names))}
    (stage / MARKER).write_text(json.dumps(ownership) + "\n", encoding="utf-8")
    outputs = [*names, MARKER]
    previous = stage / "previous"
    previous.mkdir()
    for name in outputs:
        target = directory / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or (name != MARKER and name not in owned):
                raise ValueError(f"Refusing to overwrite an unmanaged release file: {name}")
            shutil.copy2(target, previous / name)

    written = []
    try:
        for name in outputs:
            (stage / name).replace(directory / name)
            written.append(name)
        if finish is not None:
            finish()
    except BaseException:
        for name in reversed(written):
            backup = previous / name
            target = directory / name
            if backup.exists():
                backup.replace(target)
            else:
                target.unlink()
        raise
