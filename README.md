# UnityArchitect SDK Catalog

This repository contains a small, reviewable catalog for Unity SDK packages. It stores metadata only and never vendors Firebase archives or other SDK binaries.

## Firebase catalog

The initial Firebase release is `13.15.0` and tracks:

- `com.google.firebase.app`
- `com.google.firebase.analytics`
- `com.google.firebase.remote-config`

Package archives always resolve to Google's official registry:

```text
https://dl.google.com/games/registry/unity/{packageName}/{packageName}-{version}.tgz
```

`catalog/firebase.json` distinguishes two version signals:

- `latestVersion` is maintained by automation after an official stable Firebase release is downloaded and verified.
- `recommendedVersion` changes only after manual integration testing and review.

A catalog update does not modify a Unity project and does not automatically upgrade any installed SDK. Consumers must explicitly select and apply a catalog version.

## Validation

The implementation uses only the Python standard library:

```bash
python scripts/validate_catalog.py
python -m unittest discover -s tests -v
```

To check for a new stable Firebase release and update the catalog locally:

```bash
python scripts/sync_firebase.py
```

The sync process:

1. Reads the latest stable tag from the official `firebase/firebase-unity-sdk` GitHub repository.
2. Builds only the configured package URLs on `dl.google.com`.
3. Downloads archives into a temporary directory.
4. Verifies HTTPS and allowed hosts, archive SHA-256, `package/package.json`, package name/version, configured dependencies, and same-version Firebase dependencies.
5. Adds the verified release and changes only `latestVersion`; `recommendedVersion` remains untouched.

The scheduled workflow opens or updates a pull request only when the catalog has a diff. It never merges the pull request.
