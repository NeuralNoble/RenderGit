"""
Core rendering logic — file classification, notebook parsing, HTML generation.
No UI / CLI code lives here.
"""

from __future__ import annotations
import base64
import html
import json
import mimetypes
import os
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_for_filename, TextLexer
import markdown as mdlib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DEFAULT_BYTES   = 50 * 1024
IMAGE_EXTENSIONS    = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}
SVG_EXTENSIONS      = {".svg"}
NOTEBOOK_EXTENSIONS = {".ipynb"}
BINARY_EXTENSIONS   = {
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".ogg", ".flac",
    ".ttf", ".otf", ".eot", ".woff", ".woff2",
    ".so", ".dll", ".dylib", ".class", ".jar", ".exe", ".bin",
}
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown", ".mkd", ".mkdn"}
README_NAMES        = {"readme.md", "readme.txt", "readme.rst", "readme", "readme.markdown"}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Reasons: "ok" | "image" | "svg" | "notebook" | "binary" | "too_large" | "ignored"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RenderDecision:
    include: bool
    reason: str


@dataclass
class FileInfo:
    path: pathlib.Path
    rel: str          # slash-separated, repo-root-relative
    size: int
    decision: RenderDecision


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_cmd(cmd: List[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def git_clone(url: str, dst: str) -> None:
    """Clone repo; raises CalledProcessError with .stderr populated on failure."""
    run_cmd(["git", "clone", "--depth", "1", url, dst])


def git_head_commit(repo_dir: str) -> str:
    try:
        return run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()
    except Exception:
        return "(unknown)"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def bytes_human(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f, i = float(n), 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0; i += 1
    return f"{int(f)} {units[i]}" if i == 0 else f"{f:.1f} {units[i]}"


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def render_markdown(md_text: str) -> str:
    return mdlib.markdown(md_text, extensions=["fenced_code", "tables", "toc"])


def highlight_code(text: str, filename: str, formatter: HtmlFormatter) -> str:
    try:
        lexer = get_lexer_for_filename(filename, stripall=False)
    except Exception:
        lexer = TextLexer(stripall=False)
    return highlight(text, lexer, formatter)


def slugify(s: str) -> str:
    return "".join(c if c.isalnum() or c in {"-", "_"} else "-" for c in s)


def image_to_data_uri(path: pathlib.Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/png"
    with path.open("rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


def b64_to_data_uri(b64_raw: str, mime: str) -> str:
    return f"data:{mime};base64,{b64_raw.replace(chr(10), '').replace(chr(13), '')}"


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

def looks_binary(path: pathlib.Path) -> bool:
    ext = path.suffix.lower()
    if ext in BINARY_EXTENSIONS:
        return True
    if ext in IMAGE_EXTENSIONS | SVG_EXTENSIONS | NOTEBOOK_EXTENSIONS:
        return False
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return True
        chunk.decode("utf-8")
        return False
    except Exception:
        return True


def decide_file(path: pathlib.Path, repo_root: pathlib.Path, max_bytes: int) -> FileInfo:
    rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0

    if "/.git/" in f"/{rel}/" or rel.startswith(".git/"):
        return FileInfo(path, rel, size, RenderDecision(False, "ignored"))

    ext = path.suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        limit = 5 * 1024 * 1024
        return FileInfo(path, rel, size, RenderDecision(size <= limit, "image" if size <= limit else "too_large"))

    if ext in SVG_EXTENSIONS:
        return FileInfo(path, rel, size, RenderDecision(size <= max_bytes, "svg" if size <= max_bytes else "too_large"))

    if ext in NOTEBOOK_EXTENSIONS:
        nb_limit = max(max_bytes * 10, 5 * 1024 * 1024)
        return FileInfo(path, rel, size, RenderDecision(size <= nb_limit, "notebook" if size <= nb_limit else "too_large"))

    if size > max_bytes:
        return FileInfo(path, rel, size, RenderDecision(False, "too_large"))
    if looks_binary(path):
        return FileInfo(path, rel, size, RenderDecision(False, "binary"))

    return FileInfo(path, rel, size, RenderDecision(True, "ok"))


def collect_files(
    repo_root: pathlib.Path,
    max_bytes: int,
    progress_cb: Callable[[str], None] | None = None,
) -> List[FileInfo]:
    """Walk repo and classify every file. Calls progress_cb(rel_path) per file if provided."""
    infos: List[FileInfo] = []
    for p in sorted(repo_root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        fi = decide_file(p, repo_root, max_bytes)
        infos.append(fi)
        if progress_cb:
            progress_cb(fi.rel)
    return infos


# ---------------------------------------------------------------------------
# Directory tree
# ---------------------------------------------------------------------------

def generate_tree_fallback(root: pathlib.Path) -> str:
    lines: List[str] = []

    def walk(d: pathlib.Path, prefix: str = "") -> None:
        entries = sorted([e for e in d.iterdir() if e.name != ".git"],
                         key=lambda e: (not e.is_dir(), e.name.lower()))
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            lines.append(prefix + ("└── " if last else "├── ") + e.name)
            if e.is_dir():
                walk(e, prefix + ("    " if last else "│   "))

    lines.append(root.name)
    walk(root)
    return "\n".join(lines)


def try_tree_command(root: pathlib.Path) -> str:
    try:
        return run_cmd(["tree", "-a", "."], cwd=str(root)).stdout
    except Exception:
        return generate_tree_fallback(root)


# ---------------------------------------------------------------------------
# Jupyter notebook renderer
# ---------------------------------------------------------------------------

def _cell_source(cell: Dict[str, Any]) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else src


def _join_lines(val: Any) -> str:
    if isinstance(val, list):
        return "".join(val)
    return val or ""


def _output_plain_text(output: Dict[str, Any]) -> str:
    text = output.get("text")
    if text is not None:
        return _join_lines(text)
    data = output.get("data", {})
    for key in ("text/plain", "text/html"):
        val = data.get(key)
        if val:
            return _join_lines(val)
    return ""


def _output_image_html(output: Dict[str, Any]) -> str:
    data = output.get("data", {})
    for mime in ("image/png", "image/jpeg", "image/gif", "image/svg+xml"):
        raw = data.get(mime)
        if not raw:
            continue
        b64 = _join_lines(raw)
        if mime == "image/svg+xml":
            return f'<div class="nb-output-img">{b64}</div>'
        return f'<div class="nb-output-img"><img src="{b64_to_data_uri(b64, mime)}" style="max-width:100%;" alt="cell output" /></div>'
    return ""


_LANG_FILE: Dict[str, str] = {
    "python": "script.py", "python3": "script.py",
    "r": "script.r", "julia": "script.jl",
    "javascript": "script.js", "typescript": "script.ts",
    "scala": "script.scala", "ruby": "script.rb",
    "bash": "script.sh", "shell": "script.sh",
}


def render_notebook(path: pathlib.Path, formatter: HtmlFormatter) -> str:
    try:
        nb = json.loads(path.read_bytes())
    except Exception as e:
        raise ValueError(f"Could not parse notebook JSON: {e}") from e

    cells = nb.get("cells", [])
    if not cells:
        return '<p class="nb-empty">Notebook has no cells.</p>'

    lang = nb.get("metadata", {}).get("kernelspec", {}).get("language", "python").lower()
    lang_file = _LANG_FILE.get(lang, f"script.{lang}")
    parts: List[str] = []

    for idx, cell in enumerate(cells):
        cell_type = cell.get("cell_type", "unknown")
        source    = _cell_source(cell)
        inner: List[str] = []

        if cell_type == "markdown":
            if not source.strip():
                continue
            try:
                body = render_markdown(source)
            except Exception as e:
                body = f'<pre class="nb-render-error">Markdown render failed: {html.escape(str(e))}</pre>'
            inner.append(f'<div class="nb-md-body">{body}</div>')
            cell_cls = "nb-cell nb-cell-markdown"

        elif cell_type == "code":
            ec    = cell.get("execution_count") or ""
            label = f"[{ec}]" if ec != "" else "[ ]"

            if source.strip():
                try:
                    code_html = highlight_code(source, lang_file, formatter)
                except Exception:
                    code_html = f"<pre>{html.escape(source)}</pre>"
                inner.append(
                    f'<div class="nb-input">'
                    f'<span class="nb-label nb-label-in">{html.escape(label)}</span>'
                    f'<div class="nb-code">{code_html}</div>'
                    f'</div>'
                )

            for out in cell.get("outputs", []):
                out_type = out.get("output_type", "")

                img = _output_image_html(out)
                if img:
                    inner.append(
                        f'<div class="nb-output">'
                        f'<span class="nb-label nb-label-out">Out {html.escape(str(ec))}</span>'
                        f'{img}</div>'
                    )
                    continue

                if out_type == "error":
                    ename  = html.escape(out.get("ename", "Error"))
                    evalue = html.escape(out.get("evalue", ""))
                    tb     = ANSI_RE.sub("", _join_lines(out.get("traceback", [])))
                    inner.append(
                        f'<div class="nb-output nb-output-error">'
                        f'<span class="nb-label nb-label-err">Error</span>'
                        f'<pre class="nb-traceback"><strong>{ename}: {evalue}</strong>\n{html.escape(tb)}</pre>'
                        f'</div>'
                    )
                    continue

                text = _output_plain_text(out)
                if text:
                    inner.append(
                        f'<div class="nb-output">'
                        f'<span class="nb-label nb-label-out">Out {html.escape(str(ec))}</span>'
                        f'<pre class="nb-out-text">{html.escape(text)}</pre>'
                        f'</div>'
                    )

            if not inner:
                continue
            cell_cls = "nb-cell nb-cell-code"

        else:
            if not source.strip():
                continue
            inner.append(f'<pre class="nb-raw">{html.escape(source)}</pre>')
            cell_cls = "nb-cell nb-cell-raw"

        parts.append(f'<div class="{cell_cls}" data-cell="{idx}">{"".join(inner)}</div>')

    return "\n".join(parts) if parts else '<p class="nb-empty">Notebook has no renderable cells.</p>'


def notebook_to_plain_text(path: pathlib.Path) -> str:
    try:
        nb = json.loads(path.read_bytes())
    except Exception as e:
        return f"[Could not parse notebook: {e}]"

    lang  = nb.get("metadata", {}).get("kernelspec", {}).get("language", "python")
    parts = [f"# Jupyter Notebook  (kernel: {lang})\n"]

    for idx, cell in enumerate(nb.get("cells", [])):
        cell_type = cell.get("cell_type", "unknown")
        source    = _cell_source(cell)

        if cell_type == "markdown":
            if source.strip():
                parts.append(f"\n## [Markdown Cell {idx}]\n{source}")
        elif cell_type == "code":
            ec = cell.get("execution_count") or ""
            parts.append(f"\n## [Code Cell {idx}]  In [{ec}]:\n```{lang}\n{source}\n```")
            for out in cell.get("outputs", []):
                data = out.get("data", {})
                if any(k.startswith("image/") for k in data):
                    parts.append("\n### [Image output — omitted]")
                    continue
                if out.get("output_type") == "error":
                    parts.append(f"\n### Error: {out.get('ename','Error')}: {out.get('evalue','')}")
                else:
                    text = _output_plain_text(out)
                    if text:
                        parts.append(f"\n### Output:\n{text}")
        elif source.strip():
            parts.append(f"\n## [Raw Cell {idx}]\n{source}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CXML for LLM view
# ---------------------------------------------------------------------------

_LLM_REASONS = {"ok", "svg", "notebook"}


def generate_cxml_text(infos: List[FileInfo]) -> str:
    candidates = [i for i in infos if i.decision.include and i.decision.reason in _LLM_REASONS]
    lines = ["<documents>"]
    for idx, fi in enumerate(candidates, 1):
        lines.append(f'<document index="{idx}">')
        lines.append(f"<source>{fi.rel}</source>")
        lines.append("<document_content>")
        try:
            lines.append(notebook_to_plain_text(fi.path) if fi.decision.reason == "notebook" else read_text(fi.path))
        except Exception as e:
            lines.append(f"[Read error: {e}]")
        lines.append("</document_content>")
        lines.append("</document>")
    lines.append("</documents>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sidebar TOC (collapsible directory tree)
# ---------------------------------------------------------------------------

_TOC_ICONS: Dict[str, str] = {"image": "🖼️", "notebook": "📓"}


def build_toc_tree(rendered: List[FileInfo]) -> str:
    tree: Dict = {}
    for fi in rendered:
        parts = fi.rel.split("/")
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(fi)

    def render_node(node: Dict, depth: int = 0) -> str:
        out: List[str] = []
        for key in sorted(k for k in node if k != "__files__"):
            child    = node[key]
            child_id = f"td-{slugify(key)}-{depth}-{abs(id(child)) % 99999}"
            out.append(
                f'<li class="toc-dir">'
                f'<span class="dir-toggle" onclick="toggleDir(\'{child_id}\')">'
                f'<span class="dir-arrow" id="arr-{child_id}">▶</span>'
                f' 📁 {html.escape(key)}'
                f'</span>'
                f'<ul class="toc toc-children" id="{child_id}" style="display:none">'
                + render_node(child, depth + 1)
                + "</ul></li>"
            )
        for fi in node.get("__files__", []):
            anchor = slugify(fi.rel)
            icon   = _TOC_ICONS.get(fi.decision.reason, "📄")
            out.append(
                f'<li><a href="#file-{anchor}">{icon} {html.escape(fi.rel.split("/")[-1])}</a>'
                f' <span class="muted">({bytes_human(fi.size)})</span></li>'
            )
        return "\n".join(out)

    return render_node(tree)


# ---------------------------------------------------------------------------
# Main HTML builder
# ---------------------------------------------------------------------------

def build_html(repo_url: str, repo_dir: pathlib.Path, head_commit: str, infos: List[FileInfo]) -> str:
    formatter    = HtmlFormatter(nowrap=False)
    pygments_css = formatter.get_style_defs(".highlight")

    rendered      = [i for i in infos if i.decision.include]
    skipped_bin   = [i for i in infos if i.decision.reason == "binary"]
    skipped_large = [i for i in infos if i.decision.reason == "too_large"]
    total_files   = len(rendered) + len(skipped_bin) + len(skipped_large)

    # README: shallowest first, then alphabetical
    readme_info: Optional[FileInfo] = None
    for fi in sorted(rendered, key=lambda f: (f.rel.count("/"), f.rel)):
        if fi.rel.split("/")[-1].lower() in README_NAMES:
            readme_info = fi
            break

    tree_text = try_tree_command(repo_dir)
    cxml_text = generate_cxml_text(infos)
    toc_html  = build_toc_tree(rendered)

    readme_html = ""
    if readme_info:
        try:
            text = read_text(readme_info.path)
            ext  = readme_info.path.suffix.lower()
            readme_html = render_markdown(text) if ext in MARKDOWN_EXTENSIONS else f"<pre>{html.escape(text)}</pre>"
        except Exception as e:
            readme_html = f'<pre class="error">Failed to render README: {html.escape(str(e))}</pre>'

    sections: List[str] = []
    for fi in rendered:
        if readme_info and fi.rel == readme_info.rel:
            continue

        anchor = slugify(fi.rel)
        ext    = fi.path.suffix.lower()

        try:
            reason = fi.decision.reason
            if reason == "image":
                uri = image_to_data_uri(fi.path)
                body_html = (
                    f'<div class="img-wrap">'
                    f'<img src="{uri}" alt="{html.escape(fi.rel)}" style="max-width:100%;border-radius:4px;" />'
                    f'</div>'
                )
            elif reason == "notebook":
                try:
                    body_html = f'<div class="nb-container">{render_notebook(fi.path, formatter)}</div>'
                except Exception as e:
                    body_html = (
                        f'<div class="nb-render-warn">⚠️ Notebook render failed — {html.escape(str(e))}. Showing raw JSON.</div>'
                        + f'<div class="highlight">{highlight_code(read_text(fi.path), "data.json", formatter)}</div>'
                    )
            elif ext in MARKDOWN_EXTENSIONS:
                body_html = f'<div class="markdown-body">{render_markdown(read_text(fi.path))}</div>'
            else:
                body_html = f'<div class="highlight">{highlight_code(read_text(fi.path), fi.rel, formatter)}</div>'
        except Exception as e:
            body_html = f'<pre class="error">Render error: {html.escape(str(e))}</pre>'

        sections.append(
            f'<section class="file-section" id="file-{anchor}">\n'
            f'  <h2>{html.escape(fi.rel)} <span class="muted">({bytes_human(fi.size)})</span></h2>\n'
            f'  <div class="file-body">{body_html}</div>\n'
            f'  <div class="back-top"><a href="#top">↑ Back to top</a></div>\n'
            f'</section>\n'
        )

    def render_skip_list(title: str, items: List[FileInfo]) -> str:
        if not items:
            return ""
        lis = "".join(
            f"<li><code>{html.escape(i.rel)}</code> <span class='muted'>({bytes_human(i.size)})</span></li>"
            for i in items
        )
        return f"<details open><summary>{html.escape(title)} ({len(items)})</summary><ul class='skip-list'>{lis}</ul></details>"

    skipped_html = (
        render_skip_list("Skipped binaries", skipped_bin)
        + render_skip_list("Skipped large files", skipped_large)
    )

    readme_tab_btn = (
        '<button class="toggle-btn" id="btn-readme" onclick="showView(\'readme\', this)">📖 README</button>'
        if readme_info else ""
    )
    readme_view_div = (
        f'<div id="view-readme" class="view-pane" style="display:none">'
        f'<article class="markdown-body readme-article">{readme_html}</article>'
        f'</div>'
        if readme_info else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Flattened repo – {html.escape(repo_url)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    margin: 0; padding: 0; line-height: 1.5; background: #fff; color: #1f2328; font-size: 15px;
  }}
  .page {{ display: grid; grid-template-columns: 280px minmax(0,1fr); min-height: 100vh; }}
  #sidebar {{
    position: sticky; top: 0; align-self: start; height: 100vh; overflow-y: auto;
    border-right: 1px solid #d1d9e0; background: #f6f8fa;
  }}
  #sidebar .sidebar-inner {{ padding: 0.75rem 0.5rem; }}
  #sidebar h2 {{
    margin: 0 0 0.5rem 0; font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.06em; color: #57606a; padding: 0 0.25rem; font-weight: 600;
  }}
  main {{ padding: 1.25rem 1.5rem; }}
  .toc {{ list-style: none; padding-left: 0.8rem; margin: 0; }}
  .toc-root {{ padding-left: 0 !important; }}
  .toc li {{ padding: 2px 0; }}
  .toc a {{ text-decoration: none; color: #0969da; font-size: 0.88rem; word-break: break-all; }}
  .toc a:hover {{ text-decoration: underline; }}
  .toc-dir > .dir-toggle {{
    cursor: pointer; font-size: 0.88rem; font-weight: 600; color: #1f2328;
    display: flex; align-items: center; gap: 0.3rem; padding: 3px 0; user-select: none;
  }}
  .toc-dir > .dir-toggle:hover {{ color: #0969da; }}
  .dir-arrow {{ font-size: 0.65rem; color: #57606a; transition: transform 0.15s; display: inline-block; }}
  .dir-arrow.open {{ transform: rotate(90deg); }}
  .toc-children {{ border-left: 1px solid #d1d9e0; margin-left: 0.5rem; }}
  .muted {{ color: #57606a; font-size: 0.82em; font-weight: normal; }}
  .view-toggle {{
    display: flex; gap: 0.4rem; align-items: center;
    margin-bottom: 1.25rem; border-bottom: 1px solid #d1d9e0; padding-bottom: 0.75rem;
  }}
  .toggle-btn {{
    padding: 0.4rem 1rem; border: 1px solid #d1d9e0; background: white;
    cursor: pointer; border-radius: 6px; font-size: 0.92rem; color: #1f2328;
  }}
  .toggle-btn.active {{ background: #0969da; color: white; border-color: #0969da; }}
  .toggle-btn:hover:not(.active) {{ background: #f3f4f6; }}
  .meta {{
    margin-bottom: 1.25rem; padding: 0.75rem 1rem;
    background: #f6f8fa; border: 1px solid #d1d9e0; border-radius: 8px; font-size: 0.95rem;
  }}
  .meta a {{ color: #0969da; }}
  .counts {{ margin-top: 0.25rem; color: #57606a; font-size: 0.88rem; }}
  .file-section {{ padding: 1rem 0; border-top: 1px solid #eaecef; }}
  .file-section h2 {{ margin: 0 0 0.6rem 0; font-size: 1rem; word-break: break-all; }}
  .back-top {{ font-size: 0.88rem; margin-top: 0.5rem; }}
  .back-top a {{ color: #57606a; text-decoration: none; }}
  .back-top a:hover {{ text-decoration: underline; }}
  .skip-list code {{ background: #f6f8fa; padding: 0.1rem 0.3rem; border-radius: 4px; font-size: 0.88rem; }}
  pre, code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
  pre {{
    background: #f6f8fa; padding: 0.85rem 1rem; overflow-x: auto; margin: 0;
    border-radius: 6px; border: 1px solid #d1d9e0; font-size: 0.9rem; line-height: 1.6;
  }}
  .highlight {{ overflow-x: auto; border-radius: 6px; border: 1px solid #d1d9e0; }}
  .highlight pre {{ border: none; border-radius: 6px; font-size: 0.9rem; line-height: 1.6; }}
  .img-wrap {{ padding: 0.5rem 0; }}
  .error {{ color: #b00020; background: #fff3f3; padding: 0.75rem; border-radius: 6px; }}
  .markdown-body h1, .markdown-body h2, .markdown-body h3 {{ border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; margin-top: 1.2em; }}
  .markdown-body img {{ max-width: 100%; }}
  .markdown-body code {{ background: #f6f8fa; padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.9rem; }}
  .markdown-body pre code {{ background: none; padding: 0; }}
  .markdown-body pre {{ font-size: 0.9rem; line-height: 1.6; }}
  .markdown-body table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  .markdown-body th, .markdown-body td {{ border: 1px solid #d1d9e0; padding: 6px 13px; }}
  .markdown-body tr:nth-child(even) {{ background: #f6f8fa; }}
  .readme-article {{ max-width: 860px; }}
  .nb-container {{ display: flex; flex-direction: column; gap: 0.5rem; }}
  .nb-cell {{ border: 1px solid #e1e4e8; border-radius: 6px; overflow: hidden; }}
  .nb-cell-markdown {{ padding: 0.6rem 1rem; background: #fff; border-color: transparent; }}
  .nb-cell-markdown .nb-md-body {{ line-height: 1.65; }}
  .nb-cell-code {{ background: #fff; }}
  .nb-cell-raw {{ background: #fafafa; }}
  .nb-input {{ display: flex; align-items: stretch; }}
  .nb-output {{ display: flex; align-items: stretch; border-top: 1px solid #eaecef; background: #fafbfc; }}
  .nb-output-error {{ background: #fff8f8; }}
  .nb-label {{
    min-width: 54px; padding: 0.55rem 0.5rem 0.55rem 0.25rem;
    font-size: 0.78rem; font-family: ui-monospace, monospace;
    color: #57606a; text-align: right; user-select: none; flex-shrink: 0; border-right: 2px solid #eaecef;
  }}
  .nb-label-in  {{ color: #0969da; border-color: #d2e3fc; }}
  .nb-label-out {{ color: #cf6700; border-color: #fde8c8; }}
  .nb-label-err {{ color: #b00020; border-color: #ffc1c1; }}
  .nb-code {{ flex: 1; overflow-x: auto; min-width: 0; }}
  .nb-code .highlight {{ border: none; border-radius: 0; }}
  .nb-code .highlight pre {{ border-radius: 0; font-size: 0.9rem; line-height: 1.6; padding: 0.55rem 0.85rem; }}
  .nb-out-text {{
    flex: 1; margin: 0; padding: 0.55rem 0.85rem; background: transparent; border: none;
    font-size: 0.9rem; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  }}
  .nb-output-img {{ flex: 1; padding: 0.55rem 0.85rem; }}
  .nb-traceback {{
    flex: 1; margin: 0; padding: 0.55rem 0.85rem; background: transparent; border: none;
    font-size: 0.88rem; line-height: 1.5; color: #b00020; white-space: pre-wrap; word-break: break-word;
  }}
  .nb-raw {{ margin: 0; border-radius: 0; background: #fafafa; font-size: 0.9rem; }}
  .nb-empty {{ color: #57606a; font-style: italic; padding: 1rem; }}
  .nb-render-warn {{
    background: #fff8e1; border: 1px solid #f5c518; border-radius: 6px;
    padding: 0.5rem 0.85rem; font-size: 0.88rem; color: #7a5800; margin-bottom: 0.5rem;
  }}
  #llm-textarea {{
    width: 100%; height: 72vh; resize: vertical;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.9rem; line-height: 1.5; border: 1px solid #d1d9e0; border-radius: 6px; padding: 0.75rem;
  }}
  .copy-btn {{
    margin-bottom: 0.6rem; padding: 0.45rem 1.1rem;
    background: #1f2328; color: #fff; border: none;
    border-radius: 6px; font-size: 0.92rem; cursor: pointer;
    display: inline-flex; align-items: center; gap: 0.4rem;
  }}
  .copy-btn:hover {{ background: #0969da; }}
  .copy-btn.copied {{ background: #1a7f37; }}
  .copy-hint {{ margin-top: 0.5rem; color: #57606a; font-size: 0.88rem; }}
  :target {{ scroll-margin-top: 10px; }}
  {pygments_css}
</style>
</head>
<body>
<a id="top"></a>
<div class="page">
  <nav id="sidebar">
    <div class="sidebar-inner">
      <h2>Contents ({len(rendered)} files)</h2>
      <ul class="toc toc-root">
        <li style="padding-bottom:0.4rem"><a href="#top" style="color:#57606a;font-size:0.85rem">↑ Top</a></li>
        {toc_html}
      </ul>
    </div>
  </nav>
  <main>
    <div class="meta">
      <div><strong>Repository:</strong> <a href="{html.escape(repo_url)}">{html.escape(repo_url)}</a></div>
      <div><small><strong>HEAD:</strong> {html.escape(head_commit)}</small></div>
      <div class="counts">{total_files} total · {len(rendered)} rendered · {len(skipped_bin) + len(skipped_large)} skipped</div>
    </div>
    <div class="view-toggle">
      <strong style="font-size:0.92rem;margin-right:0.25rem">View:</strong>
      <button class="toggle-btn active" id="btn-human" onclick="showView('human', this)">👤 Human</button>
      {readme_tab_btn}
      <button class="toggle-btn" id="btn-llm" onclick="showView('llm', this)">🤖 LLM</button>
    </div>
    <div id="view-human" class="view-pane">
      <section>
        <h2 style="font-size:1rem;margin-bottom:0.5rem">Directory tree</h2>
        <pre>{html.escape(tree_text)}</pre>
      </section>
      <section style="margin-top:1.25rem">
        <h2 style="font-size:1rem;margin-bottom:0.5rem">Skipped items</h2>
        {skipped_html if skipped_html else "<p style='color:#57606a;font-size:0.9rem'>None — everything rendered!</p>"}
      </section>
      {''.join(sections)}
    </div>
    {readme_view_div}
    <div id="view-llm" class="view-pane" style="display:none">
      <h2 style="font-size:1.05rem;margin-top:0">🤖 LLM View — CXML Format</h2>
      <p style="font-size:0.92rem;color:#57606a;margin-top:0">
        Text-only content (images, binaries, and large files excluded). Paste into any LLM for full-repo analysis.
      </p>
      <button class="copy-btn" id="copy-btn" onclick="copyLLMText()">📋 Copy all</button>
      <textarea id="llm-textarea" readonly>{html.escape(cxml_text)}</textarea>
      <div class="copy-hint">
        Or click inside and press <kbd>Ctrl+A</kbd> / <kbd>Cmd+A</kbd> then <kbd>Ctrl+C</kbd> / <kbd>Cmd+C</kbd>.
      </div>
    </div>
  </main>
</div>
<script>
function showView(name, btn) {{
  document.querySelectorAll('.view-pane').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.toggle-btn').forEach(el => el.classList.remove('active'));
  document.getElementById('view-' + name).style.display = 'block';
  btn.classList.add('active');
  if (name === 'llm') {{ const ta = document.getElementById('llm-textarea'); ta.focus(); ta.select(); }}
}}
document.querySelectorAll('#sidebar a[href^="#file-"]').forEach(link => {{
  link.addEventListener('click', function(e) {{
    const humanBtn  = document.getElementById('btn-human');
    const humanPane = document.getElementById('view-human');
    if (humanPane.style.display === 'none') {{
      showView('human', humanBtn);
      e.preventDefault();
      const target = document.querySelector(this.getAttribute('href'));
      if (target) setTimeout(() => target.scrollIntoView({{ behavior: 'smooth', block: 'start' }}), 50);
    }}
  }});
}});
function copyLLMText() {{
  const ta = document.getElementById('llm-textarea'), btn = document.getElementById('copy-btn');
  const reset = () => setTimeout(() => {{ btn.innerHTML = '📋 Copy all'; btn.classList.remove('copied'); }}, 2000);
  navigator.clipboard.writeText(ta.value)
    .then(() => {{ btn.textContent = '✅ Copied!'; btn.classList.add('copied'); reset(); }})
    .catch(() => {{ ta.select(); document.execCommand('copy'); btn.textContent = '✅ Copied!'; btn.classList.add('copied'); reset(); }});
}}
function toggleDir(id) {{
  const el = document.getElementById(id), arr = document.getElementById('arr-' + id);
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  arr.classList.toggle('open', !open);
}}
(function() {{
  const hash = window.location.hash;
  if (!hash) return;
  const target = document.querySelector(hash);
  if (!target) return;
  let node = target;
  while (node) {{
    if (node.classList && node.classList.contains('toc-children')) {{
      node.style.display = 'block';
      const arr = document.getElementById('arr-' + node.id);
      if (arr) arr.classList.add('open');
    }}
    node = node.parentElement;
  }}
}})();
</script>
</body>
</html>
"""