using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEngine;

namespace UnityArchitect.FirebaseInstaller.Editor
{
#pragma warning disable 0649 // Populated by Unity JsonUtility.
    [Serializable]
    internal sealed class InstallerCatalog
    {
        public int schemaVersion;
        public string latestVersion;
        public string recommendedVersion;
        public InstallerRelease[] releases;
    }

    [Serializable]
    internal sealed class InstallerRelease
    {
        public string version;
        public InstallerPackage[] packages;
    }

    [Serializable]
    internal sealed class InstallerPackage
    {
        public string name;
        public string displayName;
        public string category;
        public string description;
        public bool selectable;
        public bool defaultSelected;
        public string version;
        public string archiveUrl;
        public string sha256;
        public InstallerDependency[] dependencies;
    }

    [Serializable]
    internal sealed class InstallerDependency
    {
        public string name;
        public string version;
    }
#pragma warning restore 0649

    public sealed class FirebaseInstallerWindow : EditorWindow
    {
        private const string CatalogAssetPath =
            "Packages/com.unityarchitect.firebase-installer/Editor/Resources/firebase-catalog.json";
        private const long MaxArchiveBytes = 512L * 1024L * 1024L;
        private static readonly HttpClient HttpClient = new HttpClient(
            new HttpClientHandler { AllowAutoRedirect = true }
        );

        private readonly Dictionary<string, bool> _selection =
            new Dictionary<string, bool>(StringComparer.Ordinal);
        private InstallerCatalog _catalog;
        private int _releaseIndex;
        private Vector2 _scroll;
        private bool _installing;
        private float _progress;
        private string _status = string.Empty;

        [MenuItem("Tools/UnityArchitect/Firebase SDK Installer")]
        public static void Open()
        {
            FirebaseInstallerWindow window = GetWindow<FirebaseInstallerWindow>();
            window.titleContent = new GUIContent("Firebase Installer");
            window.minSize = new Vector2(560f, 520f);
            window.Show();
        }

        private void OnEnable()
        {
            LoadCatalog();
        }

        private void LoadCatalog()
        {
            TextAsset asset = AssetDatabase.LoadAssetAtPath<TextAsset>(CatalogAssetPath);
            if (asset == null)
            {
                _catalog = null;
                _status = "Embedded Firebase catalog was not found.";
                return;
            }

            try
            {
                _catalog = JsonUtility.FromJson<InstallerCatalog>(asset.text);
                if (_catalog == null || _catalog.releases == null || _catalog.releases.Length == 0)
                {
                    throw new InvalidDataException("Catalog has no releases.");
                }

                _releaseIndex = Array.FindIndex(
                    _catalog.releases,
                    release => release.version == _catalog.recommendedVersion
                );
                if (_releaseIndex < 0)
                {
                    _releaseIndex = 0;
                }
                ResetSelection();
                _status = string.Empty;
            }
            catch (Exception exception)
            {
                _catalog = null;
                _status = "Cannot load Firebase catalog: " + exception.Message;
            }
        }

        private InstallerRelease SelectedRelease
        {
            get
            {
                if (_catalog == null || _catalog.releases == null)
                {
                    return null;
                }
                return _catalog.releases[Mathf.Clamp(_releaseIndex, 0, _catalog.releases.Length - 1)];
            }
        }

        private void ResetSelection()
        {
            _selection.Clear();
            InstallerRelease release = SelectedRelease;
            if (release == null || release.packages == null)
            {
                return;
            }
            foreach (InstallerPackage package in release.packages)
            {
                if (package.selectable)
                {
                    _selection[package.name] = package.defaultSelected;
                }
            }
        }

