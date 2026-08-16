#!/usr/bin/env python3
"""Discover, verify, and add the latest stable Firebase Unity SDK release."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from validate_catalog import (
    DEFAULT_CATALOG,
    DEFAULT_CONFIG,
    DEFAULT_INSTALLER_CATALOG,
    DEFAULT_REGISTRY,
    DEFAULT_SCHEMA,
    ValidationError,
    load_json,
    make_installer_manifest,
    make_registry_documents,
    validate_catalog_data,
    validate_config,
    validate_https_url,
)


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_JSON_BYTES = 1024 * 1024
USER_AGENT = "unityarchitect-sdk-catalog/2"


class SyncError(RuntimeError):
    """Raised when a release cannot be safely synchronized."""


def version_tuple(version: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(part) for part in version.split("."))
    except ValueError as exc:
        raise SyncError(f"Invalid stable version: {version!r}") from exc
    if len(parts) != 3 or any(part < 0 for part in parts):
        raise SyncError(f"Invalid stable version: {version!r}")
    return parts  # type: ignore[return-value]


def _request(url: str, allowed_hosts: set[str], accept: str) -> BinaryIO:
    validate_https_url(url, allowed_hosts, "request URL")
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = Request(url, headers=headers)
    try:
        response = urlopen(request, timeout=60)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise SyncError(f"Request failed for {url}: {exc}") from exc
    try:
        validate_https_url(response.geturl(), allowed_hosts, "redirected URL")
    except Exception:
        response.close()
        raise
    return response


def discover_latest(config: dict[str, Any]) -> tuple[str, str]:
    allowed_hosts = set(config["allowedHosts"])
    with _request(
        config["releaseApiUrl"], allowed_hosts, "application/vnd.github+json"
    ) as response:
        try:
            payload = json.load(response)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncError("Release API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SyncError("Release API response must be an object")
    if payload.get("draft") or payload.get("prerelease"):
        raise SyncError("GitHub latest release must be stable and published")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise SyncError("Release tag must use the v<major>.<minor>.<patch> format")
    version = tag[1:]
    version_tuple(version)
    expected_source = config["releasePageTemplate"].format(version=version)
    if payload.get("html_url") != expected_source:
        raise SyncError("Release page does not match the configured official source")
    validate_https_url(expected_source, allowed_hosts, "release source")
    return version, expected_source


def download_archive(
    url: str, destination: Path, allowed_hosts: set[str]
) -> tuple[str, str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    total = 0
    with _request(url, allowed_hosts, "application/octet-stream") as response:
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise SyncError(f"Archive exceeds {MAX_ARCHIVE_BYTES} bytes: {url}")
                sha1.update(chunk)
                sha256.update(chunk)
                sha512.update(chunk)
                output.write(chunk)
    if total == 0:
        raise SyncError(f"Downloaded archive is empty: {url}")
    integrity = "sha512-" + base64.b64encode(sha512.digest()).decode("ascii")
    return sha1.hexdigest(), sha256.hexdigest(), integrity


def inspect_archive(path: Path, expected_name: str, version: str) -> dict[str, str]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            matches = [
                member
                for member in archive.getmembers()
                if PurePosixPath(member.name) == PurePosixPath("package/package.json")
            ]
            if len(matches) != 1 or not matches[0].isfile():
                raise SyncError(
                    f"{expected_name}: archive must contain one package/package.json"
                )
            member = matches[0]
            if member.size > MAX_PACKAGE_JSON_BYTES:
                raise SyncError(f"{expected_name}: package/package.json is too large")
            handle = archive.extractfile(member)
            if handle is None:
                raise SyncError(f"{expected_name}: cannot read package/package.json")
            raw = handle.read(MAX_PACKAGE_JSON_BYTES + 1)
    except (tarfile.TarError, OSError) as exc:
        raise SyncError(f"{expected_name}: invalid tgz archive: {exc}") from exc
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"{expected_name}: invalid package/package.json") from exc
    if not isinstance(metadata, dict):
        raise SyncError(f"{expected_name}: package metadata must be an object")
    if metadata.get("name") != expected_name:
        raise SyncError(
            f"Archive name mismatch: expected {expected_name}, got {metadata.get('name')!r}"
        )
    if metadata.get("version") != version:
        raise SyncError(
            f"{expected_name}: expected version {version}, got {metadata.get('version')!r}"
        )
    dependencies = metadata.get("dependencies", {})
    if not isinstance(dependencies, dict) or any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in dependencies.items()
    ):
        raise SyncError(f"{expected_name}: dependencies must be a string map")
    return dict(dependencies)


def _build_package(
    config: dict[str, Any],
    package_config: dict[str, Any],
    version: str,
    temp_root: Path,
) -> dict[str, Any]:
    name = package_config["name"]
    url = config["archiveUrlTemplate"].format(packageName=name, version=version)
    allowed_hosts = set(config["allowedHosts"])
    validate_https_url(url, allowed_hosts, f"archive URL for {name}")
    archive_path = temp_root / f"{name}-{version}.tgz"
    sha1, sha256, integrity = download_archive(url, archive_path, allowed_hosts)
    dependencies = inspect_archive(archive_path, name, version)
    return {
        **package_config,
        "version": version,
        "archiveUrl": url,
        "sha1": sha1,
        "sha256": sha256,
        "integrity": integrity,
        "dependencies": dependencies,
    }


def build_release(
    config: dict[str, Any], version: str, source: str
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="firebase-catalog-") as temp_dir:
        temp_root = Path(temp_dir)
        tracked = config["trackedPackages"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            tracked_entries = list(
                executor.map(
                    lambda package: _build_package(config, package, version, temp_root),
                    tracked,
                )
            )

        support_entries: list[dict[str, Any]] = []
        for support in config["supportPackages"]:
            support_name = support["name"]
            required_versions = {
                entry["dependencies"][support_name]
                for entry in tracked_entries
                if support_name in entry["dependencies"]
            }
            if len(required_versions) != 1:
                raise SyncError(
                    f"Expected exactly one required version for {support_name}, "
                    f"got {sorted(required_versions)}"
                )
            support_version = next(iter(required_versions))
            version_tuple(support_version)
            support_entries.append(
                _build_package(config, support, support_version, temp_root)
            )

    entries = [*support_entries, *tracked_entries]
    names = {entry["name"] for entry in entries}
    for entry in entries:
        unknown = set(entry["dependencies"]) - names
        if unknown:
            raise SyncError(
                f"{entry['name']}: untracked dependencies {sorted(unknown)}"
            )
        for dependency, dependency_version in entry["dependencies"].items():
            target = next(item for item in entries if item["name"] == dependency)
            if target["version"] != dependency_version:
                raise SyncError(
                    f"{entry['name']}: dependency {dependency} version mismatch"
                )
    return {"source": source, "packages": entries}


def apply_release(
    catalog: dict[str, Any], version: str, release: dict[str, Any]
) -> bool:
    before = json.dumps(catalog, sort_keys=True)
    catalog["releases"][version] = release
    catalog["releases"] = dict(
        sorted(catalog["releases"].items(), key=lambda item: version_tuple(item[0]))
    )
    catalog["latestVersion"] = version
    return before != json.dumps(catalog, sort_keys=True)


def write_json_if_changed(path: Path, value: dict[str, Any]) -> bool:
    rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    try:
        if path.read_text(encoding="utf-8") == rendered:
            return False
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def write_registry_documents(
    directory: Path, documents: dict[str, dict[str, Any]]
) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, document in documents.items():
        changed = write_json_if_changed(directory / name, document) or changed
    nojekyll = directory / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.touch()
        changed = True
    return changed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument(
        "--installer-catalog", type=Path, default=DEFAULT_INSTALLER_CATALOG
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument(
        "--version",
        help="Verify a specific stable version instead of querying GitHub latest",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download and replace the selected release even when it is current",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_json(args.config)
        catalog = load_json(args.catalog)
        schema = load_json(args.schema)
        validate_config(config)
        if not args.refresh:
            validate_catalog_data(catalog, config, schema)
        if args.version:
            version_tuple(args.version)
            version = args.version
            source = config["releasePageTemplate"].format(version=version)
        else:
            version, source = discover_latest(config)

        current = catalog["latestVersion"]
        if version_tuple(version) < version_tuple(current):
            raise SyncError(
                f"Discovered version {version} is older than catalog latest {current}"
            )
        catalog_changed = False
        if args.refresh or version != current or version not in catalog["releases"]:
            release = build_release(config, version, source)
            catalog_changed = apply_release(catalog, version, release)
            validate_catalog_data(catalog, config, schema)
            catalog_changed = write_json_if_changed(args.catalog, catalog) or catalog_changed

        installer_changed = write_json_if_changed(
            args.installer_catalog, make_installer_manifest(catalog)
        )
        registry_changed = write_registry_documents(
            args.registry, make_registry_documents(catalog, config)
        )
        if catalog_changed or installer_changed or registry_changed:
            print(
                f"Updated Firebase {version}; recommendedVersion remains "
                f"{catalog['recommendedVersion']}"
            )
        else:
            print(f"Catalog is already current at Firebase {version}")
        return 0
    except (ValidationError, SyncError, OSError) as exc:
        print(f"Firebase sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
