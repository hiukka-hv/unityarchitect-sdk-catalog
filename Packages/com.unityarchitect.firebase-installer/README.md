# UnityArchitect Firebase Installer

Open **Tools > UnityArchitect > Firebase SDK Installer**, choose a verified catalog release, select individual Firebase modules, and click **Install selected modules**.

The installer downloads official `.tgz` archives from `dl.google.com` into the Unity project's `GooglePackages` directory, verifies SHA-256 before use, resolves package dependencies, and updates `Packages/manifest.json` once. It never upgrades packages without an explicit button click.

For reusable package manifests that need short version requirements such as `"com.google.firebase.analytics": "13.15.0"`, configure the UnityArchitect Firebase scoped registry documented in the repository README.