        private void OnGUI()
        {
            EditorGUILayout.Space(8f);
            EditorGUILayout.LabelField("UnityArchitect Firebase SDK Installer", EditorStyles.boldLabel);
            EditorGUILayout.LabelField(
                "Choose individual Firebase modules. Required packages are resolved automatically.",
                EditorStyles.wordWrappedLabel
            );
            EditorGUILayout.Space(8f);

            if (_catalog == null)
            {
                EditorGUILayout.HelpBox(_status, MessageType.Error);
                if (GUILayout.Button("Reload catalog"))
                {
                    LoadCatalog();
                }
                return;
            }

            EditorGUI.BeginDisabledGroup(_installing);
            string[] releaseLabels = _catalog.releases.Select(ReleaseLabel).ToArray();
            int newReleaseIndex = EditorGUILayout.Popup("Firebase release", _releaseIndex, releaseLabels);
            if (newReleaseIndex != _releaseIndex)
            {
                _releaseIndex = newReleaseIndex;
                ResetSelection();
            }

            InstallerRelease release = SelectedRelease;
            Dictionary<string, string> installed = InstalledPackages();
            _scroll = EditorGUILayout.BeginScrollView(_scroll);
            DrawRequiredPackages(release, installed);
            DrawSelectablePackages(release, installed);
            EditorGUILayout.EndScrollView();

            EditorGUILayout.Space(6f);
            EditorGUILayout.HelpBox(
                "Archives are downloaded only from dl.google.com, verified with SHA-256, " +
                "and stored in GooglePackages next to the project's Packages folder. " +
                "Add GooglePackages/ to the game repository's .gitignore.",
                MessageType.Info
            );
            bool hasSelection = _selection.Values.Any(value => value);
            EditorGUI.BeginDisabledGroup(!hasSelection);
            if (GUILayout.Button("Install selected modules", GUILayout.Height(34f)))
            {
                InstallSelectedModules();
            }
            EditorGUI.EndDisabledGroup();
            EditorGUI.EndDisabledGroup();

            if (_installing)
            {
                Rect progressRect = GUILayoutUtility.GetRect(10f, 20f, GUILayout.ExpandWidth(true));
                EditorGUI.ProgressBar(progressRect, _progress, _status);
                Repaint();
            }
            else if (!string.IsNullOrEmpty(_status))
            {
                EditorGUILayout.HelpBox(_status, MessageType.None);
            }
        }

        private string ReleaseLabel(InstallerRelease release)
        {
            List<string> labels = new List<string>();
            if (release.version == _catalog.recommendedVersion)
            {
                labels.Add("recommended");
            }
            if (release.version == _catalog.latestVersion)
            {
                labels.Add("latest");
            }
            return labels.Count == 0
                ? release.version
                : release.version + " (" + string.Join(", ", labels.ToArray()) + ")";
        }

        private static void DrawRequiredPackages(
            InstallerRelease release,
            Dictionary<string, string> installed
        )
        {
            InstallerPackage[] required = release.packages
                .Where(package => !package.selectable)
                .ToArray();
            if (required.Length == 0)
            {
                return;
            }
            EditorGUILayout.Space(6f);
            EditorGUILayout.LabelField("Automatic dependencies", EditorStyles.boldLabel);
            foreach (InstallerPackage package in required)
            {
                string state = InstalledState(package, installed);
                EditorGUILayout.LabelField(
                    "  " + package.displayName + " " + package.version + state,
                    EditorStyles.miniLabel
                );
            }
        }

        private void DrawSelectablePackages(
            InstallerRelease release,
            Dictionary<string, string> installed
        )
        {
            string currentCategory = null;
            foreach (InstallerPackage package in release.packages.Where(item => item.selectable))
            {
                if (currentCategory != package.category)
                {
                    currentCategory = package.category;
                    EditorGUILayout.Space(8f);
                    EditorGUILayout.LabelField(currentCategory, EditorStyles.boldLabel);
                }

                EditorGUILayout.BeginVertical(EditorStyles.helpBox);
                bool selected = _selection.TryGetValue(package.name, out bool value) && value;
                string state = InstalledState(package, installed);
                _selection[package.name] = EditorGUILayout.ToggleLeft(
                    package.displayName + " " + package.version + state,
                    selected,
                    EditorStyles.boldLabel
                );
                EditorGUILayout.LabelField(package.description, EditorStyles.wordWrappedMiniLabel);
                EditorGUILayout.EndVertical();
            }
        }

