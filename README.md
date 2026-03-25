# RenderGit

Render any GitHub repo into a single HTML page — paste the entire codebase into any LLM chat with one click. Also works as a beautiful, searchable code viewer with syntax highlighting, rendered Markdown, inline images, and Jupyter notebook support.


![RenderGit CLI](/Users/amananand/Projects/renderer/Screenshot 2026-03-25 at 3.19.55 PM.png)

## Install

Requires [uv](https://docs.astral.sh/uv/) and Git.

```bash
uv tool install git+https://github.com/NeuralNoble/renderer.git
```

This makes the `rendergit` command available globally.

To uninstall:

```bash
uv tool uninstall rendergit
```

## Usage

```bash
# Interactive mode — prompts for URL, output path, file size limit, etc.
rendergit

# Pass a repo URL directly
rendergit https://github.com/NeuralNoble/autoresearch

# Custom output path
rendergit https://github.com/NeuralNoble/autoresearch -o autoresearch.html

# Limit max file size to 100 KiB
rendergit https://github.com/NeuralNoble/autoresearch --max-kb 100

# Don't auto-open in browser
rendergit https://github.com/NeuralNoble/autoresearch --no-open
```

## Flags

| Flag | Description |
|---|---|
| `repo_url` | GitHub repo URL. Omit to enter interactive mode. |
| `-o`, `--out` | Output HTML file path. Defaults to `/tmp/<repo-name>.html`. |
| `--max-kb` | Max text file size in KiB (default: 50). Files larger than this are skipped. |
| `--max-bytes` | Max text file size in bytes. `--max-kb` takes priority if both are set. |
| `--no-open` | Don't open the HTML file in your browser after rendering. |
| `--no-banner` | Suppress the ASCII art banner. |

## What it does

- Shallow-clones the repo, scans every file, and builds a single HTML page
- **Code files** — syntax highlighted with Pygments
- **Markdown / RST** — rendered to HTML with tables, fenced code blocks, and TOC support
- **Images** (png, jpg, gif, webp, svg) — embedded inline as base64 data URIs
- **Jupyter notebooks** — cells rendered with code, markdown, outputs, images, and tracebacks
- **Binaries** — detected and skipped automatically
- **LLM view** — one-click copy of the entire repo as structured text, ready to paste into ChatGPT, Claude, or any LLM chat
- Collapsible directory tree sidebar for navigation
- Everything is self-contained in one `.html` file — no external CSS, JS, or assets

## What the output looks like

The generated HTML has three tabs:

- **Human** — full rendered view with syntax highlighting, a directory tree sidebar, and inline images
- **README** — the project README rendered on its own (if one exists)
- **LLM** — structured text dump of the entire codebase with a copy button — paste it straight into any LLM chat
