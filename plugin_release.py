"""Read, sign and verify the Soha executable plugin release v1 envelope."""

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from functools import cache
from pathlib import Path

from plugin_assets import publish_assets, release_stage
from plugin_web_archive import MAX_COMPRESSED_BYTES, validate_web_archive


MANIFEST = "plugin.manifest.json"
ATTESTATION = "soha-plugin-attestation.json"
SIGNATURE = "soha-plugin-attestation.sig"
PUBLISHER = "soha-plugin-publisher.pem"
EXECUTABLE_MODE = "executable-package-v1"
PROTOCOL = "object-storage/v1"
PLUGIN_ID = r"[a-z0-9][a-z0-9.\-]*\.[a-z0-9][a-z0-9._\-]*"
DRIVER_ID = r"[a-z][a-z0-9.\-]{0,126}"
VERSION = r"[A-Za-z0-9][A-Za-z0-9.+_\-]{0,127}"
PLATFORM = r"[a-z0-9]+/[a-z0-9]+"
SHA256 = r"[a-f0-9]{64}"
ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")
SCHEMAS = ("companion-pack", "plugin-manifest", "plugin-release-attestation")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def strict_json(data):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"Invalid JSON value: {value}")

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs,
                      parse_constant=invalid_constant)


def validate_identity(plugin_id, version):
    require(matches(PLUGIN_ID, plugin_id) and len(plugin_id) <= 255, "Invalid plugin ID")
    require(matches(VERSION, version), "Invalid release version")


@cache
def schema_validators(root):
    try:
        from jsonschema import Draft202012Validator, FormatChecker
        from referencing import Registry, Resource
    except ImportError as error:
        raise ValueError("Install requirements.txt in the author Python environment") from error

    schemas = {name: strict_json((root / "schemas" / f"{name}.schema.json").read_bytes())
               for name in SCHEMAS}
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    checker = FormatChecker()
    require("uri" in checker.checkers, "Install requirements.txt to validate URI formats")
    return {name: Draft202012Validator(schema, registry=registry, format_checker=checker)
            for name, schema in schemas.items()}


def validate_schema(root, name, value):
    error = next(schema_validators(root)[name].iter_errors(value), None)
    if error is not None:
        raise ValueError(f"Invalid {name} at {error.json_path} ({error.validator})")


def read_manifest(root):
    path = root / MANIFEST
    require(path.is_file() and not path.is_symlink(), f"Expected a regular {MANIFEST}")
    raw = path.read_bytes()
    manifest = strict_json(raw)
    require(isinstance(manifest, dict), "Expected a JSON object for the root manifest")
    validate_identity(manifest.get("id"), manifest.get("version"))
    return raw, manifest


def declared_artifacts(components):
    require(isinstance(components, dict) and set(components) <= {"backend", "web"},
            "Invalid release components")
    artifacts = []
    if "backend" in components:
        backend = components["backend"]
        require(isinstance(backend, dict) and set(backend) == {"driverId", "protocol", "platforms"},
                "Invalid backend component")
        require(matches(DRIVER_ID, backend["driverId"]), "Invalid driver ID")
        require(backend["protocol"] == PROTOCOL, "Unsupported storage driver protocol")
        platforms = backend["platforms"]
        require(isinstance(platforms, dict) and platforms, "Backend needs at least one platform")
        for platform, artifact in platforms.items():
            require(matches(PLATFORM, platform), f"Invalid backend platform: {platform}")
            artifacts.append(artifact)
    if "web" in components:
        artifacts.append(components["web"])

    seen = {ATTESTATION, SIGNATURE, PUBLISHER, MANIFEST}
    for artifact in artifacts:
        require(isinstance(artifact, dict) and set(artifact) == {"name", "byteLength", "sha256"},
                "Invalid release artifact record")
        name = artifact["name"]
        require(matches(r"[a-zA-Z0-9][a-zA-Z0-9._\-]{0,127}", name), "Invalid release asset name")
        require(name.casefold() not in seen, f"Duplicate or reserved release asset: {name}")
        seen.add(name.casefold())
        size = artifact["byteLength"]
        require(type(size) is int and 1 <= size <= 9007199254740991, f"Invalid byte length: {name}")
        require(matches(SHA256, artifact["sha256"]), f"Invalid SHA-256: {name}")
    return artifacts


def manifest_components(manifest):
    runtime = manifest.get("runtime", {"mode": "manifest-only"})
    require(isinstance(runtime, dict), "Invalid manifest runtime")
    mode = runtime.get("mode")
    if mode == EXECUTABLE_MODE:
        components = manifest.get("components")
        require(declared_artifacts(components), "Executable manifest needs packaged components")
        return components
    require(mode == "manifest-only", "Unsupported manifest runtime for this release tool")
    require("components" not in manifest, "Manifest-only packages cannot declare components")
    return {}


