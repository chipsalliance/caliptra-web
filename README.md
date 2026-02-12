# Caliptra Website

Copyright (c) 2025 CHIPS Alliance Authors

Repository contains a website that aggregates resources about the entire Caliptra ecosystem.

## Prerequisites

Before building ensure that Node.js, npm and Python 3 are installed on your system.

To install JavaScript dependencies run from the root of the repository:

```bash
npm install
```

Python dependencies will be installed automatically during the first build.

## Building the website

To build the website run from the root of the repository:

```bash
npm run build
```

Built files will be present in the `dist` directory in the root of the repository.

## Building the documentation

The Documentation tab serves unified [mdbook](https://rust-lang.github.io/mdBook/)
documentation that aggregates specs from across the Caliptra repositories. Building
it requires `mdbook` and `mdbook-mermaid` in addition to Python 3.

To build a single version quickly (recommended for local development):

```bash
python3 tools/build_docs.py --output book --version 2.1
```

To build all versions:

```bash
python3 tools/build_docs.py --output book
```

This will take several minutes. If you are doing local development, you can use
the `--cache .cache` flag to speed up fetching URLs.

The output is written to the `book/` directory. During `npm run dev`, a Vite
middleware automatically serves `book/` at `/caliptra-web/docs/`.

In CI, the docs are built to `book/` and then copied into `dist/docs/` after the
Astro build. The pinned commit SHAs for each repository and version are stored in
[caliptra-docs.json](./src/data/caliptra-docs.json).

## Building the development version

You can also build the development version, that supports hot reloading, by running:

```bash
npm run dev
```

This command also starts the development server and print the URL that you can connect to from your browser to visit the site.

## Modifying the website

### Adding Announcements
Add an entry to [announcements.json](./src/data/caliptra-announcements.json).

### Adding Blog Posts
Commit a new markdown file to the directory [src/data/blog](./src/data/blog). Example: [hello-world](./src/data/blog/hello-world.md).
Blog posts are published and sorted according to the commit date of the markdown source file.
