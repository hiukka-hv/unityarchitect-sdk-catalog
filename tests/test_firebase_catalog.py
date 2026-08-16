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

    def test_repository_catalog_is_valid(self) -> None:
        validate_catalog.validate_catalog_data(
            self.catalog, self.config, self.schema
        )

    def test_initial_versions_and_packages(self) -> None:
        self.assertEqual("13.15.0", self.catalog["latestVersion"])
        self.assertEqual("13.15.0", self.catalog["recommendedVersion"])
        packages = self.catalog["releases"]["13.15.0"]["packages"]
        self.assertEqual(
            [package["name"] for package in self.config["trackedPackages"]],
            [package["name"] for package in packages],
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
        catalog["releases"]["13.15.0"]["packages"][1]["dependencies"][
            "com.google.firebase.app"
        ] = "13.14.0"
        with self.assertRaises(validate_catalog.ValidationError):
            validate_catalog.validate_catalog_data(catalog, self.config, self.schema)

    def test_untrusted_archive_host_is_rejected(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        catalog["releases"]["13.15.0"]["packages"][0]["archiveUrl"] = (
            "https://example.com/firebase.tgz"
        )
        with self.assertRaises(validate_catalog.ValidationError):
            validate_catalog.validate_catalog_data(catalog, self.config, self.schema)


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
        self.assertEqual(
            {"com.google.firebase.app": "13.15.0"}, dependencies
        )

    def test_archive_rejects_cross_version_firebase_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.tgz"
            self._write_archive(archive, dependency_version="13.14.0")
            with self.assertRaisesRegex(sync_firebase.SyncError, "must use 13.15.0"):
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
