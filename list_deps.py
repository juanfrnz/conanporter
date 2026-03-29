#!/usr/bin/env python3
"""List and optionally download all direct and transitive Conan dependencies.

Usage:
    python list_deps.py <path_to_conanfile_dir> [--profile <profile>]
    python list_deps.py <path_to_conanfile_dir> --download <output_dir>

When --download is specified, each dependency is saved as a .tgz via
`conan cache save`, using filesystem-safe names. A manifest.json file
maps each .tgz filename back to its original Conan reference so packages
can be restored and uploaded to another registry:

    conan cache restore <file.tgz>
    conan upload <ref> -r <remote> -c

Requires Conan 2.x installed and accessible in PATH.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_conan_graph(conanfile_dir: Path, profile: str | None) -> dict:
    cmd = ["conan", "graph", "info", str(conanfile_dir), "--format=json"]
    if profile:
        cmd += ["--profile", profile]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running conan graph info:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def extract_deps(graph_json: dict) -> tuple[dict, dict]:
    """Returns (direct_deps, transitive_deps) as {name: version} dicts."""
    nodes = graph_json.get("graph", {}).get("nodes", {})

    # Node "0" is the root (the consumer conanfile)
    root = nodes.get("0", {})
    direct_dep_ids = set()
    for dep_list in root.get("dependencies", {}).values():
        direct_dep_ids.add(dep_list["ref"])

    direct = {}
    transitive = {}

    for node_id, node in nodes.items():
        if node_id == "0":
            continue
        ref = node.get("ref", "")
        if "/" not in ref:
            continue
        name, version = ref.split("/", 1)
        if ref in direct_dep_ids:
            direct[name] = version
        else:
            transitive[name] = version

    return direct, transitive


def ref_to_filename(ref: str) -> str:
    """Convert a Conan ref like 'spdlog/1.17.0' to a safe filename 'spdlog_1.17.0.tgz'."""
    return ref.replace("/", "_") + ".tgz"


def download_deps(all_deps: dict[str, str], output_dir: Path) -> None:
    """Download all dependencies as .tgz files and write a manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    total = len(all_deps)

    for i, name in enumerate(sorted(all_deps), 1):
        version = all_deps[name]
        ref = f"{name}/{version}"
        filename = ref_to_filename(ref)
        dest = output_dir / filename

        print(f"  [{i}/{total}] Saving {ref} -> {filename}")
        cmd = ["conan", "cache", "save", ref, "--file", str(dest)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    WARNING: Failed to save {ref}:\n    {result.stderr.strip()}", file=sys.stderr)
            continue

        manifest[filename] = ref

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"\nManifest written to {manifest_path}")

    # Also write a restore script for convenience
    restore_script = output_dir / "restore.sh"
    with open(restore_script, "w") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("# Restore all packages into the local Conan cache, then upload to a remote.\n")
        f.write('# Usage: ./restore.sh [remote_name]\n\n')
        f.write('set -euo pipefail\n')
        f.write('SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n')
        f.write('REMOTE="${1:-}"\n\n')
        f.write('for tgz in "$SCRIPT_DIR"/*.tgz; do\n')
        f.write('    echo "Restoring $(basename "$tgz") ..."\n')
        f.write('    conan cache restore "$tgz"\n')
        f.write('done\n\n')
        f.write('if [ -n "$REMOTE" ]; then\n')
        f.write('    echo "\\nUploading to remote: $REMOTE"\n')
        f.write('    while IFS= read -r ref; do\n')
        f.write('        echo "Uploading $ref ..."\n')
        f.write('        conan upload "$ref" -r "$REMOTE" -c\n')
        f.write('    done < <(python3 -c "\n')
        f.write("import json, sys\n")
        f.write("m = json.load(open(sys.argv[1]))\n")
        f.write("print('\\\\n'.join(m.values()))\n")
        f.write('" "$SCRIPT_DIR/manifest.json")\n')
        f.write('    echo "Done."\n')
        f.write('fi\n')
    restore_script.chmod(0o755)
    print(f"Restore script written to {restore_script}")


def main():
    parser = argparse.ArgumentParser(description="List Conan dependencies (direct + transitive)")
    parser.add_argument("path", help="Path to directory containing conanfile.py")
    parser.add_argument("--profile", "-p", help="Conan profile to use", default=None)
    parser.add_argument("--download", "-d", metavar="OUTPUT_DIR",
                        help="Download all dependencies as .tgz files into this directory")
    args = parser.parse_args()

    conanfile_dir = Path(args.path).resolve()
    if not (conanfile_dir / "conanfile.py").exists():
        print(f"Error: No conanfile.py found in {conanfile_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Resolving dependency graph for {conanfile_dir / 'conanfile.py'} ...")
    graph_json = run_conan_graph(conanfile_dir, args.profile)
    direct, transitive = extract_deps(graph_json)

    print(f"\n{'=' * 60}")
    print(f"  Direct dependencies ({len(direct)})")
    print(f"{'=' * 60}")
    for name in sorted(direct):
        print(f"  {name}/{direct[name]}")

    print(f"\n{'=' * 60}")
    print(f"  Transitive dependencies ({len(transitive)})")
    print(f"{'=' * 60}")
    for name in sorted(transitive):
        print(f"  {name}/{transitive[name]}")

    total = len(direct) + len(transitive)
    print(f"\nTotal: {len(direct)} direct + {len(transitive)} transitive = {total} dependencies")

    if args.download:
        output_dir = Path(args.download).resolve()
        print(f"\nDownloading all dependencies to {output_dir} ...")
        all_deps = {**direct, **transitive}
        download_deps(all_deps, output_dir)


if __name__ == "__main__":
    main()
