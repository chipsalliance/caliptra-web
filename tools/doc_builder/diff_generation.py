"""Version diff HTML generation for Caliptra documentation."""

from __future__ import annotations

import difflib
import hashlib
import html
import logging
import re
import shutil
from pathlib import Path

from build_docs import DOC_STRUCTURE

logger = logging.getLogger("build_docs")

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
