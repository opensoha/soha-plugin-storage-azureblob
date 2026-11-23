#!/usr/bin/env python3
"""Scaffold and publish a Soha plugin from its root manifest."""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from plugin_assets import assets_path, publish_assets, release_stage
from plugin_release import (
    DRIVER_ID,
    EXECUTABLE_MODE,
    MANIFEST,
    PLATFORM,
    PROTOCOL,
    asset_record,
    json_bytes,
    manifest_components,
    matches,
    read_manifest,
    require,
    sign_release,
    validate_identity,
    validate_schema,
    verify_release,
)
from plugin_web_archive import validate_web_archive


ROOT = Path(__file__).resolve().parent
SDK_WEB_FILES = ("index.js", "page.js", "protocol.js", "host.js")
VARIANTS = {
    "manifest": (),
    "go": ("backend",),
    "web": ("web",),
    "go+web": ("backend", "web"),
}
DEFAULT_PLATFORMS = ("linux/amd64", "linux/arm64")
SEMVER = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"


def init_project(args):
    validate_identity(args.id, args.version)
    require(matches(SEMVER, args.version), "Use a Semantic Version such as 0.1.0")
    prerelease = args.version.split("+", 1)[0].partition("-")[2]
    require(all(not (part.isdigit() and len(part) > 1 and part.startswith("0"))
                for part in prerelease.split(".")), "Numeric pre-release identifiers cannot have leading zeros")
    require(args.name.strip() and args.publisher.strip(), "Specify a name and publisher")
    directories = VARIANTS[args.variant]
    if "backend" in directories:
        require(matches(DRIVER_ID, args.id),
                "For this Go starter, use a plugin ID that also qualifies as a driver ID")
    require((ROOT / "starter").is_dir(), "Run init from the template repository")

    _, manifest = read_manifest(ROOT)
    manifest.update(id=args.id, name=args.name, publisher=args.publisher, version=args.version)
    if directories:
        manifest.pop("extensionPoints")
        manifest["type"] = "resource-extension" if "backend" in directories else "ui-extension"
        manifest["description"] = "Describe the extensions this executable plugin provides."
    if "backend" in directories:
        manifest["configSchema"] = {
            "type": "object",
            "description": "The read-only sample denies connection tests that require read and write access.",
            "x-soha-test-connection-action": "storage.testConnection",
        }
    if not directories:
        manifest["extensionPoints"]["ai"]["skillPacks"][0]["label"] = args.name

    destination = args.directory.resolve()
    require(not destination.is_relative_to((ROOT / "starter").resolve()),
            "Create the author directory outside the starter source tree")
    destination.mkdir()
    for file in ("plugin.py", "plugin_assets.py", "plugin_release.py", "plugin_web_archive.py",
                 "requirements.txt", "README.md", "README-cn.md", ".gitignore", "LICENSE"):
        shutil.copyfile(ROOT / file, destination / file)
    shutil.copytree(ROOT / "schemas", destination / "schemas")
    for directory in directories:
        # Copy only starter source. Locally installed tools and dependencies stay outside the scaffold.
        source = ROOT / "starter" / directory
        shutil.copytree(source, destination / directory,
                        ignore=shutil.ignore_patterns("node_modules", "dist", ".DS_Store"))
    if not directories:
        for file in ("package.json", "package-lock.json", "validate-manifest.mjs"):
            shutil.copyfile(ROOT / file, destination / file)
        shutil.copytree(ROOT / ".github", destination / ".github")
    (destination / MANIFEST).write_bytes(json_bytes(manifest))
    print(f"Created {args.variant} plugin at {destination}")


def build_backend(manifest, platforms, stage):
    require(matches(DRIVER_ID, manifest["id"]), "Use a plugin ID that also qualifies as a driver ID")
    result = {}
    for platform in platforms:
        require(matches(PLATFORM, platform), f"Use GOOS/GOARCH for a platform, got {platform!r}")
        goos, goarch = platform.split("/")
        name = f"backend-{goos}-{goarch}" + (".exe" if goos == "windows" else "")
        binary = stage / name
        env = dict(os.environ, GOWORK="off", CGO_ENABLED="0", GOOS=goos, GOARCH=goarch)
        identity = f"-X main.pluginID={manifest['id']} -X main.version={manifest['version']}"
        subprocess.run(["go", "build", "-mod=readonly", "-trimpath", "-buildvcs=false",
                        "-ldflags", identity, "-o", binary, "."],
                       cwd=ROOT / "backend", env=env, check=True)
        result[platform] = asset_record(binary)
    return {"driverId": manifest["id"], "protocol": PROTOCOL, "platforms": result}


def copy_static_files(source, destination):
    require(source.is_dir() and not source.is_symlink(), "Expected a regular web/public directory")
    for path in sorted(source.iterdir()):
        require(not path.is_symlink(), f"Web assets cannot contain symlinks: {path.name}")
        require("\\" not in path.name, f"Web assets require POSIX paths: {path.name}")
        output = destination / path.name
        if path.is_dir():
            output.mkdir()
            copy_static_files(path, output)
        else:
            require(path.is_file(), f"Web assets must be regular files: {path.name}")
            shutil.copyfile(path, output)