def asset_record(path, expected_size=None, maximum_size=None):
    require(path.is_file() and not path.is_symlink(), f"Missing or non-regular asset: {path.name}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        file_size = os.fstat(source.fileno()).st_size
        require(expected_size is None or file_size == expected_size,
                f"Size or SHA-256 mismatch: {path.name}")
        require(maximum_size is None or file_size <= maximum_size,
                f"Release asset exceeds the compressed size limit: {path.name}")
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            require(size <= file_size, f"Release asset changed during hashing: {path.name}")
            digest.update(block)
    return {"name": path.name, "byteLength": size, "sha256": digest.hexdigest()}


def verify_artifacts(directory, components):
    web = components.get("web")
    for artifact in declared_artifacts(components):
        bound = MAX_COMPRESSED_BYTES if artifact is web else None
        actual = asset_record(directory / artifact["name"], expected_size=artifact["byteLength"],
                              maximum_size=bound)
        require(actual == artifact, f"Size or SHA-256 mismatch: {artifact['name']}")
    if web is not None:
        validate_web_archive(directory / web["name"])


def git(root, *arguments):
    return subprocess.run(["git", "-C", root, *arguments],
                          check=True, stdout=subprocess.PIPE).stdout


def expected_attestation(root, repository, commit):
    repository_pattern = r"https://github\.com/[a-z0-9\-]+/[a-z0-9_.\-]+"
    require(matches(repository_pattern, repository), "Use https://github.com/owner/repo in lowercase")
    slug = repository.rsplit("/", 1)[-1]
    require(slug not in {".", ".."} and not slug.endswith(".git"), "Use a GitHub repository URL without .git")
    require(matches(r"[a-f0-9]{40}", commit), "Use the full lowercase 40-character Git commit SHA")
    checkout = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel").rstrip(b"\n")))
    require(checkout.resolve() == root, "The author directory must be the root of its own Git checkout")
    require(git(root, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip() == commit,
            "The supplied SHA must identify a commit")
    raw, manifest = read_manifest(root)
    require(git(root, "show", f"{commit}:{MANIFEST}") == raw, "Root manifest differs from the pinned commit")
    validate_schema(root, "plugin-manifest", manifest)
    return {
        "schemaVersion": "1",
        "pluginId": manifest["id"],
        "version": manifest["version"],
        "repository": repository,
        "commitSha": commit,
        "rawManifestSha256": hashlib.sha256(raw).hexdigest(),
        "components": manifest_components(manifest),
    }


def openssl(*arguments):
    return subprocess.run(["openssl", *arguments], check=True, stdout=subprocess.PIPE).stdout


def public_key_bytes(pem):
    match = re.fullmatch(rb"-----BEGIN PUBLIC KEY-----\r?\n"
                         rb"([A-Za-z0-9+/=\r\n]+)\r?\n"
                         rb"-----END PUBLIC KEY-----\r?\n?", pem)
    require(match is not None, "Expected one PEM PUBLIC KEY block")
    der = base64.b64decode(b"".join(match[1].splitlines()), validate=True)
    require(der.startswith(ED25519_SPKI) and len(der) == len(ED25519_SPKI) + 32,
            "Publisher key must be Ed25519")
    return der[len(ED25519_SPKI):]


def check_signature(key, signature, data):
    require(len(signature) == 64, "Expected a 64-byte Ed25519 signature")
    with tempfile.TemporaryDirectory(prefix="soha-verify-") as temporary:
        # OpenSSL one-shot Ed25519 needs seekable input. Parse these same verified bytes.
        paths = [Path(temporary) / filename for filename in (PUBLISHER, SIGNATURE, ATTESTATION)]
        for path, content in zip(paths, (key, signature, data)):
            path.write_bytes(content)
        public, detached, payload = paths
        openssl("pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", public,
                "-sigfile", detached, "-in", payload)


def sign_release(root, assets, repository, commit, private_key):
    private_key = private_key.resolve(strict=True)
    require(not private_key.is_relative_to(root) and not private_key.is_relative_to(assets),
            "Keep the private key outside the author repository and release asset directory")
    with release_stage(root, assets) as (stage, owned):
        require(git(root, "rev-parse", "HEAD").decode().strip() == commit and
                not git(root, "status", "--porcelain", "--untracked-files=normal"),
                "Sign the current HEAD from a clean author checkout after committing the built manifest")
        attestation = expected_attestation(root, repository, commit)
        validate_schema(root, "plugin-release-attestation", attestation)
        components = attestation["components"]
        for directory in ("backend", "web"):
            if (root / directory).is_dir():
                require(directory in components, f"Build the {directory} component before signing")
        verify_artifacts(assets, components)
        key = openssl("pkey", "-in", private_key, "-pubout")
        fingerprint = hashlib.sha256(public_key_bytes(key)).hexdigest()
        payload = stage / ATTESTATION
        payload.write_bytes(json_bytes(attestation))
        signature = stage / SIGNATURE
        openssl("pkeyutl", "-sign", "-rawin", "-inkey", private_key, "-in", payload, "-out", signature)
        (stage / PUBLISHER).write_bytes(key)
        check_signature(key, signature.read_bytes(), payload.read_bytes())
        publish_assets(root, assets, stage, (ATTESTATION, SIGNATURE, PUBLISHER), owned)
    return fingerprint


def verify_release(root, assets, repository, commit, trusted_key):
    trusted_key = trusted_key.resolve(strict=True)
    require(not trusted_key.is_relative_to(assets), "Supply the trusted public key separately from downloaded assets")
    key = trusted_key.read_bytes()
    trusted_bytes = public_key_bytes(key)
    data = read_release_asset(assets / ATTESTATION)
    check_signature(key, read_release_asset(assets / SIGNATURE), data)
    attestation = strict_json(data)
    expected = expected_attestation(root, repository, commit)
    validate_schema(root, "plugin-release-attestation", attestation)
    declared_artifacts(attestation.get("components"))
    require(attestation == expected, "Attestation does not match the repository, commit, manifest or components")
    candidate = assets / PUBLISHER
    if candidate.exists() or candidate.is_symlink():
        require(public_key_bytes(read_release_asset(candidate)) == trusted_bytes,
                "Published candidate key differs from the trusted key")
    verify_artifacts(assets, expected["components"])
    return hashlib.sha256(trusted_bytes).hexdigest()


def read_release_asset(path):
    require(path.is_file() and not path.is_symlink(), f"Missing or non-regular asset: {path.name}")
    return path.read_bytes()
