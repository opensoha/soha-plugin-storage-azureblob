"""Check the format and bounds of a Soha static Web archive without extracting it."""

import gzip
import tarfile
import zlib
from pathlib import PurePosixPath


MAX_ENTRIES = 4096
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_COMPRESSED_BYTES = MAX_EXPANDED_BYTES + 1024 * 1024
REGULAR_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)
BLOCK = tarfile.BLOCKSIZE
END_BLOCK = bytes(BLOCK)


def validate_web_archive(path):
    try:
        if path.stat().st_size > MAX_COMPRESSED_BYTES:
            raise ValueError("compressed archive exceeds 257 MiB")
        with gzip.open(path, "rb") as stream:
            scan_archive(stream)
    except (OSError, EOFError, tarfile.HeaderError, ValueError, zlib.error) as error:
        raise ValueError(f"Invalid Web archive {path.name}: {error}") from error


def scan_archive(stream):
    expanded = 0
    entries = {}
    parents = set()

    def read(size):
        nonlocal expanded
        data = stream.read(size)
        expanded += len(data)
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("expanded archive exceeds 256 MiB")
        return data

    for count in range(MAX_ENTRIES + 1):
        block = read(BLOCK)
        if block == END_BLOCK:
            if read(BLOCK) != END_BLOCK:
                raise ValueError("missing end-of-archive marker")
            while padding := read(64 * 1024):
                if any(padding):
                    raise ValueError("nonzero bytes after end-of-archive marker")
            break
        if count == MAX_ENTRIES:
            raise ValueError("archive exceeds 4096 entries")

        entry = tarfile.TarInfo.frombuf(block, "utf-8", "strict")
        directory = entry.type == tarfile.DIRTYPE
        if not directory and entry.type not in REGULAR_TYPES:
            raise ValueError("only regular files and directories are allowed")
        if entry.size < 0 or (directory and entry.size != 0):
            raise ValueError("invalid entry length")

        canonical = canonical_path(header_path(block), directory)
        if canonical in entries:
            raise ValueError("duplicate canonical path")
        if not directory and canonical in parents:
            raise ValueError("file conflicts with a directory")
        if canonical != ".":
            for parent in PurePosixPath(canonical).parents:
                key = parent.as_posix()
                if entries.get(key) is False:
                    raise ValueError("file conflicts with a directory")
                parents.add(key)
        entries[canonical] = directory

        padded_size = (entry.size + BLOCK - 1) // BLOCK * BLOCK
        if padded_size > MAX_EXPANDED_BYTES - expanded:
            raise ValueError("expanded archive exceeds 256 MiB")
        while padded_size:
            size = min(padded_size, 64 * 1024)
            if len(read(size)) != size:
                raise ValueError("truncated entry")
            padded_size -= size

    if entries.get("index.html") is not False:
        raise ValueError("archive must contain a regular index.html at its root")


def header_path(block):
    # Preserve trailing slashes for exact path validation. Only USTAR uses the
    # prefix field. GNU stores time and sparse metadata at the same offsets.
    name = block[:100].split(b"\0", 1)[0].decode("utf-8")
    magic = block[257:265]
    if magic == tarfile.POSIX_MAGIC:
        prefix = block[345:500].split(b"\0", 1)[0].decode("utf-8")
        return f"{prefix}/{name}" if prefix else name
    if magic in (tarfile.GNU_MAGIC, bytes(8)):
        return name
    raise ValueError("unsupported tar header format")


def canonical_path(name, directory):
    if directory and name in (".", "./"):
        return "."
    if name.startswith("./"):
        name = name[2:]
    if directory and name.endswith("/"):
        name = name[:-1]
    if "\\" in name or any(segment in ("", ".", "..") for segment in name.split("/")):
        raise ValueError("invalid relative entry path")
    return name
