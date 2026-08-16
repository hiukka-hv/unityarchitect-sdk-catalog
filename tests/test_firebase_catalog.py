from __future__ import annotations

import copy
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_firebase  # noqa: E402
import validate_catalog  # noqa: E402


class CatalogValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = validate_catalog.load_json(validate_catalog.DEFAULT_CONFIG)
        cls.catalog = validate_catalog.load_json(validate_catalog.DEFAULT_CATALOG)
        cls.schema = validate_catalog.load_json(validate_catalog.DEFAULT_SCHEMA)

    def test_repository_catalog_and_embedded_manifest_are_valid(self) -> None:
        validate_catalog.validate_repository()

    def test_initial_versions_and_all_official_modules(self) -> None:
        self.assertEqual("13.15.0", self.catalog["latestVersion"])
        self.assertEqual("13.15.0", self.catalog["recommendedVersion"])
        packages = self.catalog["releases"]["13.15.0"]["packages"]
        configured = [
            *self.config["supportPackages"],
            *self.config["trackedPackages"],
        ]
        self.assertEqual(
            [package["name"] for package in configured],
            [package["name"] for package in packages],
        )
        self.assertEqual(14, len(packages))

    def test_modules_are_split_into_categories(self) -> None:
        selectable = [
            package
            for package in self.config["trackedPackages"]
            if package["selectable"]
        ]
        self.assertEqual(
            {
                "AI",
                "Analytics",
                "Backend",
                "Data",
                "Engagement",
                "Foundation",
                "Identity & Security",
                "Reliability",
            },
            {package["category"] for package in selectable},
        )
        defaults = {package["name"] for package in selectable if package["defaultSelected"]}
        self.assertEqual(
            {
                "com.google.firebase.analytics",
                "com.google.firebase.remote-config",
            },
            defaults,
        )

    def test_external_dependency_manager_is_versioned_separately(self) -> None:
        packages = self.catalog["releases"]["13.15.0"]["packages"]
        by_name = {package["name"]: package for package in packages}
        self.assertEqual(
            "1.2.186",
            by_name["com.google.external-dependency-manager"]["version"],
        )
        self.assertEqual(
            {"com.google.external-dependency-manager": "1.2.186"},
            by_name["com.google.firebase.app"]["dependencies"],
        )

    def test_recommended_version_must_exist(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["recommendedVersion"] = "99.0.0"
        with self.assertRaisesRegex(
            validate_catalog.ValidationError, "recommendedVersion"
        ):
            validate_catalog.validate_catalog_data(catalog, self.config, self.schema)

    def test_firebase_dependencies_must_match_release(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        packages = catalog["releases"]["13.15.0"]["packages"]
        analytics = next(
            package
            for package in packages
            if package["name"] == "com.google.firebase.analytics"
        )
        analytics["dependencies"]["com.google.firebase.app"] = "13.14.0"
        with self.assertRaises(validate_catalog.ValidationError):
            validate_catalog.validate_catalog_data(catalog, self.config, self.schema)

    def test_untrusted_archive_host_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["releases"]["13.15.0"]["packages"][0]["archiveUrl"] = (
            "https://example.com/firebase.tgz"
        )
        with self.assertRaises(validate_catalog.ValidationError):
            validate_catalog.validate_catalog_data(catalog, self.config, self.schema)

    def test_installer_package_descriptor(self) -> None:
        descriptor = validate_catalog.load_json(
            ROOT / "Packages" / "com.unityarchitect.firebase-installer" / "package.json"
        )
        self.assertEqual("com.unityarchitect.firebase-installer", descriptor["name"])
        self.assertEqual("0.1.0", descriptor["version"])
        self.assertEqual("2020.1", descriptor["unity"])


class SyncTests(unittest.TestCase):
    def _write_archive(
        self,
        destination: Path,
        name: str = "com.google.firebase.analytics",
        version: str = "13.15.0",
        dependency_version: str = "13.15.0",
    ) -> None:
        metadata = json.dumps(
            {
                "name": name,
                "version": version,
                "dependencies": {
                    "com.google.firebase.app": dependency_version,
                },
            }
        ).encode("utf-8")
        info = tarfile.TarInfo("package/package.json")
        info.size = len(metadata)
        with tarfile.open(destination, "w:gz") as archive:
            archive.addfile(info, io.BytesIO(metadata))

    def test_archive_metadata_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.tgz"
            self._write_archive(archive)
            dependencies = sync_firebase.inspect_archive(
                archive, "com.google.firebase.analytics", "13.15.0"
            )
        self.assertEqual({"com.google.firebase.app": "13.15.0"}, dependencies)

    def test_archive_rejects_wrong_package_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.tgz"
            self._write_archive(archive, name="com.google.firebase.auth")
            with self.assertRaisesRegex(sync_firebase.SyncError, "name mismatch"):
                sync_firebase.inspect_archive(
                    archive, "com.google.firebase.analytics", "13.15.0"
                )

    def test_apply_release_preserves_recommended_version(self) -> None:
        catalog = {
            "latestVersion": "1.0.0",
            "recommendedVersion": "1.0.0",
            "releases": {"1.0.0": {"source": "old", "packages": []}},
        }
        changed = sync_firebase.apply_release(
            catalog, "1.1.0", {"source": "new", "packages": []}
        )
        self.assertTrue(changed)
        self.assertEqual("1.1.0", catalog["latestVersion"])
        self.assertEqual("1.0.0", catalog["recommendedVersion"])


if __name__ == "__main__":
    unittest.main()