        private static string InstalledState(
            InstallerPackage package,
            Dictionary<string, string> installed
        )
        {
            if (!installed.TryGetValue(package.name, out string version))
            {
                return string.Empty;
            }
            return version == package.version
                ? "  [installed]"
                : "  [installed " + version + "]";
        }

        private static Dictionary<string, string> InstalledPackages()
        {
            try
            {
                return UnityEditor.PackageManager.PackageInfo.GetAllRegisteredPackages()
                    .Where(package => !string.IsNullOrEmpty(package.name))
                    .GroupBy(package => package.name)
                    .ToDictionary(group => group.Key, group => group.First().version);
            }
            catch
            {
                return new Dictionary<string, string>(StringComparer.Ordinal);
            }
        }

        private async void InstallSelectedModules()
        {
            if (_installing)
            {
                return;
            }
            try
            {
                InstallerRelease release = SelectedRelease;
                List<InstallerPackage> packages = ResolveSelectedPackages(release);
                string summary = string.Join(
                    "\n",
                    packages.Select(package => "• " + package.displayName + " " + package.version).ToArray()
                );
                if (!EditorUtility.DisplayDialog(
                    "Install Firebase modules",
                    "The following verified packages will be installed or updated:\n\n" + summary,
                    "Download and install",
                    "Cancel"
                ))
                {
                    return;
                }

                _installing = true;
                _progress = 0f;
                Dictionary<string, string> manifestEntries = new Dictionary<string, string>();
                string packageDirectory = Path.GetFullPath(
                    Path.Combine(Application.dataPath, "..", "GooglePackages")
                );
                Directory.CreateDirectory(packageDirectory);

                for (int index = 0; index < packages.Count; index++)
                {
                    InstallerPackage package = packages[index];
                    _status = "Verifying " + package.displayName;
                    _progress = (float)index / packages.Count;
                    Repaint();
                    string archivePath = await DownloadAndVerify(package, packageDirectory);
                    manifestEntries[package.name] =
                        "file:../GooglePackages/" + Path.GetFileName(archivePath);
                }

                _progress = 1f;
                _status = "Updating Packages/manifest.json";
                Repaint();
                ManifestPatcher.UpdateDependencies(manifestEntries);
                _installing = false;
                _status = "Verified packages added. Unity Package Manager is resolving dependencies.";
                EditorUtility.DisplayDialog(
                    "Firebase installer",
                    "All archives passed SHA-256 verification. Unity will now resolve the selected packages.",
                    "OK"
                );
                Client.Resolve();
            }
            catch (Exception exception)
            {
                _installing = false;
                _progress = 0f;
                _status = "Installation failed: " + exception.Message;
                Debug.LogException(exception);
                EditorUtility.DisplayDialog("Firebase installation failed", exception.Message, "OK");
            }
            finally
            {
                Repaint();
            }
        }

        private List<InstallerPackage> ResolveSelectedPackages(InstallerRelease release)
        {
            Dictionary<string, InstallerPackage> byName = release.packages.ToDictionary(
                package => package.name,
                StringComparer.Ordinal
            );
            List<InstallerPackage> ordered = new List<InstallerPackage>();
            HashSet<string> visiting = new HashSet<string>(StringComparer.Ordinal);
            HashSet<string> visited = new HashSet<string>(StringComparer.Ordinal);
            foreach (KeyValuePair<string, bool> selected in _selection)
            {
                if (selected.Value)
                {
                    Visit(selected.Key, byName, visiting, visited, ordered);
                }
            }
            if (ordered.Count == 0)
            {
                throw new InvalidOperationException("Select at least one Firebase module.");
            }
            return ordered;
        }

        private static void Visit(
            string name,
            Dictionary<string, InstallerPackage> packages,
            HashSet<string> visiting,
            HashSet<string> visited,
            List<InstallerPackage> ordered
        )
        {
            if (visited.Contains(name))
            {
                return;
            }
            if (!packages.TryGetValue(name, out InstallerPackage package))
            {
                throw new InvalidDataException("Catalog dependency is missing: " + name);
            }
            if (!visiting.Add(name))
            {
                throw new InvalidDataException("Catalog dependency cycle at " + name);
            }
            if (package.dependencies != null)
            {
                foreach (InstallerDependency dependency in package.dependencies)
                {
                    Visit(dependency.name, packages, visiting, visited, ordered);
                }
            }
            visiting.Remove(name);
            visited.Add(name);
            ordered.Add(package);
        }

