#!/usr/bin/env python3
"""Validate Firebase catalog structure and repository-specific invariants."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "firebase.json"
DEFAULT_CATALOG = ROOT / "catalog" / "firebase.json"
DEFAULT_SCHEMA = ROOT / "schemas" / "firebase-catalog.schema.json"
DEFAULT_REGISTRY = ROOT / "registry"
DEFAULT_INSTALLER_CATALOG = (
    ROOT
    / "Packages"
    / "com.unityarchitect.firebase-installer"
    / "Editor"
    / "Resources"
    / "firebase-catalog.json"
)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PACKAGE_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INTEGRITY_RE = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")
PACKAGE_METADATA_KEYS = {
    "name",
    "displayName",
    "category",
    "description",
    "selectable",
    "defaultSelected",
}


class ValidationError(ValueError):
    """Raised when catalog data is invalid."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"Expected a JSON object in {path}")
    return value


def _resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValidationError(f"Only local schema references are supported: {reference}")
    value: Any = root_schema
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or token not in value:
            raise ValidationError(f"Unresolvable schema reference: {reference}")
        value = value[token]
    if not isinstance(value, dict):
        raise ValidationError(f"Schema reference is not an object: {reference}")
    return value


def _matches_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected not in checks:
        raise ValidationError(f"Unsupported schema type: {expected}")
    return checks[expected](value)


