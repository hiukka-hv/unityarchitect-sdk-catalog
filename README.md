# UnityArchitect SDK Catalog

This repository contains a small, reviewable catalog for Unity SDK packages. It stores metadata only and never vendors Firebase archives or other SDK binaries.

## Short version dependencies

Add the generated registry once to the consuming Unity project's `Packages/manifest.json`:

```json
"scopedRegistries": [
  {
    "name": "UnityArchitect Firebase Registry",
    "url": "https://hiukka-hv.github.io/unityarchitect-sdk-catalog",
    "scopes": [
      "com.google.firebase",
      "com.google.external-dependency-manager"
    ]
  }
]
```

Firebase requirements can then use normal, short UPM versions in either the project manifest or a reusable package manifest:

```json
"dependencies": {
  "com.google.firebase.analytics": "13.15.0",
  "com.google.firebase.remote-config": "13.15.0"
}
```

Unity resolves transitive Firebase and EDM dependencies from static npm-compatible metadata deployed through GitHub Pages. Every tarball still comes directly from `dl.google.com`; this repository and the Pages site contain metadata only. The registry becomes available after this change is merged and the repository's Pages source is set to **GitHub Actions**.

## Firebase UPM installer

After this package is merged, install the bootstrap package from Unity Package Manager with **Add package from git URL**:

```text
https://github.com/hiukka-hv/unityarchitect-sdk-catalog.git?path=/Packages/com.unityarchitect.firebase-installer
```

Then open **Tools > UnityArchitect > Firebase SDK Installer**. The editor window separates selectable modules into:

- Foundation
- Analytics
- Identity & Security
- Data
- Backend
- Engagement
- Reliability
- AI

External Dependency Manager and Firebase App are resolved automatically. The initial recommended selection is Analytics plus Remote Config, but every product module can be selected independently. Realtime Database and Cloud Storage automatically include Authentication because their official package metadata requires it.

The installer downloads verified archives into `GooglePackages` next to the Unity project's `Packages` folder and updates `Packages/manifest.json` once. Add `GooglePackages/` to the game repository's `.gitignore`; Firebase binaries are never committed to this catalog repository.

## Firebase catalog

The initial Firebase release is `13.15.0` and catalogs all current official modules:

- App (Core), Analytics, App Check, Authentication
- Realtime Database, Cloud Firestore, Cloud Storage
- Cloud Functions, Firebase Installations, Cloud Messaging
- Remote Config, Crashlytics, Firebase AI Logic

Package archives always resolve to Google's official registry:

```text
https://dl.google.com/games/registry/unity/{packageName}/{packageName}-{version}.tgz
```

`catalog/firebase.json` distinguishes two version signals:

- `latestVersion` is maintained by automation after an official stable Firebase release is downloaded and verified.
- `recommendedVersion` changes only after manual integration testing and review.

A catalog update does not modify a Unity project and does not automatically upgrade any installed SDK. A user must open the installer and explicitly select a release and modules.

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
4. Verifies HTTPS and allowed hosts, archive SHA-256, `package/package.json`, package name/version, and the complete dependency graph.
5. Generates the canonical catalog, embedded Unity installer manifest, and static UPM registry metadata.
6. Changes only `latestVersion`; `recommendedVersion` remains untouched.

The scheduled workflow opens or updates a pull request only when the catalog has a diff. It never merges the pull request.