        private async Task<string> DownloadAndVerify(
            InstallerPackage package,
            string destinationDirectory
        )
        {
            ValidatePackage(package);
            string fileName = package.name + "-" + package.version + ".tgz";
            string destination = Path.Combine(destinationDirectory, fileName);
            if (File.Exists(destination) && FileSha256(destination) == package.sha256)
            {
                return destination;
            }

            string temporary = destination + ".download";
            if (File.Exists(temporary))
            {
                File.Delete(temporary);
            }
            try
            {
                using (HttpRequestMessage request = new HttpRequestMessage(
                    HttpMethod.Get,
                    package.archiveUrl
                ))
                using (HttpResponseMessage response = await HttpClient.SendAsync(
                    request,
                    HttpCompletionOption.ResponseHeadersRead
                ))
                {
                    response.EnsureSuccessStatusCode();
                    ValidateDownloadUri(response.RequestMessage.RequestUri);
                    if (
                        response.Content.Headers.ContentLength.HasValue
                        && response.Content.Headers.ContentLength.Value > MaxArchiveBytes
                    )
                    {
                        throw new InvalidDataException("Archive is larger than the safety limit.");
                    }

                    long total = 0;
                    byte[] buffer = new byte[1024 * 1024];
                    using (Stream input = await response.Content.ReadAsStreamAsync())
                    using (FileStream output = new FileStream(
                        temporary,
                        FileMode.Create,
                        FileAccess.Write,
                        FileShare.None,
                        buffer.Length,
                        true
                    ))
                    using (SHA256 digest = SHA256.Create())
                    {
                        while (true)
                        {
                            int count = await input.ReadAsync(buffer, 0, buffer.Length);
                            if (count == 0)
                            {
                                break;
                            }
                            total += count;
                            if (total > MaxArchiveBytes)
                            {
                                throw new InvalidDataException(
                                    "Archive is larger than the safety limit."
                                );
                            }
                            digest.TransformBlock(buffer, 0, count, null, 0);
                            await output.WriteAsync(buffer, 0, count);
                        }
                        digest.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
                        string actual = Hex(digest.Hash);
                        if (!string.Equals(actual, package.sha256, StringComparison.Ordinal))
                        {
                            throw new InvalidDataException(
                                package.name + " SHA-256 mismatch. Download was rejected."
                            );
                        }
                    }
                    if (total == 0)
                    {
                        throw new InvalidDataException("Downloaded archive is empty.");
                    }
                }

                if (File.Exists(destination))
                {
                    File.Delete(destination);
                }
                File.Move(temporary, destination);
                return destination;
            }
            finally
            {
                if (File.Exists(temporary))
                {
                    File.Delete(temporary);
                }
            }
        }

        private static void ValidatePackage(InstallerPackage package)
        {
            if (
                string.IsNullOrEmpty(package.name)
                || !package.name.StartsWith("com.google.", StringComparison.Ordinal)
                || package.name.Any(
                    character => !char.IsLetterOrDigit(character)
                        && character != '.'
                        && character != '-'
                )
                || package.name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0
            )
            {
                throw new InvalidDataException("Catalog contains an invalid package name.");
            }
            if (string.IsNullOrEmpty(package.version) || string.IsNullOrEmpty(package.sha256))
            {
                throw new InvalidDataException(package.name + " has incomplete metadata.");
            }
            string[] versionParts = package.version.Split('.');
            if (
                versionParts.Length != 3
                || versionParts.Any(part => part.Length == 0 || part.Any(character => !char.IsDigit(character)))
            )
            {
                throw new InvalidDataException(package.name + " has an invalid stable version.");
            }
            if (
                package.sha256.Length != 64
                || package.sha256.Any(character => !Uri.IsHexDigit(character))
            )
            {
                throw new InvalidDataException(package.name + " has an invalid SHA-256.");
            }
            string expectedUrl = "https://dl.google.com/games/registry/unity/"
                + package.name + "/" + package.name + "-" + package.version + ".tgz";
            if (!string.Equals(package.archiveUrl, expectedUrl, StringComparison.Ordinal))
            {
                throw new InvalidDataException(package.name + " has an unexpected archive URL.");
            }
            ValidateDownloadUri(new Uri(expectedUrl, UriKind.Absolute));
        }

