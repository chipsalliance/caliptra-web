#!/usr/bin/env python3

import argparse
import json
import mistletoe
import mistletoe.block_token as tokens
import mistletoe.markdown_renderer as markdown
import re
import sys

from .containers import Section, Table


LINK_REGEX = re.compile(r'\[([^\]]*)\]\(([^\)]*)\)')


def parse_markdown(input_path: str) -> Section:
    top_section = Section('Document', -1)
    current_section = top_section

    with open(input_path, 'rt') as file:
        lines = file.readlines()

    with markdown.MarkdownRenderer() as renderer:
        doc = mistletoe.Document(lines)
        for element in doc.children:
            if isinstance(element, tokens.Heading):
                current_section = Section(
                    name=''.join(renderer.render(x) for x in element.children).rstrip(),
                    level=element.level,
                    parent=Section.find_parent(current_section, element.level)
                )
            elif isinstance(element, tokens.Table):
                header = renderer.table_row_to_text(element.header)
                rows = [renderer.table_row_to_text(row) for row in element.children]
                current_section.content.append(Table(header, rows))
            else:
                content = renderer.render(element).rstrip()
                if len(content) > 0:
                    current_section.content.append(content)

    return top_section


def generate_versioning(top: Section) -> str:
    DOT_REGEX = re.compile(r'\\\*')
    def handle_entry(entry: str) -> str:
        content: list[str] = []
        for line in entry.splitlines():
            line = LINK_REGEX.sub(r'\1', line)
            line = DOT_REGEX.sub(r'*', line)
            content.append(line.strip())
        return '\n'.join(content)

    section_name = 'Versioning'
    versioning = top.find_subsection(section_name)
    return json.dumps({
        'title': 'Caliptra releases',
        'tag': section_name,
        'description': versioning.content[0],
        'versions': [{
            'name': v.name,
            'content': '\n'.join(handle_entry(x) for x in v.content[:-1]),
            'table': v.content[-1].to_dict(),
        } for v in versioning.children]
    }, indent=2)


def generate_repositories(top: Section) -> str:
    def handle_entry(entry: list[str]) -> dict:
        entry_match = LINK_REGEX.match(entry[0])
        result = {
            'name': entry_match.group(1),
            'url': entry_match.group(2),
            'description': entry[-1],
        }

        # Use the full repository path to generate area URLs on the page
        # if the provided URL does not point to the main repository
        tree_index = result['url'].rfind("/tree/")
        if tree_index > 0:
            result['areas_url'] = result['url'][:tree_index]

        return result

    section_name = 'Repositories'
    repositories = top.find_subsection(section_name)
    return json.dumps({
        'title': 'Explore the code behind Caliptra',
        'tag': section_name,
        'description': repositories.content[0],
        'items': [handle_entry(x) for x in repositories.content[1].rows]
    }, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=False,
                        help='Output JSON file')
    parser.add_argument('--section', type=str, choices=['versioning', 'repositories'],
                        help='Data to extract')
    parser.add_argument('input',
                        help='Input Markdown file')

    args = parser.parse_args()
    top = parse_markdown(args.input)
    if args.section == 'versioning':
        result = generate_versioning(top)
    elif args.section == 'repositories':
        result = generate_repositories(top)
    else:
        print(f'Unsupported --section argument: {args.section}', file=sys.stderr)
        sys.exit(1)

    with open(args.output, 'wt') as f:
        f.write(result)


if __name__ == '__main__':
    main()
