# Caliptra Website

Copyright (c) 2025 CHIPS Alliance Authors

Repository contains a website that aggregates resources about the entire Caliptra ecosystem.

## Prerequisites

Before building ensure that Node.js, npm and Python 3 are installed on you system.

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

## Building the development version

You can also build the development version, that supports hot reloading, by running:

```bash
npm run dev
```

This command also starts the development server and print the URL that you can connect to from your browser to visit the site.
