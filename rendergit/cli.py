"""
rendergit CLI — Rich-powered TUI for rendering GitHub repos to HTML.
"""

from __future__ import annotations
import pathlib
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from typing import Optional

from rich import print as rprint
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, Confirm
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

from rendergit.renderer import (
    MAX_DEFAULT_BYTES,
    FileInfo,
    bytes_human,
    git_clone,
    git_head_commit,
    collect_files,
    build_html,
)

console = Console()


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

BANNER = """[bold white]
  ██████╗ ███████╗███╗   ██╗██████╗ ███████╗██████╗  ██████╗ ██╗████████╗
  ██╔══██╗██╔════╝████╗  ██║██╔══██╗██╔════╝██╔══██╗██╔════╝ ██║╚══██╔══╝
  ██████╔╝█████╗  ██╔██╗ ██║██║  ██║█████╗  ██████╔╝██║  ███╗██║   ██║   
  ██╔══██╗██╔══╝  ██║╚██╗██║██║  ██║██╔══╝  ██╔══██╗██║   ██║██║   ██║   
  ██║  ██║███████╗██║ ╚████║██████╔╝███████╗██║  ██║╚██████╔╝██║   ██║   
  ╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝   ╚═╝[/bold white]"""

TAGLINE = "[dim]Render any GitHub repo into a beautiful, searchable HTML page[/dim]"


