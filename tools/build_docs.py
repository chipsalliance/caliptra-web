#!/usr/bin/env python3
"""
build_docs.py - Build unified Caliptra documentation using mdbook

This script fetches documentation from multiple Caliptra repositories
and builds a unified mdbook for each supported version.

Usage:
    python build_docs.py [--version VERSION] [--output DIR] [--verbose]

Requirements:
    - Python 3.10+
    - mdbook (https://rust-lang.github.io/mdBook/)
    - mdbook-mermaid (for diagram support)
    - PyYAML (optional, for OCP bibliography support)
    - Internet connection (to fetch from GitHub)

OCP Format Support:
    This script can convert .ocp files (OCP specification format) to
    standard GitHub-flavored markdown. OCP format includes:
    - YAML frontmatter
    - LaTeX commands (tableofcontents, listoffigures, etc.)
    - Grid tables (pandoc format with +---+ borders)
    - Bibliography references [@{ref-id}]
    - Cross-references (@sec:, @tbl:, @fig:)
    - Image attributes {width=... #fig:...}
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# =============================================================================
# Constants
# =============================================================================

VERSIONS = ["1.0", "1.1", "1.2", "2.0", "2.1"]
LATEST_VERSION = "2.1"

# PlantUML configuration
PLANTUML_VERSION = "1.2025.0"
PLANTUML_JAR = f"plantuml-asl-{PLANTUML_VERSION}.jar"
PLANTUML_URL = f"https://github.com/plantuml/plantuml/releases/download/v{PLANTUML_VERSION}/{PLANTUML_JAR}"

GITHUB_REPOS: dict[str, str] = {}  # Populated from caliptra-docs.json in main()

# Fetch cache directory (set via --cache flag)
CACHE_DIR: Optional[Path] = None

# Path to local caliptra-docs.json (relative to this script)
CALIPTRA_DOCS_JSON_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "data" / "caliptra-docs.json"
)

# Regex patterns
IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HTML_IMG_PATTERN = re.compile(
    r'<img\s+([^>]*?)src=["\']([^"\']+)["\']([^>]*?)>', re.IGNORECASE
)
LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
SUMMARY_ENTRY_PATTERN = re.compile(r"^(\s*)-\s*\[([^\]]+)\]\(([^)]+)\)")
INCLUDE_PATTERN = re.compile(r"\{\{#include\s+([^}]+)\}\}")

# OCP format patterns
OCP_BIBREF_PATTERN = re.compile(r"\[@\{([^}]+)\}\]")
OCP_CROSSREF_PATTERN = re.compile(r"@(sec|tbl|fig):([a-zA-Z0-9_-]+)")
OCP_ANCHOR_PATTERN = re.compile(r"\{#(sec|tbl|fig):([^}]+)\}")
OCP_IMG_ATTR_PATTERN = re.compile(r"\{(?:width=[^}]+)?(?:\s*#fig:[^}]+)?\}")
OCP_LATEX_COMMANDS = re.compile(
    r"^\\(tableofcontents|listoffigures|listoftables|currenttemplateversion|beginappendices|endappendices)\s*$",
    re.MULTILINE,
)

# Sphinx directive patterns
SPHINX_DOC_PATTERN = re.compile(r"\{doc\}`([^`]+)`")
SPHINX_INCLUDE_PATTERN = re.compile(r"```\{include\}\s*([^\n]+)\n```", re.MULTILINE)

# =============================================================================
# Logging
# =============================================================================

logger = logging.getLogger("build_docs")


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


# =============================================================================
# Exceptions
# =============================================================================


class DocBuildError(Exception):
    """Base exception for documentation build errors."""

    pass


class FetchError(DocBuildError):
    """Failed to fetch a document from GitHub."""

    pass


class BuildError(DocBuildError):
    """mdbook build failed."""

    pass


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class DocEntry:
    """Represents a document to include in the mdbook."""

    title: str
    source_repo: str
    source_path: str
    dest_path: str
    min_version: Optional[str] = None
    max_version: Optional[str] = None
    required: bool = True
    children: list["DocEntry"] = field(default_factory=list)


# =============================================================================
# Document Structure Definition
# =============================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_STRUCTURE_PATH = REPO_ROOT / "src" / "data" / "caliptra-docs-structure.json"


def _load_doc_structure() -> list[DocEntry]:
    """Load document structure from the shared JSON file."""
    with open(DOC_STRUCTURE_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return [DocEntry(**entry) for entry in raw]


DOC_STRUCTURE = _load_doc_structure()


# =============================================================================
# Version Comparison
# =============================================================================


def version_compare(v1: str, v2: str) -> int:
    """
    Compare two version strings.
    Returns: -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2
    """
    parts1 = [int(x) for x in v1.split(".")]
    parts2 = [int(x) for x in v2.split(".")]

    # Pad shorter version with zeros
    while len(parts1) < len(parts2):
        parts1.append(0)
    while len(parts2) < len(parts1):
        parts2.append(0)

    for p1, p2 in zip(parts1, parts2):
        if p1 < p2:
            return -1
        elif p1 > p2:
            return 1
    return 0


def version_applies(
    version: str, min_version: Optional[str], max_version: Optional[str] = None
) -> bool:
    """Check if a document applies to the given version."""
    if min_version is not None and version_compare(version, min_version) < 0:
        return False
    if max_version is not None and version_compare(version, max_version) > 0:
        return False
    return True


# =============================================================================
# Fetching Functions
# =============================================================================


def _cache_key(url: str) -> Path:
    """Return a cache file path for a URL, preserving readable structure."""
    # Strip scheme and create a path-safe key
    key = url.replace("https://", "").replace("http://", "")
    # Replace characters that are problematic in filenames
    key = key.replace("?", "_q_").replace("&", "_a_").replace("=", "_e_")
    return CACHE_DIR / key


def fetch_url(url: str, retries: int = 3, binary: bool = False) -> bytes | str:
    """
    Fetch URL content with retries and proper error handling.
    Uses disk cache when CACHE_DIR is set.

    Args:
        url: URL to fetch
        retries: Number of retry attempts
        binary: If True, return bytes; otherwise decode to string

    Returns:
        Content as bytes or string

    Raises:
        FetchError: If fetch fails after all retries
    """
    # Check cache first
    if CACHE_DIR is not None:
        cache_path = _cache_key(url)
        if cache_path.exists():
            logger.debug(f"Cache hit: {url}")
            content = cache_path.read_bytes()
            return content if binary else content.decode("utf-8")

    for attempt in range(retries):
        try:
            logger.debug(f"Fetching: {url}")
            req = urllib.request.Request(
                url, headers={"User-Agent": "Caliptra-Doc-Builder/1.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read()
                # Write to cache
                if CACHE_DIR is not None:
                    cache_path = _cache_key(url)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(content)
                return content if binary else content.decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FetchError(f"Document not found: {url}")
            elif e.code == 403:
                raise FetchError(f"Rate limited or forbidden: {url}")
            logger.warning(f"HTTP {e.code} on attempt {attempt + 1}: {url}")
        except urllib.error.URLError as e:
            logger.warning(f"Network error on attempt {attempt + 1}: {e}")
        except Exception as e:
            logger.warning(f"Error on attempt {attempt + 1}: {e}")

        if attempt < retries - 1:
            sleep_time = 2**attempt
            logger.debug(f"Retrying in {sleep_time}s...")
            time.sleep(sleep_time)

    raise FetchError(f"Failed to fetch after {retries} attempts: {url}")


def resolve_commit(repo_key: str, ref: str) -> str:
    """
    Resolve a branch/tag/HEAD to actual commit SHA using GitHub API.

    Args:
        repo_key: Key in GITHUB_REPOS dict
        ref: Git reference (branch, tag, or "HEAD")

    Returns:
        Commit SHA
    """
    if not ref:
        ref = "main"

    # Check if it looks like a full SHA (40 hex chars)
    if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower()):
        return ref

    repo = GITHUB_REPOS[repo_key]
    url = f"https://api.github.com/repos/{repo}/commits/{ref}"

    try:
        content = fetch_url(url)
        data = json.loads(content)
        return data["sha"]
    except FetchError:
        logger.warning(f"Could not resolve commit for {repo_key}/{ref}, using '{ref}'")
        return ref


def get_raw_url(repo_key: str, commit: str, path: str) -> str:
    """Construct GitHub raw content URL."""
    repo = GITHUB_REPOS[repo_key]
    return f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"


def fetch_document(repo_key: str, commit: str, path: str) -> str:
    """Fetch a markdown document from GitHub."""
    url = get_raw_url(repo_key, commit, path)
    return fetch_url(url)


def fetch_binary(repo_key: str, commit: str, path: str) -> bytes:
    """Fetch a binary file (image) from GitHub."""
    url = get_raw_url(repo_key, commit, path)
    return fetch_url(url, binary=True)


# =============================================================================
# Configuration
# =============================================================================


def load_docs_json() -> dict[str, dict[str, str]]:
    """
    Load caliptra-docs.json and populate GITHUB_REPOS.

    Returns:
        Dict mapping version -> {repo: commit/ref}
    """
    global GITHUB_REPOS

    data = json.loads(CALIPTRA_DOCS_JSON_PATH.read_text())
    repos = data.get("repositories", {})

    # Populate GITHUB_REPOS from JSON
    for key, repo_info in repos.items():
        GITHUB_REPOS[key] = repo_info["github"]

    # Build commits config: {version: {repo: ref}}
    # Collect all version keys across all repos
    all_versions = set()
    for repo_info in repos.values():
        all_versions.update(repo_info.get("versions", {}).keys())
    all_versions.discard("default")

    commits_config = {}
    for version in sorted(all_versions):
        version_commits = {}
        for repo_key, repo_info in repos.items():
            repo_versions = repo_info.get("versions", {})
            ref = repo_versions.get(version, repo_versions.get("default", ""))
            if ref:
                version_commits[repo_key] = ref
        commits_config[version] = version_commits

    return commits_config



# =============================================================================
# CLI
# =============================================================================


def main() -> int:
    """Main entry point."""
    from doc_builder.diff_generation import generate_all_version_diffs
    from doc_builder.mdbook_build import build_version, generate_version_index

    parser = argparse.ArgumentParser(
        description="Build unified Caliptra documentation using mdbook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          Build all versions
  %(prog)s --version 2.0            Build only version 2.0
  %(prog)s --clean --version 2.1    Clean and rebuild version 2.1
        """,
    )

    parser.add_argument(
        "--version",
        "-V",
        action="append",
        dest="versions",
        choices=VERSIONS,
        help="Version(s) to build (default: all)",
    )

    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("book"),
        help="Output directory (default: book/)",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be done without doing it",
    )

    parser.add_argument(
        "--clean", action="store_true", help="Clean output directory before building"
    )

    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Cache directory for GitHub fetches (speeds up repeated builds)",
    )

    parser.add_argument(
        "--diffs-only",
        action="store_true",
        help="Only regenerate version diffs (requires existing _sources)",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose)

    # Setup cache
    global CACHE_DIR
    if args.cache:
        CACHE_DIR = args.cache.resolve()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"Using fetch cache: {CACHE_DIR}")

    # Determine versions to build
    versions = args.versions or VERSIONS

    # Handle --diffs-only mode
    if args.diffs_only:
        # Need GITHUB_REPOS populated for diffs
        load_docs_json()
        output_path = args.output.resolve()
        if not output_path.exists():
            logger.error(f"Output directory not found: {output_path}")
            return 1
        # Find versions with _sources directories
        available_versions = [
            v for v in versions if (output_path / v / "_sources").exists()
        ]
        if len(available_versions) < 2:
            logger.error("Need at least 2 versions with _sources to generate diffs")
            return 1
        logger.info(f"Regenerating diffs for versions: {available_versions}")
        generate_all_version_diffs(output_path, available_versions)
        generate_version_index(available_versions, output_path)
        logger.info("Diffs regenerated!")
        return 0

    # Load configuration from caliptra-docs.json
    commits_config = load_docs_json()

    # Ensure output path is absolute (needed because mdbook runs in temp dir)
    output_path = args.output.resolve()

    # Clean if requested
    if args.clean and output_path.exists():
        logger.info(f"Cleaning {output_path}...")
        shutil.rmtree(output_path)

    # Create output directory
    output_path.mkdir(parents=True, exist_ok=True)

    # Build each version
    success = True
    built_versions = []

    for version in versions:
        version_commits = commits_config.get(version, {})
        if build_version(version, version_commits, output_path, args.dry_run):
            built_versions.append(version)
        else:
            success = False

    # Generate version index and diffs
    if built_versions and not args.dry_run:
        # Generate diffs between adjacent versions first
        generate_all_version_diffs(output_path, built_versions)

        # Then generate version index (which includes links to diffs)
        generate_version_index(built_versions, output_path)

    if success:
        logger.info("Documentation build complete!")
    else:
        logger.error("Some versions failed to build")

    return 0 if success else 1


if __name__ == "__main__":
    # Register as 'build_docs' so sub-modules importing by name get the same object
    sys.modules.setdefault("build_docs", sys.modules[__name__])
    sys.exit(main())
