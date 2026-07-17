"""mdbook build orchestration: SUMMARY.md, book.toml, MCU merge, version index."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from build_docs import (
    CALIPTRA_DOCS_JSON_PATH,
    CACHE_DIR,
    DOC_STRUCTURE,
    GITHUB_REPOS,
    LATEST_VERSION,
    PLANTUML_JAR,
    PLANTUML_URL,
    SUMMARY_ENTRY_PATTERN,
    DocEntry,
    FetchError,
    fetch_binary,
    fetch_document,
    fetch_url,
    resolve_commit,
    version_applies,
    version_compare,
)
from doc_builder.diff_generation import VERSION_PAIRS, generate_all_version_diffs
from doc_builder.markdown_processing import (
    find_include_refs,
    process_document,
)
from doc_builder.ocp_convert import load_bibliography

logger = logging.getLogger("build_docs")


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


def generate_dpe_cert_visualizer_page() -> str:
    """Generate markdown page for DPE Certificate & CSR Visualizer WASM tool."""
    return (
        "# DPE Certificate & CSR Visualizer\n\n"
        "The **DPE Certificate & CSR Visualizer** is a 100% client-side WebAssembly (WASM) tool hosted at "
        "[https://chipsalliance.github.io/caliptra-dpe/cert-printer/](https://chipsalliance.github.io/caliptra-dpe/cert-printer/).\n\n"
        "It enables parsing, inspecting, and visualizing DICE/DPE TCB (Trusted Computing Base) context derivation trees, "
        "certificates (X.509/DICE), CSRs, CMS SignedData, and binary DPE response packets directly in your browser with zero server latency. "
        "Because all WebAssembly computation executes locally inside your browser, certificate data and payload details remain strictly private "
        "and are never sent to or stored on any external server.\n\n"
        "## Features\n\n"
        "- **100% Client-Side Privacy**: Operates entirely within your local browser sandbox without transmitting or leaking certificate/CSR details to any server.\n"
        "- **X.509 Certificate & CSR Parsing**: Decodes standard X.509 V3 certificates and PKCS#10 Certificate Signing Requests (CSRs).\n"
        "- **DICE MultiTcbInfo / TcbInfo Extraction**: Parses `2.23.133.5.4.5` (`tcg-dice-MultiTcbInfo`) and `2.23.133.5.4.1` (`tcg-dice-TcbInfo`) ASN.1 structures into TCB context nodes.\n"
        "- **TCB Context Graphing**: Constructs a directed acyclic graph (DAG) representing the DPE TCB context derivation chain using Mermaid.js.\n"
        "- **DPE Profile Inference**: Inspects public key types (ECC P-256, P-384, ML-DSA), signature algorithms, and FWID OIDs (SHA-256 vs. SHA-384) to automatically infer the active DPE profile (e.g. `P256-SHA256`, `P384-SHA384`, `MLDSA-87`).\n"
        "- **Interactive Visualizer**: Supports drag-and-drop file upload (`.der`, `.pem`, binary packets) and built-in sample certificates.\n\n"
        "---\n\n"
        "## Interactive Visualizer\n\n"
        '<div style="margin: 1rem 0;">\n'
        '  <a href="https://chipsalliance.github.io/caliptra-dpe/cert-printer/" target="_blank" rel="noopener noreferrer" style="display: inline-block; padding: 0.6em 1.2em; background-color: #3b82f6; color: white; text-decoration: none; border-radius: 6px; font-weight: 500;">'
        "Open Visualizer in New Tab ↗</a>\n"
        "</div>\n\n"
        '<iframe src="https://chipsalliance.github.io/caliptra-dpe/cert-printer/" style="width: 100%; height: 850px; border: 1px solid #334155; border-radius: 8px; margin-top: 1em;" title="DPE Certificate & CSR Visualizer"></iframe>\n\n'
        "---\n\n"
        "## Source Code & Repository\n\n"
        "The source code and WebAssembly application are maintained in the [`chipsalliance/caliptra-dpe`](https://github.com/chipsalliance/caliptra-dpe) repository.\n"
    )


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

    # Tools & Utilities section
    lines.append("")
    lines.append("# Tools & Utilities")
    lines.append("- [DPE Cert Visualizer](./tools/dpe_cert_visualizer.md)")

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

        # 4c. Generate DPE Cert Visualizer tool page
        logger.info("  Generating DPE Cert Visualizer page...")
        tools_dir = src_dir / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        (tools_dir / "dpe_cert_visualizer.md").write_text(
            generate_dpe_cert_visualizer_page()
        )

        # 5. Generate book.toml
        book_toml = generate_book_toml(version)
        (build_dir / "book.toml").write_text(book_toml)

        # 5b. Install theme files for page TOC
        logger.info("  Installing theme files...")
        script_dir = Path(__file__).parent.parent
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