def print_banner() -> None:
    console.print(BANNER)
    console.print(f"  {TAGLINE}\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def derive_output_path(repo_url: str) -> pathlib.Path:
    parts = repo_url.rstrip("/").split("/")
    repo_name = (parts[-1] if parts else "repo").removesuffix(".git")
    return pathlib.Path(tempfile.gettempdir()) / f"{repo_name}.html"


def diagnose_clone_error(stderr: str) -> str:
    if "Repository not found" in stderr or "does not exist" in stderr:
        return "Repo not found — check the URL or whether it's private."
    if "Could not resolve host" in stderr or "unable to access" in stderr:
        return "Network error — check your internet connection."
    if "already exists" in stderr:
        return "Destination directory already exists."
    return stderr.strip() or "Unknown git error."


def count_by_reason(infos: list[FileInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fi in infos:
        counts[fi.decision.reason] = counts.get(fi.decision.reason, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Interactive prompt flow
# ---------------------------------------------------------------------------

def prompt_for_options() -> dict:
    console.print(Rule("[bold]Configuration[/bold]", style="dim"))
    console.print()

    repo_url = Prompt.ask(
        "  [bold cyan]Repo URL[/bold cyan]",
        console=console,
    ).strip()

    # Default output path derived from repo name
    default_out = str(derive_output_path(repo_url))
    out_str = Prompt.ask(
        "  [bold cyan]Output path[/bold cyan]",
        default=default_out,
        console=console,
    ).strip()

    max_bytes_kb = Prompt.ask(
        "  [bold cyan]Max file size[/bold cyan] [dim](KiB per file)[/dim]",
        default=str(MAX_DEFAULT_BYTES // 1024),
        console=console,
    ).strip()

    try:
        max_bytes = int(max_bytes_kb) * 1024
    except ValueError:
        console.print("  [yellow]Invalid size, using default 50 KiB[/yellow]")
        max_bytes = MAX_DEFAULT_BYTES

    open_browser = Confirm.ask(
        "  [bold cyan]Open in browser when done?[/bold cyan]",
        default=True,
        console=console,
    )

    console.print()
    return {
        "repo_url":     repo_url,
        "out":          out_str,
        "max_bytes":    max_bytes,
        "open_browser": open_browser,
    }


# ---------------------------------------------------------------------------
# Core run logic (used by both interactive + CLI flag modes)
# ---------------------------------------------------------------------------

def run_render(
    repo_url: str,
    out: str,
    max_bytes: int,
    open_browser: bool,
) -> int:
    tmpdir   = tempfile.mkdtemp(prefix="rendergit_")
    repo_dir = pathlib.Path(tmpdir, "repo")

    try:
        # ── Step 1: Clone ──────────────────────────────────────────────────
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Cloning repository…", total=None)
            try:
                git_clone(repo_url, str(repo_dir))
            except subprocess.CalledProcessError as e:
                progress.stop()
                msg = diagnose_clone_error(e.stderr or e.output or "")
                console.print(f"\n  [bold red]✗  Clone failed:[/bold red] {msg}")
                return 1

        head = git_head_commit(str(repo_dir))
        console.print(f"  [green]✓[/green]  Cloned  [dim]HEAD {head[:8]}[/dim]")

        # ── Step 2: Scan files ─────────────────────────────────────────────
        scanned: list[str] = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Scanning files…", total=None)
            infos = collect_files(repo_dir, max_bytes, progress_cb=lambda rel: scanned.append(rel))

        counts    = count_by_reason(infos)
        rendered  = sum(1 for i in infos if i.decision.include)
        notebooks = counts.get("notebook", 0)
        images    = counts.get("image", 0)
        nb_note   = f" · [cyan]{notebooks} notebook{'s' if notebooks != 1 else ''}[/cyan]" if notebooks else ""
        img_note  = f" · [cyan]{images} image{'s' if images != 1 else ''}[/cyan]" if images else ""
        console.print(
            f"  [green]✓[/green]  Scanned  "
            f"[bold]{len(infos)}[/bold] files · "
            f"[bold]{rendered}[/bold] rendering"
            f"{nb_note}{img_note}"
        )

        # ── Step 3: Build HTML ─────────────────────────────────────────────
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Rendering HTML…", total=None)
            html_out = build_html(repo_url, repo_dir, head, infos)

        out_path = pathlib.Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_out, encoding="utf-8")
        size = bytes_human(out_path.stat().st_size)
        console.print(f"  [green]✓[/green]  Written  [bold]{size}[/bold]  →  [dim]{out_path.resolve()}[/dim]")

        # ── Summary table ──────────────────────────────────────────────────
        console.print()
        table = Table(box=box.ROUNDED, show_header=False, border_style="dim", padding=(0, 1))
        table.add_column(style="dim", width=16)
        table.add_column()

        skipped_bin   = counts.get("binary", 0)
        skipped_large = counts.get("too_large", 0)

        table.add_row("Rendered",       f"[green]{rendered}[/green] files")
        if notebooks:
            table.add_row("Notebooks",  f"[cyan]{notebooks}[/cyan] rendered")
        if images:
            table.add_row("Images",     f"[cyan]{images}[/cyan] embedded inline")
        if skipped_bin:
            table.add_row("Binaries",   f"[yellow]{skipped_bin}[/yellow] skipped")
        if skipped_large:
            table.add_row("Too large",  f"[yellow]{skipped_large}[/yellow] skipped")
        table.add_row("Output",         f"[bold]{out_path.resolve()}[/bold]")
        table.add_row("Size",           size)

        console.print(Panel(table, title="[bold green]✓  Done[/bold green]", border_style="green"))

        # ── Open browser ───────────────────────────────────────────────────
        if open_browser:
            webbrowser.open(f"file://{out_path.resolve()}")

        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    # Parse args — but if only a bare URL is given (or nothing), stay interactive-friendly
    ap = argparse.ArgumentParser(
        prog="rendergit",
        description="Render a GitHub repo into a single HTML page",
        add_help=True,
    )
    ap.add_argument("repo_url", nargs="?", help="GitHub repo URL (omit for interactive mode)")
    ap.add_argument("-o", "--out",       help="Output HTML file path")
    ap.add_argument("--max-bytes",       type=int, default=None,
                                         help=f"Max text-file size in bytes (default {MAX_DEFAULT_BYTES})")
    ap.add_argument("--max-kb",          type=int, default=None,
                                         help="Max text-file size in KiB (overrides --max-bytes)")
    ap.add_argument("--no-open",         action="store_true", help="Don't open browser after render")
    ap.add_argument("--no-banner",       action="store_true", help="Suppress the ASCII banner")
    args = ap.parse_args()

    if not args.no_banner:
        print_banner()

    # ── Interactive mode (no URL given) ────────────────────────────────────
    if not args.repo_url:
        opts = prompt_for_options()
        return run_render(
            repo_url=opts["repo_url"],
            out=opts["out"],
            max_bytes=opts["max_bytes"],
            open_browser=opts["open_browser"],
        )

    # ── Non-interactive / scripted mode ────────────────────────────────────
    max_bytes = MAX_DEFAULT_BYTES
    if args.max_kb is not None:
        max_bytes = args.max_kb * 1024
    elif args.max_bytes is not None:
        max_bytes = args.max_bytes

    out = args.out or str(derive_output_path(args.repo_url))

    console.print()
    return run_render(
        repo_url=args.repo_url,
        out=out,
        max_bytes=max_bytes,
        open_browser=not args.no_open,
    )


if __name__ == "__main__":
    sys.exit(main())