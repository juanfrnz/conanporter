#!/usr/bin/env python3
"""List and optionally download all direct and transitive Conan 1.x dependencies.

Usage:
    python list_deps_v1.py <path_to_conanfile_dir> [--profile <profile>]
    python list_deps_v1.py <path_to_conanfile_dir> --download <output_dir> --remote <remote>

When --download is specified, each dependency is first fully fetched from
the remote (recipe + sources + binaries) via `conan download`, then its
cache folder is archived into a .tgz. This avoids the "exports_sources
not found in local cache" error that occurs when packages were only
installed (binaries only) rather than fully downloaded.

A manifest.json maps each .tgz back to its original Conan reference for
restoring on the target side.

On the target machine, use the generated restore.sh:
    ./restore.sh                  # restore into local cache
    ./restore.sh my-remote        # restore + upload to a remote

Requires Conan 1.x installed and accessible in PATH.
"""

import argparse
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path


def get_conan_cache_dir():
    """Return the Conan 1.x data directory (typically ~/.conan/data)."""
    # Conan 1.x respects CONAN_USER_HOME; default is ~/.conan
    conan_home = os.environ.get("CONAN_USER_HOME", os.path.expanduser("~"))
    return Path(conan_home) / ".conan" / "data"


def run_conan_info(conanfile_dir, profile):
    """Run `conan info` and return parsed JSON."""
    cmd = ["conan", "info", str(conanfile_dir), "--json"]
    if profile:
        cmd += ["--profile", profile]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("Error running conan info:\n{}".format(result.stderr), file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def parse_ref(reference):
    """Parse 'name/version' or 'name/version@user/channel' into (name, version, user, channel)."""
    if "@" in reference:
        name_ver, user_chan = reference.split("@", 1)
        parts = user_chan.split("/", 1)
        user = parts[0]
        channel = parts[1] if len(parts) > 1 else "_"
    else:
        name_ver = reference
        user = "_"
        channel = "_"
    name, version = name_ver.split("/", 1)
    return name, version, user, channel


def extract_deps(info_json):
    """Return (direct_deps, transitive_deps) as {ref_string: (name, version, user, channel)} dicts.

    In Conan 1 JSON output, the root node's reference looks like
    'conanfile.py' or 'conanfile.py (projname/version)'. Its 'requires'
    list gives the direct dependencies.
    """
    # Find root node — its reference starts with "conanfile.py"
    root = None
    for node in info_json:
        ref = node.get("reference", "")
        if ref.startswith("conanfile.py"):
            root = node
            break

    if root is None:
        print("Error: Could not find root conanfile node in conan info output.", file=sys.stderr)
        sys.exit(1)

    direct_refs = set(root.get("requires", []))

    direct = {}
    transitive = {}

    for node in info_json:
        ref = node.get("reference", "")
        if ref.startswith("conanfile.py"):
            continue
        if "/" not in ref:
            continue

        parsed = parse_ref(ref)
        if ref in direct_refs:
            direct[ref] = parsed
        else:
            transitive[ref] = parsed

    return direct, transitive


def ref_to_filename(ref):
    """Convert a Conan ref to a safe .tgz filename.

    'spdlog/1.17.0@user/channel' -> 'spdlog_1.17.0_user_channel.tgz'
    'spdlog/1.17.0'              -> 'spdlog_1.17.0.tgz'
    """
    safe = ref.replace("/", "_").replace("@", "_")
    return safe + ".tgz"


def cache_path_for_ref(cache_dir, name, version, user, channel):
    """Return the Conan 1.x cache path for a reference."""
    return cache_dir / name / version / user / channel


def fetch_from_remote(ref, remote):
    """Fully download a package (recipe + sources + binaries) from a remote."""
    cmd = ["conan", "download", ref, "-r", remote]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("    WARNING: Failed to download {} from '{}':\n    {}".format(
            ref, remote, result.stderr.strip()), file=sys.stderr)
        return False
    return True


def download_deps(all_deps, output_dir, remote):
    """Archive each dependency's cache folder into a .tgz and write a manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = get_conan_cache_dir()
    manifest = {}
    total = len(all_deps)

    for i, ref in enumerate(sorted(all_deps), 1):
        name, version, user, channel = all_deps[ref]

        # Fetch full package from remote first (recipe + exports_sources + binaries)
        if remote:
            print("  [{}/{}] Downloading {} from '{}' ...".format(i, total, ref, remote))
            if not fetch_from_remote(ref, remote):
                print("    Skipping archive for {}".format(ref), file=sys.stderr)
                continue

        pkg_path = cache_path_for_ref(cache_dir, name, version, user, channel)

        filename = ref_to_filename(ref)
        dest = output_dir / filename

        if not pkg_path.exists():
            print("  [{}/{}] SKIP {} (not in local cache at {})".format(i, total, ref, pkg_path),
                  file=sys.stderr)
            continue

        print("  [{}/{}] Archiving {} -> {}".format(i, total, ref, filename))
        # Archive preserving the relative path structure: name/version/user/channel/...
        # so it can be extracted directly into ~/.conan/data/
        arcname_base = str(Path(name) / version / user / channel)
        with tarfile.open(dest, "w:gz") as tar:
            tar.add(str(pkg_path), arcname=arcname_base)

        manifest[filename] = ref

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print("\nManifest written to {}".format(manifest_path))

    # Write restore script
    restore_script = output_dir / "restore.sh"
    with open(restore_script, "w") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("# Restore Conan 1.x packages into local cache, then optionally upload.\n")
        f.write("# Usage: ./restore.sh [remote_name]\n\n")
        f.write("set -euo pipefail\n")
        f.write('SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n')
        f.write('REMOTE="${1:-}"\n\n')
        f.write('CONAN_HOME="${CONAN_USER_HOME:-$HOME}"\n')
        f.write('CACHE_DIR="$CONAN_HOME/.conan/data"\n')
        f.write('mkdir -p "$CACHE_DIR"\n\n')
        f.write('echo "Restoring packages to $CACHE_DIR ..."\n')
        f.write('for tgz in "$SCRIPT_DIR"/*.tgz; do\n')
        f.write('    echo "  Extracting $(basename "$tgz") ..."\n')
        f.write('    tar -xzf "$tgz" -C "$CACHE_DIR"\n')
        f.write('done\n\n')
        f.write('if [ -n "$REMOTE" ]; then\n')
        f.write('    echo ""\n')
        f.write('    echo "Uploading to remote: $REMOTE"\n')
        f.write('    while IFS= read -r ref; do\n')
        f.write('        echo "  Uploading $ref ..."\n')
        f.write('        conan upload "$ref" -r "$REMOTE" --all --confirm\n')
        f.write('    done < <(python3 -c "\n')
        f.write("import json, sys\n")
        f.write("m = json.load(open(sys.argv[1]))\n")
        f.write("print('\\\\n'.join(m.values()))\n")
        f.write('" "$SCRIPT_DIR/manifest.json")\n')
        f.write('    echo "Done."\n')
        f.write("fi\n")
    restore_script.chmod(0o755)
    print("Restore script written to {}".format(restore_script))


def main():
    parser = argparse.ArgumentParser(description="List Conan 1.x dependencies (direct + transitive)")
    parser.add_argument("path", help="Path to directory containing conanfile.py")
    parser.add_argument("--profile", "-p", help="Conan profile to use", default=None)
    parser.add_argument("--download", "-d", metavar="OUTPUT_DIR",
                        help="Download all dependencies as .tgz files into this directory")
    parser.add_argument("--remote", "-r",
                        help="Conan remote to fully download packages from before archiving "
                             "(fetches recipe + exports_sources + binaries). "
                             "Required to avoid 'sources not found in local cache' errors.")
    args = parser.parse_args()

    conanfile_dir = Path(args.path).resolve()
    if not (conanfile_dir / "conanfile.py").exists():
        print("Error: No conanfile.py found in {}".format(conanfile_dir), file=sys.stderr)
        sys.exit(1)

    print("Resolving dependency graph for {} ...".format(conanfile_dir / "conanfile.py"))
    info_json = run_conan_info(conanfile_dir, args.profile)
    direct, transitive = extract_deps(info_json)

    print("\n{}".format("=" * 60))
    print("  Direct dependencies ({})".format(len(direct)))
    print("=" * 60)
    for ref in sorted(direct):
        print("  {}".format(ref))

    print("\n{}".format("=" * 60))
    print("  Transitive dependencies ({})".format(len(transitive)))
    print("=" * 60)
    for ref in sorted(transitive):
        print("  {}".format(ref))

    total = len(direct) + len(transitive)
    print("\nTotal: {} direct + {} transitive = {} dependencies".format(
        len(direct), len(transitive), total))

    if args.download:
        output_dir = Path(args.download).resolve()
        print("\nDownloading all dependencies to {} ...".format(output_dir))
        all_deps = {**direct, **transitive}
        download_deps(all_deps, output_dir, args.remote)


if __name__ == "__main__":
    main()