def validate_against_schema(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any] | None = None,
    location: str = "$",
) -> None:
    """Validate the JSON Schema subset used by this repository."""
    root_schema = root_schema or schema
    if "$ref" in schema:
        validate_against_schema(
            value, _resolve_ref(root_schema, schema["$ref"]), root_schema, location
        )
        return

    expected_type = schema.get("type")
    if expected_type and not _matches_type(value, expected_type):
        raise ValidationError(
            f"{location}: expected {expected_type}, got {type(value).__name__}"
        )
    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{location}: expected constant {schema['const']!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValidationError(f"{location}: string is too short")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            raise ValidationError(f"{location}: value does not match {pattern!r}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValidationError(f"{location}: array has too few items")
        if schema.get("items"):
            for index, item in enumerate(value):
                validate_against_schema(
                    item, schema["items"], root_schema, f"{location}[{index}]"
                )

    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise ValidationError(f"{location}: object has too few properties")
        for required in schema.get("required", []):
            if required not in value:
                raise ValidationError(f"{location}: missing required property {required!r}")
        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        matched: set[str] = set()
        for key, child in value.items():
            if key in properties:
                validate_against_schema(
                    child, properties[key], root_schema, f"{location}.{key}"
                )
                matched.add(key)
            for pattern, child_schema in pattern_properties.items():
                if re.search(pattern, key):
                    validate_against_schema(
                        child, child_schema, root_schema, f"{location}.{key}"
                    )
                    matched.add(key)
        additional = schema.get("additionalProperties", True)
        for key in value.keys() - matched:
            if additional is False:
                raise ValidationError(f"{location}: unexpected property {key!r}")
            if isinstance(additional, dict):
                validate_against_schema(
                    value[key], additional, root_schema, f"{location}.{key}"
                )


def validate_https_url(url: str, allowed_hosts: set[str], location: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValidationError(f"{location}: URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValidationError(f"{location}: URL must not contain credentials")
    if parsed.port not in (None, 443):
        raise ValidationError(f"{location}: URL must use the default HTTPS port")
    if parsed.hostname not in allowed_hosts:
        raise ValidationError(
            f"{location}: host {parsed.hostname!r} is not in allowedHosts"
        )
    if not parsed.path.startswith("/"):
        raise ValidationError(f"{location}: URL must contain an absolute path")


def package_configs(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [*config["supportPackages"], *config["trackedPackages"]]


def validate_config(config: dict[str, Any]) -> None:
    expected_keys = {
        "provider",
        "releaseApiUrl",
        "releasePageTemplate",
        "archiveUrlTemplate",
        "registryName",
        "registryUrl",
        "registryScopes",
        "allowedHosts",
        "supportPackages",
        "trackedPackages",
    }
    if set(config) != expected_keys:
        raise ValidationError(
            f"Config properties must be exactly {sorted(expected_keys)}"
        )
    if config["provider"] != "firebase":
        raise ValidationError("Config provider must be 'firebase'")
    hosts = config["allowedHosts"]
    if not isinstance(hosts, list) or not hosts or any(
        not isinstance(host, str) for host in hosts
    ):
        raise ValidationError("allowedHosts must be a non-empty string array")
    if len(hosts) != len(set(hosts)):
        raise ValidationError("allowedHosts must not contain duplicates")
    allowed_hosts = set(hosts)
    validate_https_url(config["releaseApiUrl"], allowed_hosts, "releaseApiUrl")
    if not isinstance(config["registryName"], str) or not config["registryName"]:
        raise ValidationError("registryName must be a non-empty string")
    registry_url = config["registryUrl"]
    if not isinstance(registry_url, str):
        raise ValidationError("registryUrl must be a string")
    parsed_registry = urlparse(registry_url)
    validate_https_url(registry_url, {"hiukka-hv.github.io"}, "registryUrl")
    if parsed_registry.query or parsed_registry.fragment or registry_url.endswith("/"):
        raise ValidationError(
            "registryUrl must not contain query, fragment, or trailing slash"
        )
    scopes = config["registryScopes"]
    if scopes != ["com.google.firebase", "com.google.external-dependency-manager"]:
        raise ValidationError("registryScopes must cover Firebase and EDM")
    for field in ("releasePageTemplate", "archiveUrlTemplate"):
        template = config[field]
        if not isinstance(template, str):
            raise ValidationError(f"{field} must be a string")
        validate_https_url(
            template.format(packageName="com.google.firebase.app", version="1.2.3"),
            allowed_hosts,
            field,
        )

    if not isinstance(config["supportPackages"], list):
        raise ValidationError("supportPackages must be an array")
    if not isinstance(config["trackedPackages"], list) or not config["trackedPackages"]:
        raise ValidationError("trackedPackages must be a non-empty array")
    names: list[str] = []
    for index, package in enumerate(package_configs(config)):
        if not isinstance(package, dict) or set(package) != PACKAGE_METADATA_KEYS:
            raise ValidationError(
                f"Package config at index {index} must contain {sorted(PACKAGE_METADATA_KEYS)}"
            )
        name = package["name"]
        if not isinstance(name, str) or PACKAGE_RE.fullmatch(name) is None:
            raise ValidationError(f"Invalid package ID: {name!r}")
        for key in ("displayName", "category", "description"):
            if not isinstance(package[key], str) or not package[key]:
                raise ValidationError(f"{name}: {key} must be a non-empty string")
        for key in ("selectable", "defaultSelected"):
            if not isinstance(package[key], bool):
                raise ValidationError(f"{name}: {key} must be a boolean")
        if package["defaultSelected"] and not package["selectable"]:
            raise ValidationError(f"{name}: a non-selectable package cannot be default selected")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValidationError("Package config must not contain duplicate package IDs")
    if config["trackedPackages"][0]["name"] != "com.google.firebase.app":
        raise ValidationError("Firebase App must be the first tracked package")
    if any(package["selectable"] for package in config["supportPackages"]):
        raise ValidationError("Support packages cannot be directly selectable")


def make_installer_manifest(catalog: dict[str, Any]) -> dict[str, Any]:
    releases: list[dict[str, Any]] = []
    for version, release in catalog["releases"].items():
        packages: list[dict[str, Any]] = []
        for package in release["packages"]:
            installer_package = {
                key: value for key, value in package.items() if key != "dependencies"
            }
            installer_package["dependencies"] = [
                {"name": name, "version": dependency_version}
                for name, dependency_version in package["dependencies"].items()
            ]
            packages.append(installer_package)
        releases.append({"version": version, "packages": packages})
    return {
        "schemaVersion": catalog["schemaVersion"],
        "latestVersion": catalog["latestVersion"],
        "recommendedVersion": catalog["recommendedVersion"],
        "releases": releases,
    }


def make_registry_documents(
    catalog: dict[str, Any], config: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Build static npm-compatible metadata documents for Unity Package Manager."""
    documents: dict[str, dict[str, Any]] = {}
    package_metadata = {item["name"]: item for item in package_configs(config)}

    def release_package(release_version: str, package_name: str) -> dict[str, Any]:
        matches = [
            package
            for package in catalog["releases"][release_version]["packages"]
            if package["name"] == package_name
        ]
        if len(matches) != 1:
            raise ValidationError(
                f"{release_version}: expected one catalog entry for {package_name}"
            )
        return matches[0]

    for package_name, metadata in package_metadata.items():
        versions: dict[str, dict[str, Any]] = {}
        for release in catalog["releases"].values():
            package = next(
                item
                for item in release["packages"]
                if item["name"] == package_name
            )
            package_version = package["version"]
            version_document = {
                "_id": f"{package_name}@{package_version}",
                "name": package_name,
                "version": package_version,
                "displayName": metadata["displayName"],
                "description": metadata["description"],
                "dependencies": package["dependencies"],
                "dist": {
                    "tarball": package["archiveUrl"],
                    "shasum": package["sha1"],
                    "integrity": package["integrity"],
                },
            }
            existing = versions.get(package_version)
            if existing is not None and existing != version_document:
                raise ValidationError(
                    f"{package_name} {package_version}: conflicting registry metadata"
                )
            versions[package_version] = version_document

        latest = release_package(catalog["latestVersion"], package_name)["version"]
        recommended = release_package(
            catalog["recommendedVersion"], package_name
        )["version"]
        documents[package_name] = {
            "_id": package_name,
            "name": package_name,
            "description": metadata["description"],
            "dist-tags": {"latest": latest, "recommended": recommended},
            "versions": versions,
        }

    documents["index.json"] = {
        "name": config["registryName"],
        "url": config["registryUrl"],
        "scopes": config["registryScopes"],
        "latestFirebaseVersion": catalog["latestVersion"],
        "recommendedFirebaseVersion": catalog["recommendedVersion"],
        "packages": [
            {"name": item["name"], "displayName": item["displayName"]}
            for item in package_configs(config)
        ],
    }
    return documents


def validate_catalog_data(
    catalog: dict[str, Any], config: dict[str, Any], schema: dict[str, Any]
) -> None:
    validate_config(config)
    validate_against_schema(catalog, schema)
    if catalog["provider"] != config["provider"]:
        raise ValidationError("Catalog provider does not match config provider")
    releases = catalog["releases"]
    for pointer in ("latestVersion", "recommendedVersion"):
        if catalog[pointer] not in releases:
            raise ValidationError(f"{pointer} must reference a catalog release")

    allowed_hosts = set(config["allowedHosts"])
    configs = package_configs(config)
    expected_names = [package["name"] for package in configs]
    config_by_name = {package["name"]: package for package in configs}
    tracked_names = {package["name"] for package in config["trackedPackages"]}
    for release_version, release in releases.items():
        expected_source = config["releasePageTemplate"].format(version=release_version)
        if release["source"] != expected_source:
            raise ValidationError(
                f"releases.{release_version}.source must be {expected_source}"
            )
        validate_https_url(release["source"], allowed_hosts, "release source")
        names = [package["name"] for package in release["packages"]]
        if names != expected_names:
            raise ValidationError(
                f"releases.{release_version}.packages must follow config order"
            )
        packages_by_name = {package["name"]: package for package in release["packages"]}
        for package in release["packages"]:
            name = package["name"]
            metadata = config_by_name[name]
            for key in PACKAGE_METADATA_KEYS - {"name"}:
                if package[key] != metadata[key]:
                    raise ValidationError(f"{name}: {key} does not match config")
            package_version = package["version"]
            if name in tracked_names and package_version != release_version:
                raise ValidationError(
                    f"{name}: Firebase package version must be {release_version}"
                )
            expected_url = config["archiveUrlTemplate"].format(
                packageName=name, version=package_version
            )
            if package["archiveUrl"] != expected_url:
                raise ValidationError(f"{name}: archiveUrl must be {expected_url}")
            validate_https_url(package["archiveUrl"], allowed_hosts, f"{name}.archiveUrl")
            if SHA256_RE.fullmatch(package["sha256"]) is None:
                raise ValidationError(f"{name}: invalid SHA-256")
            if SHA1_RE.fullmatch(package["sha1"]) is None:
                raise ValidationError(f"{name}: invalid SHA-1")
            if INTEGRITY_RE.fullmatch(package["integrity"]) is None:
                raise ValidationError(f"{name}: invalid npm integrity")
            for dependency, dependency_version in package["dependencies"].items():
                target = packages_by_name.get(dependency)
                if target is None:
                    raise ValidationError(f"{name}: untracked dependency {dependency}")
                if target["version"] != dependency_version:
                    raise ValidationError(
                        f"{name}: dependency {dependency} must use {target['version']}"
                    )
                if dependency.startswith("com.google.firebase.") and (
                    dependency_version != release_version
                ):
                    raise ValidationError(
                        f"{name}: Firebase dependency {dependency} must use {release_version}"
                    )


def validate_repository(
    config_path: Path = DEFAULT_CONFIG,
    catalog_path: Path = DEFAULT_CATALOG,
    schema_path: Path = DEFAULT_SCHEMA,
    installer_catalog_path: Path = DEFAULT_INSTALLER_CATALOG,
    registry_path: Path = DEFAULT_REGISTRY,
) -> None:
    config = load_json(config_path)
    catalog = load_json(catalog_path)
    validate_catalog_data(catalog, config, load_json(schema_path))
    installer_catalog = load_json(installer_catalog_path)
    if installer_catalog != make_installer_manifest(catalog):
        raise ValidationError("Embedded installer catalog is out of date")
    expected_documents = make_registry_documents(catalog, config)
    actual_names = {
        path.name
        for path in registry_path.iterdir()
        if path.is_file() and path.name != ".nojekyll"
    }
    if actual_names != set(expected_documents):
        raise ValidationError("Static UPM registry file set is out of date")
    for name, expected in expected_documents.items():
        if load_json(registry_path / name) != expected:
            raise ValidationError(
                f"Static UPM registry document is out of date: {name}"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument(
        "--installer-catalog", type=Path, default=DEFAULT_INSTALLER_CATALOG
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_repository(
            args.config,
            args.catalog,
            args.schema,
            args.installer_catalog,
            args.registry,
        )
    except ValidationError as exc:
        print(f"Catalog validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Catalog validation passed: {args.catalog}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