        private static void ValidateDownloadUri(Uri uri)
        {
            if (
                uri == null
                || uri.Scheme != Uri.UriSchemeHttps
                || !string.Equals(uri.Host, "dl.google.com", StringComparison.OrdinalIgnoreCase)
                || !uri.IsDefaultPort
                || !string.IsNullOrEmpty(uri.UserInfo)
            )
            {
                throw new InvalidDataException(
                    "Firebase archives must use HTTPS on dl.google.com."
                );
            }
        }

        private static string FileSha256(string path)
        {
            using (FileStream stream = File.OpenRead(path))
            using (SHA256 digest = SHA256.Create())
            {
                return Hex(digest.ComputeHash(stream));
            }
        }

        private static string Hex(byte[] bytes)
        {
            StringBuilder builder = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes)
            {
                builder.Append(value.ToString("x2"));
            }
            return builder.ToString();
        }
    }

    internal static class ManifestPatcher
    {
        public static void UpdateDependencies(Dictionary<string, string> updates)
        {
            string manifestPath = Path.GetFullPath(
                Path.Combine(Application.dataPath, "..", "Packages", "manifest.json")
            );
            string json = File.ReadAllText(manifestPath, Encoding.UTF8);
            Tuple<int, int> range = FindObjectRange(json, "dependencies");
            List<KeyValuePair<string, string>> dependencies = ParseStringObject(
                json,
                range.Item1,
                range.Item2
            );
            foreach (KeyValuePair<string, string> update in updates)
            {
                int index = dependencies.FindIndex(pair => pair.Key == update.Key);
                if (index >= 0)
                {
                    dependencies[index] = update;
                }
                else
                {
                    dependencies.Add(update);
                }
            }

            string indent = LineIndent(json, range.Item1);
            StringBuilder replacement = new StringBuilder();
            replacement.Append("{\n");
            for (int index = 0; index < dependencies.Count; index++)
            {
                KeyValuePair<string, string> pair = dependencies[index];
                replacement.Append(indent).Append("  \"")
                    .Append(Escape(pair.Key)).Append("\": \"")
                    .Append(Escape(pair.Value)).Append("\"");
                if (index + 1 < dependencies.Count)
                {
                    replacement.Append(',');
                }
                replacement.Append('\n');
            }
            replacement.Append(indent).Append('}');
            string updated = json.Substring(0, range.Item1)
                + replacement
                + json.Substring(range.Item2 + 1);
            string temporary = manifestPath + ".unityarchitect.tmp";
            File.WriteAllText(temporary, updated, new UTF8Encoding(false));
            try
            {
                File.Replace(temporary, manifestPath, null);
            }
            catch (PlatformNotSupportedException)
            {
                File.Copy(temporary, manifestPath, true);
                File.Delete(temporary);
            }
        }

        private static Tuple<int, int> FindObjectRange(string json, string propertyName)
        {
            int depth = 0;
            for (int index = 0; index < json.Length; index++)
            {
                char character = json[index];
                if (character == '"')
                {
                    int cursor = index;
                    string token = ReadString(json, ref cursor);
                    if (depth == 1 && token == propertyName)
                    {
                        int value = cursor;
                        SkipWhitespace(json, ref value);
                        if (value >= json.Length || json[value] != ':')
                        {
                            throw new InvalidDataException("Invalid project manifest.");
                        }
                        value++;
                        SkipWhitespace(json, ref value);
                        if (value >= json.Length || json[value] != '{')
                        {
                            throw new InvalidDataException(
                                "Project manifest dependencies must be an object."
                            );
                        }
                        return Tuple.Create(value, MatchingBrace(json, value));
                    }
                    index = cursor - 1;
                    continue;
                }
                if (character == '{' || character == '[')
                {
                    depth++;
                }
                else if (character == '}' || character == ']')
                {
                    depth--;
                }
            }
            throw new InvalidDataException("Project manifest has no dependencies object.");
        }

        private static int MatchingBrace(string json, int opening)
        {
            int depth = 0;
            for (int index = opening; index < json.Length; index++)
            {
                if (json[index] == '"')
                {
                    int cursor = index;
                    ReadString(json, ref cursor);
                    index = cursor - 1;
                    continue;
                }
                if (json[index] == '{')
                {
                    depth++;
                }
                else if (json[index] == '}' && --depth == 0)
                {
                    return index;
                }
            }
            throw new InvalidDataException("Unclosed dependencies object in project manifest.");
        }

        private static List<KeyValuePair<string, string>> ParseStringObject(
            string json,
            int opening,
            int closing
        )
        {
            List<KeyValuePair<string, string>> pairs =
                new List<KeyValuePair<string, string>>();
            int cursor = opening + 1;
            while (cursor < closing)
            {
                SkipWhitespaceAndCommas(json, ref cursor);
                if (cursor >= closing)
                {
                    break;
                }
                string key = ReadString(json, ref cursor);
                SkipWhitespace(json, ref cursor);
                if (cursor >= closing || json[cursor++] != ':')
                {
                    throw new InvalidDataException("Invalid dependency entry in manifest.");
                }
                SkipWhitespace(json, ref cursor);
                string value = ReadString(json, ref cursor);
                pairs.Add(new KeyValuePair<string, string>(key, value));
                SkipWhitespace(json, ref cursor);
                if (cursor < closing && json[cursor] == ',')
                {
                    cursor++;
                }
            }
            return pairs;
        }

        private static string ReadString(string json, ref int cursor)
        {
            if (cursor >= json.Length || json[cursor] != '"')
            {
                throw new InvalidDataException("Expected a JSON string.");
            }
            cursor++;
            StringBuilder value = new StringBuilder();
            while (cursor < json.Length)
            {
                char character = json[cursor++];
                if (character == '"')
                {
                    return value.ToString();
                }
                if (character != '\\')
                {
                    value.Append(character);
                    continue;
                }
                if (cursor >= json.Length)
                {
                    throw new InvalidDataException("Invalid JSON escape.");
                }
                char escaped = json[cursor++];
                switch (escaped)
                {
                    case '"': value.Append('"'); break;
                    case '\\': value.Append('\\'); break;
                    case '/': value.Append('/'); break;
                    case 'b': value.Append('\b'); break;
                    case 'f': value.Append('\f'); break;
                    case 'n': value.Append('\n'); break;
                    case 'r': value.Append('\r'); break;
                    case 't': value.Append('\t'); break;
                    case 'u':
                        if (cursor + 4 > json.Length)
                        {
                            throw new InvalidDataException("Invalid Unicode escape.");
                        }
                        value.Append((char)Convert.ToInt32(json.Substring(cursor, 4), 16));
                        cursor += 4;
                        break;
                    default:
                        throw new InvalidDataException("Unsupported JSON escape.");
                }
            }
            throw new InvalidDataException("Unterminated JSON string.");
        }

        private static string Escape(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private static string LineIndent(string json, int position)
        {
            int lineStart = json.LastIndexOf('\n', Math.Max(0, position - 1));
            lineStart = lineStart < 0 ? 0 : lineStart + 1;
            int cursor = lineStart;
            while (cursor < json.Length && (json[cursor] == ' ' || json[cursor] == '\t'))
            {
                cursor++;
            }
            return json.Substring(lineStart, cursor - lineStart);
        }

        private static void SkipWhitespace(string json, ref int cursor)
        {
            while (cursor < json.Length && char.IsWhiteSpace(json[cursor]))
            {
                cursor++;
            }
        }

        private static void SkipWhitespaceAndCommas(string json, ref int cursor)
        {
            while (
                cursor < json.Length
                && (char.IsWhiteSpace(json[cursor]) || json[cursor] == ',')
            )
            {
                cursor++;
            }
        }
    }
}
