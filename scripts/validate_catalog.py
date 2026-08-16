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
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PACKAGE_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValidationError(f"Unsupported schema type: {expected}")


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
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError(f"{location}: value is not in the allowed enum")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValidationError(f"{location}: string is too short")
        pattern = schema.get("pattern")
        if pattern and re.search(pattern, value) is None:
            raise ValidationError(f"{location}: value does not match {pattern!r}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValidationError(f"{location}: array has too few items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_against_schema(
                    item, item_schema, root_schema, f"{location}[{index}]"
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


def resolve_dependencies(package_config: dict[str, Any], version: str) -> dict[str, str]:
    dependencies = package_config.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValidationError(
            f"Config package {package_config.get('name')!r} dependencies must be an object"
        )
    resolved: dict[str, str] = {}
    for name, dependency_version in dependencies.items():
        if not isinstance(name, str) or PACKAGE_RE.fullmatch(name) is None:
            raise ValidationError(f"Invalid dependency package ID in config: {name!r}")
        if not isinstance(dependency_version, str):
            raise ValidationError(f"Dependency version for {name} must be a string")
        resolved[name] = dependency_version.replace("{version}", version)
    return resolved


def validate_config(config: dict[str, Any]) -> None:
    expected_keys = {
        "provider",
        "releaseApiUrl",
        "releasePageTemplate",
        "archiveUrlTemplate",
        "allowedHosts",
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
    for field in ("releasePageTemplate", "archiveUrlTemplate"):
        template = config[field]
        if not isinstance(template, str):
            raise ValidationError(f"{field} must be a string")
        sample = template.format(
            packageName="com.google.firebase.app", version="1.2.3"
        )
        validate_https_url(sample, allowed_hosts, field)

    packages = config["trackedPackages"]
    if not isinstance(packages, list) or not packages:
        raise ValidationError("trackedPackages must be a non-empty array")
    names: list[str] = []
    for index, package in enumerate(packages):
        if not isinstance(package, dict) or set(package) != {"name", "dependencies"}:
            raise ValidationError(
                f"trackedPackages[{index}] must contain name and dependencies"
            )
        name = package["name"]
        if not isinstance(name, str) or PACKAGE_RE.fullmatch(name) is None:
            raise ValidationError(f"Invalid tracked package ID: {name!r}")
        names.append(name)
        resolve_dependencies(package, "1.2.3")
    if len(names) != len(set(names)):
        raise ValidationError("trackedPackages must not contain duplicate package IDs")


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
    expected_names = [package["name"] for package in config["trackedPackages"]]
    config_by_name = {
        package["name"]: package for package in config["trackedPackages"]
    }
    for version, release in releases.items():
        if VERSION_RE.fullmatch(version) is None:
            raise ValidationError(f"Invalid release version key: {version!r}")
        expected_source = config["releasePageTemplate"].format(version=version)
        if release["source"] != expected_source:
            raise ValidationError(
                f"releases.{version}.source must be {expected_source}"
            )
        validate_https_url(
            release["source"], allowed_hosts, f"releases.{version}.source"
        )
        package_names = [package["name"] for package in release["packages"]]
        if package_names != expected_names:
            raise ValidationError(
                f"releases.{version}.packages must follow trackedPackages order"
            )
        if len(package_names) != len(set(package_names)):
            raise ValidationError(f"releases.{version} contains duplicate packages")

        for package in release["packages"]:
            name = package["name"]
            expected_url = config["archiveUrlTemplate"].format(
                packageName=name, version=version
            )
            if package["archiveUrl"] != expected_url:
                raise ValidationError(
                    f"{name} {version}: archiveUrl must be {expected_url}"
                )
            validate_https_url(
                package["archiveUrl"], allowed_hosts, f"{name} {version}.archiveUrl"
            )
            if SHA256_RE.fullmatch(package["sha256"]) is None:
                raise ValidationError(f"{name} {version}: invalid SHA-256")
            expected_dependencies = resolve_dependencies(config_by_name[name], version)
            if package["dependencies"] != expected_dependencies:
                raise ValidationError(
                    f"{name} {version}: dependencies do not match config"
                )
            for dependency, dependency_version in package["dependencies"].items():
                if dependency.startswith("com.google.firebase.") and dependency_version != version:
                    raise ValidationError(
                        f"{name} {version}: Firebase dependency {dependency} "
                        f"must use the same version"
                    )


def validate_repository(
    config_path: Path = DEFAULT_CONFIG,
    catalog_path: Path = DEFAULT_CATALOG,
    schema_path: Path = DEFAULT_SCHEMA,
) -> None:
    validate_catalog_data(
        load_json(catalog_path), load_json(config_path), load_json(schema_path)
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_repository(args.config, args.catalog, args.schema)
    except ValidationError as exc:
        print(f"Catalog validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Catalog validation passed: {args.catalog}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
