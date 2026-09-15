"""Sphinx configuration for the seine documentation."""

from __future__ import annotations

project = "seine"
author = "Cedric Hombourger"
copyright = "2026, Cedric Hombourger"
release = "0.3"

extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
root_doc = "index"
exclude_patterns = ["_build"]

# Give the headings in the existing Markdown pages stable, linkable anchors.
myst_heading_anchors = 4

html_theme = "sphinx_book_theme"
html_title = "seine documentation"
html_theme_options = {
    "repository_url": "https://github.com/chombourger/seine",
    "repository_branch": "master",
    "use_repository_button": True,
    "use_issues_button": True,
}


def rewrite_readme_links(app, relative_path, parent_docname, content):
    """Resolve README links from the documentation source directory."""
    if parent_docname == "introduction" and str(relative_path) == "../README.md":
        content[0] = content[0].replace("](docs/", "](")


def setup(app):
    app.connect("include-read", rewrite_readme_links)