def build_web(manifest, stage):
    web = ROOT / "web"
    public = stage / "site"
    public.mkdir()
    copy_static_files(web / "public", public)
    require((public / "index.html").is_file(), "The web bundle requires web/public/index.html")
    sdk = web / "node_modules" / "@opensoha" / "plugin-ui-sdk"
    sdk_dist = next((path for path in (sdk / "dist", sdk)
                     if all((path / file).is_file() for file in SDK_WEB_FILES)), None)
    require(sdk_dist is not None, "Install the published SDK in web/ before building")
    (public / "sdk").mkdir()
    for file in SDK_WEB_FILES:
        shutil.copyfile(sdk_dist / file, public / "sdk" / file)
    shutil.copyfile(sdk / "LICENSE", public / "sdk" / "LICENSE")

    identity = {"pluginId": manifest["id"], "version": manifest["version"]}
    identity_file = public / "identity.js"
    with identity_file.open("xb") as output:
        output.write(b"export const identity = " + json_bytes(identity))

    bundle = stage / "web.tar.gz"
    with bundle.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for file in sorted(public.rglob("*")):
                path = PurePosixPath(file.relative_to(public)).as_posix()
                if not file.is_file():
                    continue
                entry = tarfile.TarInfo(path)
                entry.mode = 0o644
                entry.size = file.stat().st_size
                with file.open("rb") as stream:
                    archive.addfile(entry, stream)
    validate_web_archive(bundle)
    return asset_record(bundle)


def build_project(args, assets):
    raw, manifest = read_manifest(ROOT)
    validate_schema(ROOT, "plugin-manifest", manifest)
    backend = (ROOT / "backend").is_dir()
    web = (ROOT / "web").is_dir()
    if not (backend or web):
        require(not args.platform, "--platform requires a backend component")
        require(not manifest_components(manifest), "No sources found for the declared components")
        with release_stage(ROOT, assets):
            pass
        print(f"Validated release identity for {manifest['id']}@{manifest['version']} (manifest-only)")
        return
    require(backend or not args.platform, "--platform requires a backend component")
    with release_stage(ROOT, assets) as (stage, owned):
        manifest["runtime"] = {"mode": EXECUTABLE_MODE}
        components = {}
        if backend:
            platforms = tuple(dict.fromkeys(args.platform or DEFAULT_PLATFORMS))
            components["backend"] = build_backend(manifest, platforms, stage)
        if web:
            components["web"] = build_web(manifest, stage)
        manifest["components"] = components
        manifest_components(manifest)
        validate_schema(ROOT, "plugin-manifest", manifest)
        final = json_bytes(manifest)
        names = [artifact.name for artifact in stage.iterdir() if artifact.is_file()]
        if final != raw:
            with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".soha-manifest-", delete=False) as output:
                manifest_temp = Path(output.name)
                try:
                    output.write(final)
                    output.close()
                    manifest_temp.chmod((ROOT / MANIFEST).stat().st_mode & 0o777)
                    publish_assets(ROOT, assets, stage, names, owned,
                                   lambda: manifest_temp.replace(ROOT / MANIFEST))
                finally:
                    manifest_temp.unlink(missing_ok=True)
        else:
            publish_assets(ROOT, assets, stage, names, owned)
    print(f"Built {manifest['id']}@{manifest['version']} release assets in {assets}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a new author source directory")
    init.add_argument("directory", type=Path)
    init.add_argument("--variant", choices=VARIANTS, required=True)
    init.add_argument("--id", required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--publisher", required=True)
    init.add_argument("--version", default="0.1.0")
    build = commands.add_parser("build", help="build components and update the root manifest")
    build.add_argument("--platform", action="append", help="repeatable GOOS/GOARCH (default: linux/amd64 and linux/arm64)")
    sign = commands.add_parser("sign", help="sign a built and committed release")
    sign.add_argument("--private-key", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify a release using an independently trusted public key")
    verify.add_argument("--trusted-key", type=Path, required=True)
    for command in (build, sign, verify):
        command.add_argument("--assets-dir", type=Path, default=ROOT / "release")
    for command in (sign, verify):
        command.add_argument("--repository", required=True)
        command.add_argument("--commit", required=True)

    args = parser.parse_args()
    try:
        if args.command == "init":
            init_project(args)
            return 0
        assets = assets_path(ROOT, args.assets_dir, writing=args.command != "verify")
        if args.command == "build":
            build_project(args, assets)
        elif args.command == "sign":
            fingerprint = sign_release(ROOT, assets, args.repository, args.commit, args.private_key)
            print(f"Signed {args.commit} with publisher SHA-256 {fingerprint}")
        else:
            fingerprint = verify_release(ROOT, assets, args.repository, args.commit, args.trusted_key)
            print(f"Verified {args.commit} with publisher SHA-256 {fingerprint}")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
