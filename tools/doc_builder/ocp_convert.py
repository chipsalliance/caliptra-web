"""OCP format (.ocp) to GitHub-flavored markdown conversion."""

from __future__ import annotations

import logging
import re

from build_docs import (
    OCP_ANCHOR_PATTERN,
    OCP_BIBREF_PATTERN,
    OCP_CROSSREF_PATTERN,
    OCP_IMG_ATTR_PATTERN,
    OCP_LATEX_COMMANDS,
    FetchError,
    fetch_document,
)

logger = logging.getLogger("build_docs")


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
