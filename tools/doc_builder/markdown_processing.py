"""Markdown content processing: image/link rewriting, Sphinx directives, tables."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from build_docs import (
    GITHUB_REPOS,
    HTML_IMG_PATTERN,
    IMAGE_PATTERN,
    INCLUDE_PATTERN,
    LINK_PATTERN,
    SPHINX_DOC_PATTERN,
    SPHINX_INCLUDE_PATTERN,
)
from doc_builder.ocp_convert import convert_ocp_to_markdown

logger = logging.getLogger("build_docs")


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
