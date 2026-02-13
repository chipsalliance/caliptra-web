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
import difflib
import hashlib
import html
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
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
# OCP Format Conversion
# =============================================================================


def load_bibliography(repo_key: str, commit: str, bib_path: str) -> dict[str, dict]:
    """
    Load bibliography.yaml file and return a dict mapping ref IDs to metadata.

    Args:
        repo_key: Repository key
        commit: Commit SHA
        bib_path: Path to bibliography.yaml

    Returns:
        Dict mapping reference ID to {title, url, publisher, ...}
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed, bibliography links will be simplified")
        return {}

    try:
        content = fetch_document(repo_key, commit, bib_path)
        data = yaml.safe_load(content)
        refs = {}
        for ref in data.get("references", []):
            ref_id = ref.get("id")
            if ref_id:
                refs[ref_id] = ref
        return refs
    except Exception as e:
        logger.warning(f"Could not load bibliography: {e}")
        return {}


def convert_grid_table_to_markdown(table_text: str) -> str:
    """
    Convert a pandoc grid table to GitHub-flavored markdown table.

    Grid tables look like:
    +------+------+
    | Col1 | Col2 |
    +======+======+
    | data | data |
    +------+------+

    Args:
        table_text: The grid table text

    Returns:
        Markdown table text
    """
    lines = table_text.strip().split("\n")
    if not lines:
        return table_text

    result_rows = []
    current_row_cells = []
    header_found = False
    header_row_index = -1

    for i, line in enumerate(lines):
        # Skip border lines but detect header separator
        if line.startswith("+"):
            if "=" in line and current_row_cells:
                # This is the header separator - mark header done
                header_found = True
                header_row_index = len(result_rows)
                # Save current row as header
                result_rows.append(current_row_cells)
                current_row_cells = []
            elif current_row_cells:
                # Regular row separator - save accumulated row
                result_rows.append(current_row_cells)
                current_row_cells = []
            continue

        # Parse data row
        if line.startswith("|"):
            # Split by | and strip
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if not current_row_cells:
                current_row_cells = cells
            else:
                # Multi-line cell - append to existing cells
                for j, cell in enumerate(cells):
                    if j < len(current_row_cells) and cell:
                        if current_row_cells[j]:
                            current_row_cells[j] += " " + cell
                        else:
                            current_row_cells[j] = cell

    # Don't forget last row
    if current_row_cells:
        result_rows.append(current_row_cells)

    if not result_rows:
        return table_text

    # Build markdown table
    md_lines = []

    # Determine column count from first row
    num_cols = max(len(row) for row in result_rows) if result_rows else 0

    for i, row in enumerate(result_rows):
        # Pad row to have consistent columns
        while len(row) < num_cols:
            row.append("")
        md_lines.append("| " + " | ".join(row) + " |")

        # Add header separator after header row
        if header_found and i == header_row_index:
            md_lines.append("| " + " | ".join(["---"] * num_cols) + " |")
        elif not header_found and i == 0:
            # No explicit header, treat first row as header
            md_lines.append("| " + " | ".join(["---"] * num_cols) + " |")

    return "\n".join(md_lines)


def convert_ocp_to_markdown(content: str, bibliography: dict[str, dict] = None) -> str:
    """
    Convert OCP format (.ocp) to standard GitHub-flavored markdown.

    OCP format includes:
    - YAML frontmatter
    - LaTeX commands (tableofcontents, listoffigures, etc.)
    - Grid tables (pandoc format)
    - Bibliography references [@{ref-id}]
    - Cross-references (@sec:, @tbl:, @fig:)
    - Image attributes {width=... #fig:...}

    Args:
        content: OCP format content
        bibliography: Optional dict of bibliography references

    Returns:
        Converted markdown content
    """
    if bibliography is None:
        bibliography = {}

    # 1. Remove YAML frontmatter (between first two ---)
    # Find the start and end of frontmatter
    if content.startswith("---"):
        # Find the closing ---
        end_idx = content.find("\n---", 3)
        if end_idx != -1:
            # Skip past the closing --- and newline
            content = content[end_idx + 4 :].lstrip("\n")

    # 2. Remove LaTeX commands
    content = OCP_LATEX_COMMANDS.sub("", content)

    # 3. Remove standalone --- separators (used as section breaks)
    content = re.sub(r"^\s*---\s*$", "", content, flags=re.MULTILINE)

    # 4. Convert grid tables to markdown tables
    # Grid tables start with +--- and contain | for cells
    grid_table_pattern = re.compile(
        r"((?:^\+[-+=]+\+\s*\n)(?:^\|.*\|\s*\n|^\+[-+=]+\+\s*\n)+)", re.MULTILINE
    )

    def replace_grid_table(match):
        return convert_grid_table_to_markdown(match.group(1)) + "\n"

    content = grid_table_pattern.sub(replace_grid_table, content)

    # 5. Convert bibliography references [@{ref-id}] to links or text
    def replace_bibref(match):
        ref_id = match.group(1)
        ref = bibliography.get(ref_id, {})
        title = ref.get("title", ref_id)
        url = ref.get("url")
        if url:
            return f"[{title}]({url})"
        return f"*{title}*"

    content = OCP_BIBREF_PATTERN.sub(replace_bibref, content)

    # 6. Convert cross-references to markdown links
    # @sec:name -> [Section](#name)
    # @tbl:name -> [Table](#name)
    # @fig:name -> [Figure](#name)
    def replace_crossref(match):
        ref_type = match.group(1)
        ref_name = match.group(2)
        type_labels = {"sec": "Section", "tbl": "Table", "fig": "Figure"}
        label = type_labels.get(ref_type, ref_type)
        return f"[{label}](#{ref_name})"

    content = OCP_CROSSREF_PATTERN.sub(replace_crossref, content)

    # 7. Convert anchor definitions {#sec:name} to HTML anchors
    # Keep them as markdown anchors: {#name}
    def replace_anchor(match):
        ref_name = match.group(2)
        return f"{{#{ref_name}}}"

    content = OCP_ANCHOR_PATTERN.sub(replace_anchor, content)

    # 8. Clean up image attributes - remove width but keep anchor
    # {width=512px #fig:name} -> {#name}
    def clean_img_attr(match):
        attr = match.group(0)
        # Extract figure anchor if present
        fig_match = re.search(r"#fig:([^}\s]+)", attr)
        if fig_match:
            return "{#" + fig_match.group(1) + "}"
        return ""

    content = OCP_IMG_ATTR_PATTERN.sub(clean_img_attr, content)

    # 9. Handle Table: captions - convert to bold text
    # "Table: Caption text {#tbl:name}" -> "**Table: Caption text** {#name}"
    content = re.sub(
        r"^(Table:\s+[^{]+)(\{#[^}]+\})?",
        lambda m: f"**{m.group(1).strip()}**"
        + (f" {m.group(2)}" if m.group(2) else ""),
        content,
        flags=re.MULTILINE,
    )

    # 10. Convert LaTeX math delimiters for mdBook MathJax
    # $$ ... $$ (display math) -> \\[ ... \\]
    # $ ... $ (inline math) -> \\( ... \\)
    # Must do display math first to avoid matching $$ as two inline $
    content = re.sub(r"\$\$(.+?)\$\$", r"\\\\[\g<1>\\\\]", content, flags=re.DOTALL)
    content = re.sub(r"\$([^$\n]+?)\$", r"\\\\(\g<1>\\\\)", content)

    # 11. Clean up multiple blank lines
    content = re.sub(r"\n{3,}", "\n\n", content)

    return content.strip() + "\n"


# =============================================================================
# Content Processing
# =============================================================================


def find_image_refs(content: str) -> list[tuple[str, str]]:
    """
    Find all image references in markdown content.

    Returns:
        List of (alt_text, image_path) tuples
    """
    return IMAGE_PATTERN.findall(content)


def find_link_refs(content: str) -> list[tuple[str, str]]:
    """
    Find all link references in markdown content.

    Returns:
        List of (link_text, href) tuples
    """
    return LINK_PATTERN.findall(content)


def find_include_refs(content: str) -> list[str]:
    """
    Find all mdbook include directives in markdown content.

    Matches patterns like {{#include file.md}} or {{#include ../path/file.md}}

    Returns:
        List of include file paths
    """
    return INCLUDE_PATTERN.findall(content)


def rewrite_image_paths(
    content: str, source_repo: str, source_path: str, dest_path: str
) -> tuple[str, list[str]]:
    """
    Rewrite relative image paths to unified images directory.

    Args:
        content: Markdown content
        source_repo: Repository the document came from
        source_path: Original path within the repository
        dest_path: Destination path in mdbook (used to calculate relative prefix)

    Returns:
        Tuple of (modified content, list of image paths to fetch)
    """
    source_dir = str(Path(source_path).parent)
    images_to_fetch = []

    # Calculate how many directory levels deep the dest file is
    # e.g., "mcu/foo.md" -> 1 level -> need "../"
    # e.g., "mcu/sub/bar.md" -> 2 levels -> need "../../"
    dest_depth = len(Path(dest_path).parent.parts)
    path_prefix = "../" * dest_depth if dest_depth > 0 else ""

    def process_img_path(img_path: str) -> tuple[str, str] | None:
        """Process an image path and return (full_img_path, new_path) or None if should skip."""
        # Skip absolute URLs
        if img_path.startswith(("http://", "https://", "//")):
            return None

        # Skip anchor-only links
        if img_path.startswith("#"):
            return None

        # Normalize the path
        if img_path.startswith("./"):
            img_path = img_path[2:]

        # Resolve relative to source directory
        if source_dir and source_dir != ".":
            full_img_path = str(Path(source_dir) / img_path)
        else:
            full_img_path = img_path

        # Normalize path (resolve .. etc)
        full_img_path = str(Path(full_img_path))

        # Track image for fetching
        images_to_fetch.append((source_repo, full_img_path))

        # Rewrite to images/{repo}/{path} with correct relative prefix
        new_path = f"{path_prefix}images/{source_repo}/{full_img_path}"
        return full_img_path, new_path

    def replace_md_image(match: re.Match) -> str:
        """Replace Markdown-style images: ![alt](path)"""
        alt_text = match.group(1)
        img_path = match.group(2)

        result = process_img_path(img_path)
        if result is None:
            return match.group(0)

        _, new_path = result
        return f"![{alt_text}]({new_path})"

    def replace_html_image(match: re.Match) -> str:
        """Replace HTML-style images: <img src="path">"""
        before_src = match.group(1)
        img_path = match.group(2)
        after_src = match.group(3)

        result = process_img_path(img_path)
        if result is None:
            return match.group(0)

        _, new_path = result
        return f'<img {before_src}src="{new_path}"{after_src}>'

    # Process both Markdown and HTML image references
    modified_content = IMAGE_PATTERN.sub(replace_md_image, content)
    modified_content = HTML_IMG_PATTERN.sub(replace_html_image, modified_content)
    return modified_content, images_to_fetch


def convert_sphinx_directives(
    content: str, source_repo: str, source_path: str
) -> tuple[str, list[tuple[str, str]]]:
    """
    Convert Sphinx/MyST directives to standard markdown.

    Handles:
    - {doc}`name` -> [name](name.md) links
    - ```{include} path``` -> content to be fetched and inlined

    Args:
        content: Markdown content with Sphinx directives
        source_repo: Source repository key
        source_path: Path within source repo (for resolving relative includes)

    Returns:
        Tuple of (converted content, list of (repo, include_path) to fetch)
    """
    includes_to_fetch = []

    # Convert {doc}`name` to markdown links
    # {doc}`overview` -> [overview](overview.md)
    def replace_doc_ref(match):
        doc_name = match.group(1)
        # Use the doc name as both link text and target
        return f"[{doc_name}]({doc_name}.md)"

    content = SPHINX_DOC_PATTERN.sub(replace_doc_ref, content)

    # Find {include} directives and mark them for fetching
    # ```{include} ../../path/file.md``` -> placeholder for later replacement
    def replace_include(match):
        include_path = match.group(1).strip()
        # Resolve relative path from source file location
        source_dir = "/".join(source_path.split("/")[:-1])
        if source_dir:
            # Normalize the path
            full_path = source_dir + "/" + include_path
        else:
            full_path = include_path
        # Normalize .. references
        parts = []
        for part in full_path.split("/"):
            if part == "..":
                if parts:
                    parts.pop()
            elif part and part != ".":
                parts.append(part)
        resolved_path = "/".join(parts)
        includes_to_fetch.append((source_repo, resolved_path))
        # Return a placeholder that will be replaced with actual content
        return f"{{{{INCLUDE:{resolved_path}}}}}"

    content = SPHINX_INCLUDE_PATTERN.sub(replace_include, content)

    return content, includes_to_fetch


def normalize_markdown_tables(content: str) -> str:
    """
    Normalize markdown tables to ensure proper rendering.

    - Converts tabs to spaces
    - Normalizes whitespace in table cells
    - Ensures consistent column separators
    - Fixes tables that are incorrectly indented within list context
    """
    lines = content.split("\n")
    result = []
    in_table = False
    table_lines = []
    pre_table_context = []

    def flush_table():
        """Process accumulated table lines and add to result."""
        if not table_lines:
            return

        # Check if table is improperly indented (starts with whitespace but
        # previous line is a list item ending with colon - indicating the table
        # should follow the list item)
        needs_blank_before = False
        if pre_table_context:
            last_non_empty = None
            for ctx_line in reversed(pre_table_context):
                if ctx_line.strip():
                    last_non_empty = ctx_line.strip()
                    break
            # If previous content ends with colon and table has leading whitespace,
            # the table is likely meant to follow but is malformed
            if last_non_empty and last_non_empty.endswith(":"):
                needs_blank_before = True

        # Add blank line before table if needed for proper parsing
        if needs_blank_before and result and result[-1].strip():
            result.append("")

        for tbl_line in table_lines:
            # Remove leading whitespace from table rows (common error)
            stripped = tbl_line.strip()
            # Replace tabs with spaces
            stripped = stripped.replace("\t", " ")
            # Normalize multiple spaces to single space within cells
            parts = stripped.split("|")
            normalized_parts = []
            for part in parts:
                # Collapse multiple spaces but preserve cell content
                normalized = " ".join(part.split())
                # Add padding for readability
                if normalized:
                    normalized = f" {normalized} "
                normalized_parts.append(normalized)
            result.append("|".join(normalized_parts))

        # Add blank line after table for clean separation
        result.append("")
        table_lines.clear()
        pre_table_context.clear()

    for line in lines:
        stripped = line.strip()

        # Check if this line looks like a table row
        if stripped.startswith("|") and stripped.endswith("|"):
            if not in_table:
                in_table = True
                # Capture last few lines for context
                pre_table_context = result[-3:] if len(result) >= 3 else result[:]
            table_lines.append(line)
        elif in_table:
            # Line doesn't look like table - flush accumulated table
            flush_table()
            in_table = False
            result.append(line)
        else:
            result.append(line)

    # Handle table at end of document
    if table_lines:
        flush_table()

    return "\n".join(result)


def rewrite_external_md_links(
    content: str, source_repo: str, source_path: str, commit: str
) -> str:
    """
    Rewrite relative markdown links to external GitHub URLs.

    Links to .md files that are not part of our doc structure should point
    to the GitHub blob URL so they don't break in mdbook.

    Args:
        content: Markdown content
        source_repo: Repository the document came from
        source_path: Original path within the repository
        commit: Commit SHA for the source

    Returns:
        Modified content with external .md links rewritten
    """
    source_dir = str(Path(source_path).parent)
    github_repo = GITHUB_REPOS.get(source_repo, source_repo)

    def replace_md_link(match: re.Match) -> str:
        """Replace relative .md links with GitHub URLs."""
        link_text = match.group(1)
        link_path = match.group(2)

        # Skip absolute URLs
        if link_path.startswith(("http://", "https://", "//")):
            return match.group(0)

        # Skip anchor-only links
        if link_path.startswith("#"):
            return match.group(0)

        # Only process .md links
        if not link_path.lower().endswith(".md"):
            return match.group(0)

        # Check if this links to a document we're including in our build
        # For now, only rewrite links that go outside our docs directory
        # (links starting with ../ that escape the docs folder)
        if link_path.startswith("../") and not link_path.startswith("../docs/"):
            # Normalize the path relative to source directory
            if source_dir and source_dir != ".":
                full_path = str((Path(source_dir) / link_path).resolve())
                # Remove any leading path components that would go above repo root
                # by using normpath and ensuring no leading ..
                full_path = str(Path(source_dir) / link_path)
                # Normalize .. references
                parts = []
                for part in full_path.split("/"):
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part and part != ".":
                        parts.append(part)
                full_path = "/".join(parts)
            else:
                full_path = link_path

            # Create GitHub blob URL
            github_url = f"https://github.com/{github_repo}/blob/{commit}/{full_path}"
            return f"[{link_text}]({github_url})"

        return match.group(0)

    # Match markdown links: [text](path)
    return LINK_PATTERN.sub(replace_md_link, content)


def process_document(
    content: str,
    source_repo: str,
    source_path: str,
    dest_path: str,
    bibliography: dict[str, dict] = None,
    commit: str = None,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Process a markdown document for inclusion in mdbook.

    Args:
        content: Raw markdown content
        source_repo: Source repository key
        source_path: Path within source repo
        dest_path: Destination path in mdbook
        bibliography: Optional bibliography dict for OCP files
        commit: Commit SHA for rewriting external links

    Returns:
        Tuple of (processed content, list of (repo, image_path) to fetch,
                  list of (repo, include_path) to fetch)
    """
    includes = []

    # Convert OCP format if needed
    if source_path.endswith(".ocp"):
        content = convert_ocp_to_markdown(content, bibliography)
    else:
        # Convert LaTeX math delimiters for mdBook MathJax (for non-OCP files)
        # OCP conversion already handles this
        # $$ ... $$ (display math) -> \\[ ... \\]
        # $ ... $ (inline math) -> \\( ... \\)
        # Must do display math first to avoid matching $$ as two inline $
        content = re.sub(r"\$\$(.+?)\$\$", r"\\\\[\g<1>\\\\]", content, flags=re.DOTALL)
        content = re.sub(r"\$([^$\n]+?)\$", r"\\\\(\g<1>\\\\)", content)

    # Convert Sphinx/MyST directives
    content, includes = convert_sphinx_directives(content, source_repo, source_path)

    # Normalize markdown tables (fix tabs, spacing issues)
    content = normalize_markdown_tables(content)

    # Rewrite external .md links to GitHub URLs
    if commit:
        content = rewrite_external_md_links(content, source_repo, source_path, commit)

    # Rewrite image paths with correct relative prefix based on dest_path depth
    content, images = rewrite_image_paths(content, source_repo, source_path, dest_path)

    return content, images, includes


# =============================================================================
# Version Diff Generation
# =============================================================================

# Adjacent version pairs for diff generation
VERSION_PAIRS = [
    ("2.1", "2.0"),
    ("2.0", "1.2"),
    ("1.2", "1.1"),
    ("1.1", "1.0"),
]

DIFF_CSS = """
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; }
.diff-header { background: #f6f8fa; padding: 10px 15px; border: 1px solid #d0d7de; border-radius: 6px 6px 0 0; }
.diff-header h1 { margin: 0; font-size: 1.25em; }
.diff-header .meta { color: #656d76; font-size: 0.875em; margin-top: 5px; }
.diff-content { border: 1px solid #d0d7de; border-top: none; border-radius: 0 0 6px 6px; overflow: hidden; }
.diff-table { width: 100%; border-collapse: collapse; font-family: ui-monospace, monospace; font-size: 12px; }
.diff-table td { padding: 1px 10px; vertical-align: top; }
.diff-table .line-num { width: 1%; min-width: 50px; text-align: right; color: #656d76; background: #f6f8fa; user-select: none; }
.diff-table .line-content { white-space: pre-wrap; word-break: break-all; }
.diff-add { background-color: #e6ffec; }
.diff-add .line-num { background-color: #ccffd8; }
.diff-del { background-color: #ffebe9; }
.diff-del .line-num { background-color: #ffd7d5; }
.diff-change { background-color: #fff8c5; }
.diff-unchanged { background-color: #ffffff; }
.diff-section { margin: 20px 0; }
.image-diff { display: flex; gap: 20px; flex-wrap: wrap; margin: 20px 0; }
.image-diff .image-panel { flex: 1; min-width: 300px; border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden; }
.image-diff .image-panel h3 { margin: 0; padding: 10px; background: #f6f8fa; border-bottom: 1px solid #d0d7de; font-size: 0.875em; }
.image-diff .image-panel img { max-width: 100%; display: block; padding: 10px; }
.image-same { text-align: center; padding: 20px; background: #f6f8fa; border-radius: 6px; }
.image-same img { max-width: 100%; }
.no-changes { padding: 40px; text-align: center; color: #656d76; background: #f6f8fa; border-radius: 6px; }
.stats { display: flex; gap: 20px; margin: 15px 0; }
.stats .stat { padding: 5px 10px; border-radius: 4px; font-size: 0.875em; }
.stats .additions { background: #dafbe1; color: #116329; }
.stats .deletions { background: #ffebe9; color: #82071e; }
.toc { background: #f6f8fa; padding: 15px; border-radius: 6px; margin-bottom: 20px; }
.toc h2 { margin: 0 0 10px 0; font-size: 1em; }
.toc ul { margin: 0; padding-left: 20px; }
.toc li { margin: 5px 0; }
.toc a { color: #0969da; text-decoration: none; }
.toc a:hover { text-decoration: underline; }
</style>
"""


def generate_diff_html(
    old_content: str,
    new_content: str,
    old_version: str,
    new_version: str,
    doc_title: str,
    image_diffs: list[tuple[str, str, str, bool]] = None,
) -> str:
    """
    Generate an HTML page showing the diff between two document versions.

    Args:
        old_content: Content from older version
        new_content: Content from newer version
        old_version: Version string of older doc (e.g., "2.0")
        new_version: Version string of newer doc (e.g., "2.1")
        doc_title: Title of the document
        image_diffs: List of (image_name, old_path, new_path, is_same) tuples

    Returns:
        HTML content for the diff page
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    # Generate unified diff
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"{doc_title} (v{old_version})",
            tofile=f"{doc_title} (v{new_version})",
            lineterm="",
        )
    )

    # Count additions and deletions
    additions = sum(
        1 for line in diff if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in diff if line.startswith("-") and not line.startswith("---")
    )

    # Build HTML
    html_parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8">',
        f"<title>Diff: {html.escape(doc_title)} - v{new_version} vs v{old_version}</title>",
        DIFF_CSS,
        "</head>",
        "<body>",
        '<div class="diff-header">',
        f"<h1>Changes to {html.escape(doc_title)}</h1>",
        f'<div class="meta">Comparing version {new_version} to {old_version}</div>',
        "</div>",
    ]

    # Stats
    html_parts.append('<div class="stats">')
    html_parts.append(f'<span class="stat additions">+{additions} additions</span>')
    html_parts.append(f'<span class="stat deletions">-{deletions} deletions</span>')
    html_parts.append("</div>")

    if additions == 0 and deletions == 0:
        html_parts.append(
            '<div class="no-changes">No text changes between versions</div>'
        )
    else:
        # Render diff as table
        html_parts.append('<div class="diff-content">')
        html_parts.append('<table class="diff-table">')

        old_line_num = 0
        new_line_num = 0
        in_hunk = False

        for line in diff:
            if line.startswith("@@"):
                # Parse hunk header
                match = re.match(r"^@@ -(\d+)", line)
                if match:
                    old_line_num = int(match.group(1)) - 1
                match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
                if match:
                    new_line_num = int(match.group(1)) - 1
                in_hunk = True
                html_parts.append(
                    f'<tr class="diff-section"><td colspan="4" style="background:#f3f0ff;padding:8px;font-weight:bold;">{html.escape(line.strip())}</td></tr>'
                )
            elif line.startswith("---") or line.startswith("+++"):
                continue  # Skip file headers
            elif in_hunk:
                escaped_line = html.escape(line.rstrip("\n\r"))
                if line.startswith("+"):
                    new_line_num += 1
                    html_parts.append(
                        f'<tr class="diff-add"><td class="line-num"></td><td class="line-num">{new_line_num}</td><td class="line-content" colspan="2">{escaped_line}</td></tr>'
                    )
                elif line.startswith("-"):
                    old_line_num += 1
                    html_parts.append(
                        f'<tr class="diff-del"><td class="line-num">{old_line_num}</td><td class="line-num"></td><td class="line-content" colspan="2">{escaped_line}</td></tr>'
                    )
                else:
                    old_line_num += 1
                    new_line_num += 1
                    html_parts.append(
                        f'<tr class="diff-unchanged"><td class="line-num">{old_line_num}</td><td class="line-num">{new_line_num}</td><td class="line-content" colspan="2">{escaped_line}</td></tr>'
                    )

        html_parts.append("</table>")
        html_parts.append("</div>")

    # Image diffs section - only show images that are different
    changed_images = [x for x in (image_diffs or []) if not x[3]]  # x[3] is is_same
    if changed_images:
        html_parts.append('<h2 style="margin-top:30px;">Image Changes</h2>')
        for img_name, old_path, new_path, is_same in changed_images:
            html_parts.append('<div class="image-diff">')
            html_parts.append(
                f'<div class="image-panel"><h3>v{old_version}: {html.escape(img_name)}</h3>'
            )
            if old_path:
                html_parts.append(
                    f'<img src="{html.escape(old_path)}" alt="Old version">'
                )
            else:
                html_parts.append(
                    '<p style="padding:20px;color:#656d76;">Image not present in this version</p>'
                )
            html_parts.append("</div>")
            html_parts.append(
                f'<div class="image-panel"><h3>v{new_version}: {html.escape(img_name)}</h3>'
            )
            if new_path:
                html_parts.append(
                    f'<img src="{html.escape(new_path)}" alt="New version">'
                )
            else:
                html_parts.append(
                    '<p style="padding:20px;color:#656d76;">Image not present in this version</p>'
                )
            html_parts.append("</div>")
            html_parts.append("</div>")

    html_parts.append("</body>")
    html_parts.append("</html>")

    return "\n".join(html_parts)


def compute_file_hash(content: bytes) -> str:
    """Compute SHA256 hash of file content."""
    return hashlib.sha256(content).hexdigest()


def generate_version_diffs(
    version: str,
    prev_version: str,
    current_docs: dict[str, str],
    prev_docs: dict[str, str],
    current_images: dict[str, bytes],
    prev_images: dict[str, bytes],
    output_dir: Path,
) -> list[tuple[str, str, str]]:
    """
    Generate diff pages for documents that exist in both versions.

    Args:
        version: Current version
        prev_version: Previous version
        current_docs: Dict of dest_path -> content for current version
        prev_docs: Dict of dest_path -> content for previous version
        current_images: Dict of image_path -> content for current version
        prev_images: Dict of image_path -> content for previous version
        output_dir: Output directory for diff files

    Returns:
        List of (title, dest_path, diff_filename) for documents that were diffed
    """
    diff_dir = output_dir / "diffs"
    diff_dir.mkdir(parents=True, exist_ok=True)

    diff_entries = []

    # Find documents present in both versions
    common_docs = set(current_docs.keys()) & set(prev_docs.keys())

    # Build title mapping from DOC_STRUCTURE for better titles
    title_map = {}
    for entry in DOC_STRUCTURE:
        title_map[entry.dest_path] = entry.title

    # Define category order to match TOC structure
    def get_category_order(path: str) -> tuple[int, str]:
        """Return sort key matching TOC order."""
        if "overview" in path.lower() and "subsystem/" not in path:
            return (0, path)
        elif path in ("caliptra_spec.md", "lock_spec.md"):
            return (1, path)
        elif path.startswith("hardware/"):
            return (2, path)
        elif path.startswith("subsystem/"):
            return (3, path)
        elif path.startswith("firmware/"):
            return (4, path)
        elif path.startswith("mcu/"):
            return (5, path)
        else:
            return (6, path)

    for dest_path in sorted(common_docs, key=get_category_order):
        current_content = current_docs[dest_path]
        prev_content = prev_docs[dest_path]

        # Get title from DOC_STRUCTURE mapping, fallback to first heading or filename
        if dest_path in title_map:
            title = title_map[dest_path]
        else:
            title_match = re.search(r"^#\s+(.+)$", current_content, re.MULTILINE)
            title = (
                title_match.group(1)
                if title_match
                else Path(dest_path).stem.replace("_", " ").title()
            )

        # Find images referenced in both versions
        current_img_refs = set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", current_content))
        prev_img_refs = set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", prev_content))
        all_img_refs = current_img_refs | prev_img_refs

        # Compare images - normalize paths for lookup
        image_diffs = []
        for img_ref in sorted(all_img_refs):
            # Normalize image path - strip leading ./ and handle relative paths
            normalized_ref = img_ref.lstrip("./")
            img_name = Path(img_ref).name

            # Try to find image in our collected images
            current_img = current_images.get(normalized_ref)
            prev_img = prev_images.get(normalized_ref)

            # Also try with the raw ref
            if current_img is None:
                current_img = current_images.get(img_ref)
            if prev_img is None:
                prev_img = prev_images.get(img_ref)

            # Skip images that couldn't be found in either version
            if current_img is None and prev_img is None:
                continue

            if current_img and prev_img:
                is_same = compute_file_hash(current_img) == compute_file_hash(prev_img)
            else:
                is_same = False

            # Paths relative to diff file location - use normalized path
            current_path = f"../{normalized_ref}" if current_img else None
            prev_path = f"../../{prev_version}/{normalized_ref}" if prev_img else None

            image_diffs.append((img_name, prev_path, current_path, is_same))

        # Skip if content is identical
        if prev_content == current_content:
            continue

        # Generate diff HTML
        diff_html = generate_diff_html(
            prev_content,
            current_content,
            prev_version,
            version,
            title,
            image_diffs if image_diffs else None,
        )

        # Write diff file
        safe_name = dest_path.replace("/", "_").replace(".md", "")
        diff_filename = f"diffs/diff_{safe_name}.html"
        diff_file = output_dir / f"diffs/diff_{safe_name}.html"
        diff_file.write_text(diff_html)

        diff_entries.append((title, dest_path, diff_filename))

    # Special case: cross-format comparisons between related documents
    # These have different dest_paths but represent the same conceptual document
    cross_format_pairs = []
    if version == "2.1" and prev_version == "2.0":
        # 2.0 OCP spec vs 2.1 markdown overview
        cross_format_pairs.append(
            (
                "caliptra_spec.md",
                "caliptra_overview.md",
                "Caliptra Spec: OCP (2.0) → Markdown (2.1)",
            )
        )
    elif version == "2.0" and prev_version == "1.2":
        # 1.2 markdown overview vs 2.0 OCP spec
        cross_format_pairs.append(
            (
                "caliptra_overview.md",
                "caliptra_spec.md",
                "Caliptra Spec: Markdown (1.2) → OCP (2.0)",
            )
        )

    for old_path, new_path, cross_title in cross_format_pairs:
        if old_path in prev_docs and new_path in current_docs:
            cross_diff_html = generate_diff_html(
                prev_docs[old_path],
                current_docs[new_path],
                prev_version,
                version,
                cross_title,
                None,  # Skip image comparison for cross-format
            )
            cross_safe_name = f"cross_{old_path.replace('/', '_').replace('.md', '')}_to_{new_path.replace('/', '_').replace('.md', '')}"
            cross_diff_file = output_dir / f"diffs/diff_{cross_safe_name}.html"
            cross_diff_file.write_text(cross_diff_html)
            # Use special marker for categorization
            diff_entries.append(
                (cross_title, "CROSS_FORMAT_SPEC", f"diffs/diff_{cross_safe_name}.html")
            )

    return diff_entries


# =============================================================================
# Additional Resources Page Generation
# =============================================================================


def generate_additional_resources_page(
    version: str,
    commits: dict[str, str],
    doc_entries: list[DocEntry],
) -> str:
    """
    Generate an Additional Resources markdown page linking to documents from
    caliptra-docs.json that are not already rendered in the mdbook.

    Args:
        version: Caliptra version string (e.g., "2.0")
        commits: Dict mapping repo keys to commit SHAs or refs
        doc_entries: List of DocEntry objects already included in the mdbook

    Returns:
        Markdown content for the additional resources page
    """
    # Collect source paths already rendered in the mdbook
    rendered_paths: set[tuple[str, str]] = set()
    for entry in doc_entries:
        rendered_paths.add((entry.source_repo, entry.source_path))

    # Load caliptra-docs.json
    try:
        docs_data = json.loads(CALIPTRA_DOCS_JSON_PATH.read_text())
    except Exception as e:
        logger.warning(
            f"Could not load caliptra-docs.json from {CALIPTRA_DOCS_JSON_PATH}: {e}"
        )
        return (
            "# Additional Resources\n\n" "*Could not load additional resources list.*\n"
        )

    repositories = docs_data.get("repositories", {})
    documents = docs_data.get("documents", [])

    # Group documents by category, filtering out already-rendered ones
    categories: dict[str, list[dict]] = {}
    for doc in documents:
        repo_key = doc.get("repo", "")
        doc_path = doc.get("doc", "")

        # Skip documents already rendered in the mdbook
        if repo_key and doc_path and (repo_key, doc_path) in rendered_paths:
            continue

        category = doc.get("category", "Other")
        categories.setdefault(category, []).append(doc)

    # Build the markdown page
    lines = [
        "# Additional Resources\n",
        "",
        "The following documents are available across the Caliptra project repositories.",
        "Links point to the version-appropriate reference when available.",
        "",
    ]

    # Desired category order
    category_order = [
        "Hardware",
        "Software",
        "Integration",
        "Release Notes",
        "Tools",
        "Governance",
    ]
    # Add any categories not in the predefined order
    for cat in categories:
        if cat not in category_order:
            category_order.append(cat)

    for category in category_order:
        docs_in_cat = categories.get(category)
        if not docs_in_cat:
            continue

        lines.append(f"## {category}\n")
        lines.append("")

        for doc in docs_in_cat:
            name = doc.get("name", "Untitled")
            summary = doc.get("summary", "")
            repo_key = doc.get("repo", "")
            href = doc.get("href")
            doc_path = doc.get("doc", "")

            # Determine the link URL
            if href:
                # External link — use as-is
                url = href
            elif repo_key and doc_path:
                # Build a GitHub blob URL using the version-appropriate ref
                github_repo = GITHUB_REPOS.get(repo_key, "")
                ref = commits.get(repo_key)
                if not ref:
                    # Fall back to the default ref from the JSON
                    repo_info = repositories.get(repo_key, {})
                    ref = repo_info.get("versions", {}).get("default", "main")
                url = f"https://github.com/{github_repo}/blob/{ref}/{doc_path}"
            else:
                continue

            # Format the entry
            summary_text = f" — {summary}" if summary else ""
            lines.append(f"- [{name}]({url}){summary_text}")

        lines.append("")

    return "\n".join(lines)


# =============================================================================
# SUMMARY.md Generation
# =============================================================================


def generate_summary(
    version: str,
    doc_entries: list[DocEntry],
    mcu_entries: Optional[list[DocEntry]] = None,
    diff_entries: Optional[list[tuple[str, str, str]]] = None,
    prev_version: Optional[str] = None,
    version_commits: Optional[dict[str, str]] = None,
) -> str:
    """
    Generate SUMMARY.md content for a specific version.

    Args:
        version: Caliptra version string
        doc_entries: List of main documentation entries
        mcu_entries: Optional list of MCU documentation entries (2.0+)
        diff_entries: Optional list of (title, dest_path, diff_filename) for diffs
        prev_version: Previous version for diff section title
        version_commits: Optional dict of repo -> branch/commit for external links

    Returns:
        SUMMARY.md content
    """
    lines = ["# Summary\n"]
    lines.append("")
    lines.append("# Overview")

    # Group entries by category
    # Note: subsystem entries should not appear in overview even if they have "overview" in the name
    overview_entries = [
        e
        for e in doc_entries
        if "overview" in e.dest_path.lower() and "subsystem/" not in e.dest_path
    ]
    hardware_entries = [e for e in doc_entries if "hardware/" in e.dest_path]
    subsystem_entries = [e for e in doc_entries if "subsystem/" in e.dest_path]
    firmware_entries = [e for e in doc_entries if "firmware/" in e.dest_path]
    # OCP spec entries (caliptra_spec.md, lock_spec.md)
    ocp_spec_entries = [
        e for e in doc_entries if e.dest_path in ("caliptra_spec.md", "lock_spec.md")
    ]

    # Overview section
    for entry in overview_entries:
        if version_applies(version, entry.min_version, entry.max_version):
            lines.append(f"- [{entry.title}](./{entry.dest_path})")

    # OCP Specifications section (2.0+)
    applicable_ocp = [
        e
        for e in ocp_spec_entries
        if version_applies(version, e.min_version, e.max_version)
    ]
    if applicable_ocp:
        lines.append("")
        lines.append("# OCP Specifications")
        for entry in applicable_ocp:
            lines.append(f"- [{entry.title}](./{entry.dest_path})")

    # Hardware section
    if hardware_entries:
        lines.append("")
        lines.append("# Core Hardware Specifications")
        # Separate Adams Bridge sub-docs from other hardware entries
        ab_sub_entries = [
            e for e in hardware_entries if "adams_bridge_ml" in e.dest_path
        ]
        other_hw_entries = [
            e for e in hardware_entries if "adams_bridge_ml" not in e.dest_path
        ]
        for entry in other_hw_entries:
            if version_applies(version, entry.min_version, entry.max_version):
                lines.append(f"- [{entry.title}](./{entry.dest_path})")
                # Nest ML-DSA/ML-KEM under Adams Bridge spec
                if entry.dest_path.endswith("adams_bridge_spec.md"):
                    for sub in ab_sub_entries:
                        if version_applies(version, sub.min_version, sub.max_version):
                            lines.append(f"    - [{sub.title}](./{sub.dest_path})")
        # Add link to register documentation (not available for 1.0)
        if version != "1.0":
            lines.append(f"- [Core Registers ↗](./hardware/registers.md)")

    # Subsystem section (2.0+ only)
    # Separate I3C entries from other subsystem entries
    i3c_entries = [e for e in subsystem_entries if "i3c_" in e.dest_path]
    other_ss_entries = [e for e in subsystem_entries if "i3c_" not in e.dest_path]
    applicable_ss = [
        e
        for e in other_ss_entries
        if version_applies(version, e.min_version, e.max_version)
    ]
    applicable_i3c = [
        e for e in i3c_entries if version_applies(version, e.min_version, e.max_version)
    ]
    if applicable_ss or applicable_i3c:
        lines.append("")
        lines.append("# Subsystem Hardware Specifications")
        for entry in applicable_ss:
            lines.append(f"- [{entry.title}](./{entry.dest_path})")
        # Add link to subsystem register documentation (2.0+ only)
        if version_compare(version, "2.0") >= 0:
            lines.append(f"- [Subsystem Registers ↗](./subsystem/registers.md)")
        # I3C Core as a nested section
        if applicable_i3c:
            # First entry is the main I3C spec (introduction)
            main_i3c = [
                e for e in applicable_i3c if e.dest_path.endswith("i3c_spec.md")
            ]
            sub_i3c = [
                e for e in applicable_i3c if not e.dest_path.endswith("i3c_spec.md")
            ]
            for entry in main_i3c:
                lines.append(f"- [{entry.title}](./{entry.dest_path})")
            for entry in sub_i3c:
                lines.append(f"    - [{entry.title}](./{entry.dest_path})")

    # Firmware section
    if firmware_entries:
        lines.append("")
        lines.append("# Core Firmware Specifications")
        for entry in firmware_entries:
            if version_applies(version, entry.min_version, entry.max_version):
                lines.append(f"- [{entry.title}](./{entry.dest_path})")

    # MCU section (2.0+ only)
    if mcu_entries and version_compare(version, "2.0") >= 0:
        lines.append("")
        lines.append("# MCU Firmware Specifications")
        for entry in mcu_entries:
            indent = "    " * entry.children[0] if entry.children else ""
            lines.append(f"{indent}- [{entry.title}](./{entry.dest_path})")

    # Version diff section
    if diff_entries and prev_version:
        lines.append("")
        lines.append(f"# Changes from {prev_version}")
        for title, dest_path, diff_filename in diff_entries:
            lines.append(f"- [{title}](./{diff_filename})")

    # Additional Resources section (always at the bottom)
    lines.append("")
    lines.append("# Reference")
    lines.append("- [Additional Resources](./additional_resources.md)")

    return "\n".join(lines) + "\n"


def parse_summary_md(content: str) -> list[DocEntry]:
    """
    Parse a SUMMARY.md file to extract document structure.

    Args:
        content: SUMMARY.md content

    Returns:
        List of DocEntry objects with indent level stored in children[0]
    """
    entries = []

    for line in content.split("\n"):
        match = SUMMARY_ENTRY_PATTERN.match(line)
        if match:
            indent = len(match.group(1))
            title = match.group(2)
            path = match.group(3).lstrip("./")

            # Store indent level in children list as a hack
            entry = DocEntry(
                title=title,
                source_repo="caliptra-mcu-sw",
                source_path=path,
                dest_path=f"mcu/{path}",
            )
            entry.children = [indent // 4]  # Store indent level
            entries.append(entry)

    return entries


# =============================================================================
# MCU mdbook Merge
# =============================================================================


def merge_mcu_mdbook(
    commit: str, dest_dir: Path, dry_run: bool = False
) -> list[DocEntry]:
    """
    Fetch and merge caliptra-mcu-sw docs into unified mdbook.

    Args:
        commit: Commit to fetch from
        dest_dir: Destination mdbook directory
        dry_run: If True, don't actually fetch/write files

    Returns:
        List of DocEntry objects for SUMMARY.md generation
    """
    logger.info("Merging MCU firmware documentation...")

    base_url = f"https://raw.githubusercontent.com/{GITHUB_REPOS['caliptra-mcu-sw']}/{commit}/docs"

    # Fetch SUMMARY.md to discover all files
    if dry_run:
        logger.info("  [DRY RUN] Would fetch MCU SUMMARY.md")
        return []

    try:
        summary_content = fetch_url(f"{base_url}/src/SUMMARY.md")
    except FetchError as e:
        logger.warning(f"Could not fetch MCU SUMMARY.md: {e}")
        return []

    # Parse SUMMARY.md to get file list and structure
    entries = parse_summary_md(summary_content)
    logger.info(f"  Found {len(entries)} MCU documents")

    # Create MCU directory
    mcu_dir = dest_dir / "src" / "mcu"
    mcu_dir.mkdir(parents=True, exist_ok=True)

    # Fetch each markdown file and track included files
    all_images = []
    include_files_to_fetch: set[str] = set()  # Track unique include files
    fetched_includes: set[str] = set()  # Track what we've already fetched

    for entry in entries:
        try:
            content = fetch_url(f"{base_url}/src/{entry.source_path}")

            # Find include directives before processing
            includes = find_include_refs(content)
            source_dir = str(Path(entry.source_path).parent)
            for inc_path in includes:
                # Resolve include path relative to the source file
                if source_dir and source_dir != ".":
                    full_inc_path = str(Path(source_dir) / inc_path)
                else:
                    full_inc_path = inc_path
                # Normalize path
                full_inc_path = str(Path(full_inc_path))
                include_files_to_fetch.add(full_inc_path)

            content, images, sphinx_includes = process_document(
                content,
                "caliptra-mcu-sw",
                f"docs/src/{entry.source_path}",
                entry.dest_path,
                commit=commit,
            )
            all_images.extend(images)
            # Note: sphinx_includes not expected in MCU docs, ignore

            # Add source link header
            github_repo = GITHUB_REPOS.get("caliptra-mcu-sw")
            source_path = f"docs/src/{entry.source_path}"
            source_url = f"https://github.com/{github_repo}/blob/{commit}/{source_path}"
            # Only truncate if it's a full SHA (40 hex chars), otherwise show full ref
            is_sha = len(commit) == 40 and all(
                c in "0123456789abcdef" for c in commit.lower()
            )
            display_ref = commit[:7] if is_sha else commit
            source_header = (
                f'<div style="font-size: 0.85em; color: #656d76; margin-bottom: 1em; '
                f'padding: 0.5em; background: #f6f8fa; border-radius: 4px;">\n'
                f'📄 Source: <a href="{source_url}" target="_blank">{github_repo}/{source_path}</a> '
                f"@ <code>{display_ref}</code>\n"
                f"</div>\n\n"
            )
            content = source_header + content

            # Write file
            dest_file = dest_dir / "src" / entry.dest_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            dest_file.write_text(content)
            logger.debug(f"  Written: {entry.dest_path}")

        except FetchError as e:
            logger.warning(f"  Could not fetch {entry.source_path}: {e}")

    # Fetch included files (files referenced by {{#include ...}})
    if include_files_to_fetch:
        logger.info(f"  Fetching {len(include_files_to_fetch)} included files...")
        for inc_path in include_files_to_fetch:
            if inc_path in fetched_includes:
                continue
            try:
                inc_content = fetch_url(f"{base_url}/src/{inc_path}")
                dest_inc = dest_dir / "src" / "mcu" / inc_path
                dest_inc.parent.mkdir(parents=True, exist_ok=True)
                dest_inc.write_text(inc_content)
                fetched_includes.add(inc_path)
                logger.debug(f"  Include: {inc_path}")
            except FetchError:
                logger.warning(f"  Could not fetch include: {inc_path}")

    # Fetch images
    for repo, img_path in all_images:
        try:
            # img_path already includes full path from repo root (e.g., docs/src/images/foo.svg)
            img_content = fetch_binary(repo, commit, img_path)
            # Keep full path to match the rewritten markdown references
            dest_img = dest_dir / "src" / "images" / repo / img_path
            dest_img.parent.mkdir(parents=True, exist_ok=True)
            dest_img.write_bytes(img_content)
            # Optimize PNG images with pngcrush
            if img_path.lower().endswith(".png"):
                try:
                    crushed = dest_img.with_suffix(".crushed.png")
                    result = subprocess.run(
                        ["pngcrush", "-q", str(dest_img), str(crushed)],
                        capture_output=True,
                        timeout=30,
                    )
                    if result.returncode == 0 and crushed.exists():
                        crushed.replace(dest_img)
                    elif crushed.exists():
                        crushed.unlink()
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass  # pngcrush not available or timed out
            logger.debug(f"  Image: {img_path}")
        except FetchError:
            logger.warning(f"  Could not fetch image: {img_path}")

    return entries


# =============================================================================
# Build Functions
# =============================================================================


def generate_book_toml(version: str) -> str:
    """Generate book.toml content for a version."""
    return f"""# Auto-generated by build_docs.py
[book]
authors = ["Caliptra Authors"]
language = "en"
multilingual = false
src = "src"
title = "Caliptra {version} Documentation"

[preprocessor.mermaid]
command = "mdbook-mermaid"

[preprocessor.plantuml]
plantuml-cmd = "java -jar ./{PLANTUML_JAR}"

[output.html]
additional-js = ["mermaid.min.js", "mermaid-init.js", "pagetoc.js"]
additional-css = ["pagetoc.css"]
default-theme = "light"
git-repository-url = "https://github.com/chipsalliance/Caliptra"
mathjax-support = true

[output.html.search]
enable = true
limit-results = 20
use-hierarchical-titles = true
"""


def generate_version_index(versions: list[str], output_dir: Path) -> None:
    """Generate index.html linking to all version builds and diffs."""
    # Check which version pairs have diffs available
    diff_links = {}
    for new_ver, old_ver in VERSION_PAIRS:
        if new_ver in versions and old_ver in versions:
            diff_index = output_dir / new_ver / "diffs" / "index.html"
            if diff_index.exists():
                diff_links[new_ver] = (old_ver, f"./{new_ver}/diffs/index.html")

    html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Caliptra Documentation</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            max-width: 800px;
            margin: 50px auto;
            padding: 20px;
            background: #fafafa;
        }
        h1 { color: #333; }
        h2 { color: #555; margin-top: 40px; }
        p { color: #666; }
        .version-list { list-style: none; padding: 0; }
        .version-list li { margin: 10px 0; }
        .version-list a {
            display: block;
            padding: 15px 20px;
            background: #fff;
            text-decoration: none;
            color: #333;
            border-radius: 5px;
            border: 1px solid #e0e0e0;
            transition: all 0.2s;
        }
        .version-list a:hover {
            background: #f0f0f0;
            border-color: #ccc;
        }
        .latest {
            border-left: 4px solid #4CAF50 !important;
        }
        .version-num { font-weight: bold; }
        .version-label { color: #888; font-size: 0.9em; }
        .diff-list { list-style: none; padding: 0; }
        .diff-list li { margin: 8px 0; }
        .diff-list a {
            display: inline-block;
            padding: 10px 15px;
            background: #fff;
            text-decoration: none;
            color: #0969da;
            border-radius: 5px;
            border: 1px solid #e0e0e0;
            transition: all 0.2s;
        }
        .diff-list a:hover {
            background: #f0f8ff;
            border-color: #0969da;
        }
        .diff-icon { margin-right: 8px; }
    </style>
</head>
<body>
    <h1>Caliptra Documentation</h1>
    <p>Select a documentation version:</p>
    <ul class="version-list">
"""

    sorted_versions = sorted(
        versions, key=lambda v: [int(x) for x in v.split(".")], reverse=True
    )

    for i, version in enumerate(sorted_versions):
        is_latest = version == LATEST_VERSION
        latest_class = ' class="latest"' if is_latest else ""
        label = " (Latest)" if is_latest else ""
        html += f"""        <li>
            <a href="./{version}/index.html"{latest_class}>
                <span class="version-num">Caliptra {version}</span>
                <span class="version-label">{label}</span>
            </a>
        </li>
"""

    html += """    </ul>
"""

    # Add diff section if any diffs exist
    if diff_links:
        html += """
    <h2>Version Comparisons</h2>
    <p>View what changed between versions:</p>
    <ul class="diff-list">
"""
        for new_ver in sorted_versions:
            if new_ver in diff_links:
                old_ver, diff_url = diff_links[new_ver]
                html += f"""        <li>
            <a href="{diff_url}">
                <span class="diff-icon">↔</span>
                Changes from {old_ver} to {new_ver}
            </a>
        </li>
"""
        html += """    </ul>
"""

    html += """    <p style="margin-top: 40px; font-size: 0.85em; color: #999;">
        Generated by <a href="https://github.com/chipsalliance/caliptra-web">Caliptra</a> documentation builder
    </p>
</body>
</html>
"""
    (output_dir / "index.html").write_text(html)
    logger.info(f"Generated version index at {output_dir / 'index.html'}")

    # Create 'latest' redirect to LATEST_VERSION
    if LATEST_VERSION in versions:
        latest_dir = output_dir / "latest"
        latest_dir.mkdir(parents=True, exist_ok=True)

        # Create redirect HTML
        redirect_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="0; url=../{LATEST_VERSION}/index.html">
    <title>Redirecting to Caliptra {LATEST_VERSION}</title>
    <script>window.location.href = "../{LATEST_VERSION}/index.html";</script>
</head>
<body>
    <p>Redirecting to <a href="../{LATEST_VERSION}/index.html">Caliptra {LATEST_VERSION} Documentation</a>...</p>
</body>
</html>
"""
        (latest_dir / "index.html").write_text(redirect_html)
        logger.info(f"Created 'latest' redirect to {LATEST_VERSION}")


def build_version(
    version: str, commits: dict[str, str], output_dir: Path, dry_run: bool = False
) -> bool:
    """
    Build mdbook for a specific Caliptra version.

    Args:
        version: Version string (e.g., "2.0")
        commits: Dict mapping repo keys to commit SHAs or refs
        output_dir: Output directory for built docs
        dry_run: If True, show what would be done without doing it

    Returns:
        True if build succeeded, False otherwise
    """
    logger.info(f"Building Caliptra {version} documentation...")

    if dry_run:
        logger.info(f"  [DRY RUN] Would build version {version}")
        for repo, commit in commits.items():
            if commit:
                logger.info(f"    {repo}: {commit}")
        return True

    # Resolve all refs to actual commit SHAs
    logger.info("  Resolving commit SHAs...")
    resolved_commits = {}
    for repo, ref in commits.items():
        if not ref:
            continue
        try:
            sha = resolve_commit(repo, ref)
            resolved_commits[repo] = sha
            if sha != ref:
                logger.debug(f"    {repo}: {ref} -> {sha[:7]}")
            else:
                logger.debug(f"    {repo}: {sha[:7]}")
        except Exception as e:
            logger.warning(f"    Could not resolve {repo}/{ref}: {e}")
            resolved_commits[repo] = ref
    commits = resolved_commits

    # Create temporary build directory
    with tempfile.TemporaryDirectory() as tmpdir:
        build_dir = Path(tmpdir)
        src_dir = build_dir / "src"
        src_dir.mkdir()
        (src_dir / "images").mkdir()
        (src_dir / "hardware").mkdir()
        (src_dir / "subsystem").mkdir()
        (src_dir / "firmware").mkdir()

        # Track all images to fetch
        all_images: list[tuple[str, str, str]] = []  # (repo, commit, path)
        processed_entries: list[DocEntry] = []

        # 1. Fetch and process all documents
        for entry in DOC_STRUCTURE:
            # Skip if version doesn't apply
            if not version_applies(version, entry.min_version, entry.max_version):
                version_range = []
                if entry.min_version:
                    version_range.append(f">={entry.min_version}")
                if entry.max_version:
                    version_range.append(f"<={entry.max_version}")
                logger.debug(
                    f"  Skipping {entry.title} (requires {' and '.join(version_range)})"
                )
                continue

            # Get commit for this repo
            commit = commits.get(entry.source_repo)
            if not commit:
                logger.debug(
                    f"  Skipping {entry.title} (no commit for {entry.source_repo})"
                )
                continue

            try:
                logger.info(f"  Fetching: {entry.title}")
                content = fetch_document(entry.source_repo, commit, entry.source_path)

                # Load bibliography for OCP files
                bibliography = None
                if entry.source_path.endswith(".ocp"):
                    source_dir = str(Path(entry.source_path).parent)
                    bib_path = f"{source_dir}/bibliography.yaml"
                    bibliography = load_bibliography(
                        entry.source_repo, commit, bib_path
                    )

                content, images, includes = process_document(
                    content,
                    entry.source_repo,
                    entry.source_path,
                    entry.dest_path,
                    bibliography,
                    commit=commit,
                )

                # Fetch and replace include placeholders
                for inc_repo, inc_path in includes:
                    try:
                        inc_commit = commits.get(inc_repo, commit)
                        inc_content = fetch_document(inc_repo, inc_commit, inc_path)
                        placeholder = f"{{{{INCLUDE:{inc_path}}}}}"
                        content = content.replace(placeholder, inc_content)
                    except FetchError as e:
                        logger.warning(f"    Could not fetch include {inc_path}: {e}")
                        placeholder = f"{{{{INCLUDE:{inc_path}}}}}"
                        content = content.replace(
                            placeholder, f"*[Include not available: {inc_path}]*"
                        )

                # Track images with commit info
                for repo, img_path in images:
                    all_images.append((repo, commits.get(repo, commit), img_path))

                # Add source link header
                github_repo = GITHUB_REPOS.get(entry.source_repo, entry.source_repo)
                source_url = f"https://github.com/{github_repo}/blob/{commit}/{entry.source_path}"
                # Only truncate if it's a full SHA (40 hex chars), otherwise show full ref
                is_sha = len(commit) == 40 and all(
                    c in "0123456789abcdef" for c in commit.lower()
                )
                display_ref = commit[:7] if is_sha else commit
                source_header = (
                    f'<div style="font-size: 0.85em; color: #656d76; margin-bottom: 1em; '
                    f'padding: 0.5em; background: #f6f8fa; border-radius: 4px;">\n'
                    f'📄 Source: <a href="{source_url}" target="_blank">{github_repo}/{entry.source_path}</a> '
                    f"@ <code>{display_ref}</code>\n"
                    f"</div>\n\n"
                )
                content = source_header + content

                # Write document
                dest_path = src_dir / entry.dest_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_text(content)

                processed_entries.append(entry)

            except FetchError as e:
                if entry.required:
                    logger.error(f"  Failed to fetch required doc: {e}")
                    return False
                logger.warning(f"  Optional doc unavailable: {e}")

        # 2. Handle MCU mdbook merge (2.0+ only)
        mcu_entries = None
        if version_compare(version, "2.0") >= 0:
            mcu_commit = commits.get("caliptra-mcu-sw")
            if mcu_commit:
                mcu_entries = merge_mcu_mdbook(mcu_commit, build_dir, dry_run)

        # 3. Fetch all images
        logger.info("  Fetching images...")
        for repo, commit, img_path in all_images:
            try:
                img_content = fetch_binary(repo, commit, img_path)
                dest_img = src_dir / "images" / repo / img_path
                dest_img.parent.mkdir(parents=True, exist_ok=True)
                dest_img.write_bytes(img_content)
                # Optimize PNG images with pngcrush
                if img_path.lower().endswith(".png"):
                    try:
                        crushed = dest_img.with_suffix(".crushed.png")
                        result = subprocess.run(
                            ["pngcrush", "-q", str(dest_img), str(crushed)],
                            capture_output=True,
                            timeout=30,
                        )
                        if result.returncode == 0 and crushed.exists():
                            crushed.replace(dest_img)
                        elif crushed.exists():
                            crushed.unlink()
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass  # pngcrush not available or timed out
                logger.debug(f"    Image: {img_path}")
            except FetchError:
                logger.warning(f"    Could not fetch image: {img_path}")

        # 4. Generate SUMMARY.md
        summary = generate_summary(
            version, processed_entries, mcu_entries, version_commits=commits
        )
        (src_dir / "SUMMARY.md").write_text(summary)

        # 4a. Generate Additional Resources page
        logger.info("  Generating additional resources page...")
        additional_resources = generate_additional_resources_page(
            version, commits, processed_entries
        )
        (src_dir / "additional_resources.md").write_text(additional_resources)

        # 4b. Generate register documentation pages with iframes
        # Map Caliptra versions to register doc versions
        reg_version_map = {
            "1.0": None,  # No registers for 1.0
            "1.1": "v1_1",
            "1.2": "v1_1",  # 1.2 uses v1_1 registers
            "2.0": "v2_0",
            "2.1": "v2_1",
        }
        reg_version = reg_version_map.get(version)

        if reg_version:
            # Core registers
            core_regs_url = f"https://chipsalliance.github.io/caliptra-rtl/{reg_version}/internal-regs/"
            (src_dir / "hardware" / "registers.md").write_text(
                f"# Core Registers\n\n"
                f"Redirecting to external documentation...\n\n"
                f"**[Click here if not redirected ↗]({core_regs_url})**\n\n"
                f"<script>window.location.href = '{core_regs_url}';</script>\n"
            )

            # Subsystem registers (2.0+ only)
            if version_compare(version, "2.0") >= 0:
                ss_regs_url = (
                    f"https://chipsalliance.github.io/caliptra-ss/{reg_version}/regs/"
                )
                (src_dir / "subsystem" / "registers.md").write_text(
                    f"# Subsystem Registers\n\n"
                    f"Redirecting to external documentation...\n\n"
                    f"**[Click here if not redirected ↗]({ss_regs_url})**\n\n"
                    f"<script>window.location.href = '{ss_regs_url}';</script>\n"
                )

        # 5. Generate book.toml
        book_toml = generate_book_toml(version)
        (build_dir / "book.toml").write_text(book_toml)

        # 5b. Install theme files for page TOC
        logger.info("  Installing theme files...")
        script_dir = Path(__file__).parent
        theme_src = script_dir / "theme"
        theme_dst = build_dir / "theme"
        theme_dst.mkdir(parents=True, exist_ok=True)
        # Copy index.hbs to theme folder (mdbook uses this as template)
        index_hbs = theme_src / "index.hbs"
        if index_hbs.exists():
            shutil.copy(index_hbs, theme_dst / "index.hbs")
            logger.debug("    Copied index.hbs")
        # Copy JS and CSS to build root (for additional-js/additional-css)
        for asset_file in ["pagetoc.js", "pagetoc.css"]:
            src_file = theme_src / asset_file
            if src_file.exists():
                shutil.copy(src_file, build_dir / asset_file)
                logger.debug(f"    Copied {asset_file}")
            else:
                logger.warning(f"    Theme file not found: {src_file}")

        # 6. Install mermaid assets using mdbook-mermaid
        logger.info("  Installing mermaid assets...")
        try:
            mermaid_result = subprocess.run(
                ["mdbook-mermaid", "install", str(build_dir)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if mermaid_result.returncode != 0:
                logger.warning(
                    f"  mdbook-mermaid install warning: {mermaid_result.stderr}"
                )
        except FileNotFoundError:
            logger.warning(
                "  mdbook-mermaid not found, mermaid diagrams may not render"
            )
        except subprocess.TimeoutExpired:
            logger.warning("  mdbook-mermaid install timed out")

        # 6b. Download PlantUML JAR for diagram rendering
        plantuml_jar_path = build_dir / PLANTUML_JAR
        if not plantuml_jar_path.exists():
            # Check cache directory first
            cached_jar = CACHE_DIR / PLANTUML_JAR if CACHE_DIR else None
            if cached_jar and cached_jar.exists():
                shutil.copy(cached_jar, plantuml_jar_path)
                logger.debug(f"  Copied {PLANTUML_JAR} from cache")
            else:
                logger.info("  Downloading PlantUML JAR...")
                try:
                    jar_content = fetch_url(PLANTUML_URL, binary=True)
                    plantuml_jar_path.write_bytes(jar_content)
                    if cached_jar:
                        cached_jar.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy(plantuml_jar_path, cached_jar)
                    logger.debug(f"  Downloaded {PLANTUML_JAR}")
                except FetchError as e:
                    logger.warning(f"  Could not download PlantUML JAR: {e}")
                    logger.warning("  PlantUML diagrams may not render")

        # 7. Run mdbook build
        version_output = output_dir / version
        version_output.mkdir(parents=True, exist_ok=True)

        logger.info(f"  Running mdbook build...")
        try:
            result = subprocess.run(
                ["mdbook", "build", "--dest-dir", str(version_output)],
                cwd=build_dir,
                capture_output=True,
                text=True,
                timeout=300,  # Longer timeout for PlantUML processing
            )

            if result.returncode != 0:
                logger.error(f"  mdbook build failed:\n{result.stderr}")
                return False

            # Log stderr even on success (may contain warnings)
            if result.stderr:
                logger.debug(f"  mdbook output: {result.stderr}")

            # Verify output was actually created
            html_files = list(version_output.glob("*.html"))
            if not html_files:
                logger.error(f"  mdbook build produced no output in {version_output}")
                logger.error(f"  mdbook stderr: {result.stderr}")
                return False

            # Copy markdown sources to output for diff generation
            md_source_dir = version_output / "_sources"
            md_source_dir.mkdir(parents=True, exist_ok=True)
            for md_file in src_dir.rglob("*.md"):
                rel_path = md_file.relative_to(src_dir)
                dest_md = md_source_dir / rel_path
                dest_md.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(md_file, dest_md)
            logger.debug(f"  Saved markdown sources to {md_source_dir}")

            logger.info(f"  Built successfully: {version_output}")
            return True

        except FileNotFoundError:
            logger.error("  mdbook not found. Install with: cargo install mdbook")
            return False
        except subprocess.TimeoutExpired:
            logger.error("  mdbook build timed out")
            return False


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


def generate_all_version_diffs(output_dir: Path, built_versions: list[str]) -> None:
    """
    Generate diff pages between adjacent versions after all versions are built.

    Args:
        output_dir: Directory containing built version outputs
        built_versions: List of versions that were successfully built
    """
    logger.info("Generating version diffs...")

    for new_version, old_version in VERSION_PAIRS:
        if new_version not in built_versions or old_version not in built_versions:
            logger.debug(
                f"  Skipping diff {new_version} vs {old_version} (not both built)"
            )
            continue

        logger.info(f"  Generating diffs: {new_version} vs {old_version}")

        new_dir = output_dir / new_version
        old_dir = output_dir / old_version

        if not new_dir.exists() or not old_dir.exists():
            logger.warning(f"    Missing directory, skipping")
            continue

        # Clean old diff files before regenerating
        diff_dir = new_dir / "diffs"
        if diff_dir.exists():
            shutil.rmtree(diff_dir)

        # Use saved markdown sources for comparison
        new_src_dir = new_dir / "_sources"
        old_src_dir = old_dir / "_sources"

        if not new_src_dir.exists() or not old_src_dir.exists():
            logger.warning(f"    Missing _sources directory, skipping")
            continue

        # Collect markdown files from both versions
        new_docs = {}
        old_docs = {}
        new_images = {}
        old_images = {}

        # Read markdown source files - collect ALL files from both versions
        for md_file in new_src_dir.rglob("*.md"):
            if md_file.name == "SUMMARY.md":
                continue
            rel_path = md_file.relative_to(new_src_dir)
            dest_path = str(rel_path)
            try:
                new_docs[dest_path] = md_file.read_text()
            except Exception as e:
                logger.debug(f"    Error reading new {rel_path}: {e}")

        for md_file in old_src_dir.rglob("*.md"):
            if md_file.name == "SUMMARY.md":
                continue
            rel_path = md_file.relative_to(old_src_dir)
            dest_path = str(rel_path)
            try:
                old_docs[dest_path] = md_file.read_text()
            except Exception as e:
                logger.debug(f"    Error reading old {rel_path}: {e}")

        # Collect images
        for img_file in new_dir.rglob("*.png"):
            if "_sources" in str(img_file):
                continue
            rel_path = str(img_file.relative_to(new_dir))
            new_images[rel_path] = img_file.read_bytes()

        for img_file in new_dir.rglob("*.jpg"):
            if "_sources" in str(img_file):
                continue
            rel_path = str(img_file.relative_to(new_dir))
            new_images[rel_path] = img_file.read_bytes()

        for img_file in old_dir.rglob("*.png"):
            if "_sources" in str(img_file):
                continue
            rel_path = str(img_file.relative_to(old_dir))
            old_images[rel_path] = img_file.read_bytes()

        for img_file in old_dir.rglob("*.jpg"):
            if "_sources" in str(img_file):
                continue
            rel_path = str(img_file.relative_to(old_dir))
            old_images[rel_path] = img_file.read_bytes()

        # Generate diffs
        diff_entries = generate_version_diffs(
            new_version,
            old_version,
            new_docs,
            old_docs,
            new_images,
            old_images,
            new_dir,
        )

        if diff_entries:
            # Generate diff index page grouped by category
            diff_index_path = new_dir / "diffs" / "index.html"
            diff_index_path.parent.mkdir(parents=True, exist_ok=True)

            # Group entries by category
            categories = {
                "Overview": [],
                "OCP Specifications": [],
                "Core Hardware": [],
                "Subsystem Hardware": [],
                "Core Firmware": [],
                "MCU Firmware": [],
                "Other": [],
            }

            for title, dest_path, diff_filename in diff_entries:
                if dest_path == "CROSS_FORMAT_SPEC":
                    categories["OCP Specifications"].append(
                        (title, dest_path, diff_filename)
                    )
                elif "overview" in dest_path.lower() and "subsystem/" not in dest_path:
                    categories["Overview"].append((title, dest_path, diff_filename))
                elif dest_path in ("caliptra_spec.md", "lock_spec.md"):
                    categories["OCP Specifications"].append(
                        (title, dest_path, diff_filename)
                    )
                elif dest_path.startswith("hardware/"):
                    categories["Core Hardware"].append(
                        (title, dest_path, diff_filename)
                    )
                elif dest_path.startswith("subsystem/"):
                    categories["Subsystem Hardware"].append(
                        (title, dest_path, diff_filename)
                    )
                elif dest_path.startswith("firmware/"):
                    categories["Core Firmware"].append(
                        (title, dest_path, diff_filename)
                    )
                elif dest_path.startswith("mcu/"):
                    categories["MCU Firmware"].append((title, dest_path, diff_filename))
                else:
                    categories["Other"].append((title, dest_path, diff_filename))

            # Build HTML with categorized sections
            diff_index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Changes from {old_version} to {new_version}</title>
{DIFF_CSS}
<style>
.category {{ margin: 20px 0; }}
.category h3 {{ color: #333; border-bottom: 1px solid #d0d7de; padding-bottom: 5px; }}
.category ul {{ list-style: none; padding-left: 0; }}
.category li {{ margin: 8px 0; }}
.category a {{ color: #0969da; text-decoration: none; }}
.category a:hover {{ text-decoration: underline; }}
.back-link {{ margin-bottom: 20px; }}
.back-link a {{ color: #656d76; text-decoration: none; }}
.back-link a:hover {{ color: #0969da; }}
</style>
</head>
<body>
<div class="back-link"><a href="../index.html">← Back to {new_version} Documentation</a></div>
<h1>Changes from {old_version} to {new_version}</h1>
<p>This page shows documents that changed between version {old_version} and {new_version}.</p>
"""
            # Add each non-empty category
            for cat_name in [
                "Overview",
                "OCP Specifications",
                "Core Hardware",
                "Subsystem Hardware",
                "Core Firmware",
                "MCU Firmware",
                "Other",
            ]:
                entries = categories[cat_name]
                if entries:
                    diff_index_html += (
                        f'<div class="category"><h3>{cat_name}</h3><ul>\n'
                    )
                    for title, dest_path, diff_filename in entries:
                        fname = Path(diff_filename).name
                        diff_index_html += (
                            f'<li><a href="{fname}">{html.escape(title)}</a></li>\n'
                        )
                    diff_index_html += "</ul></div>\n"

            diff_index_html += """</body>
</html>"""
            diff_index_path.write_text(diff_index_html)

            logger.info(f"    Generated {len(diff_entries)} diff pages")
        else:
            logger.info(f"    No common documents to diff")


# =============================================================================
# CLI
# =============================================================================


def main() -> int:
    """Main entry point."""
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
    sys.exit(main())
