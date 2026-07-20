"""Render the README 'Command Line Reference' from the typer app.

Single source of truth = the help text in ``src/redfetch/main.py``. After
changing a command or option, refresh the docs:

    hatch run dev:gen-docs          # or: python tests/cli_reference.py --write

Run ``hatch run dev:check-docs`` in CI or before release to catch drift.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import typer

from redfetch.main import app

README = Path(__file__).resolve().parent.parent / "README.md"

BEGIN = "<!-- BEGIN GENERATED CLI REFERENCE -->"
END = "<!-- END GENERATED CLI REFERENCE -->"

INTRO = (
    "Run `redfetch --help` for the current list, or `redfetch <COMMAND> --help` "
    "for a command's options. It looks like:"
)

# Rich console markup ([bold], [/green], [bold red], [/]) — stripped from help text.
_MARKUP = re.compile(r"\[/?[a-z0-9 #_]+\]|\[/\]", re.IGNORECASE)


def _markdown(text: str | None) -> str:
    """Rich emphasis -> Markdown (bold/italic), drop other tags; single line."""
    if not text:
        return ""
    text = text.split("\f", 1)[0]        # drop typer long-help after form feed
    text = text.split("\n\n", 1)[0]      # first paragraph only
    text = re.sub(r"\[/?b(?:old)?\]", "**", text)   # [bold]/[b] -> **
    text = re.sub(r"\[/?i(?:talic)?\]", "*", text)  # [italic]/[i] -> *
    text = _MARKUP.sub("", text)                    # remaining tags (colors, [/])
    return " ".join(text.split())


def _arg_metavar(param) -> str:
    return param.metavar or param.human_readable_name


def _is_argument(param) -> bool:
    return type(param).__name__ == "TyperArgument"


def _signature(name: str, params) -> str:
    """Command name plus its positional args: ``config <SETTING_PATH> <VALUE>``."""
    parts = [name]
    for p in params:
        if not _is_argument(p):
            continue
        meta = _arg_metavar(p)
        parts.append(f"<{meta}>" if p.required else f"[{meta}]")
    return " ".join(parts)


def _option_label(param) -> str:
    opts = list(param.opts)
    if not opts:
        return ""
    head = f"{opts[0]} <{param.metavar}>" if param.metavar else opts[0]
    return " / ".join(f"`{o}`" for o in [head, *opts[1:]])


def _param_bullets(params) -> list[str]:
    bullets: list[str] = []
    for p in params:
        if p.name == "help" or getattr(p, "hidden", False):
            continue
        help_text = _markdown(getattr(p, "help", None))
        if not help_text:
            continue
        label = f"`{_arg_metavar(p)}`" if _is_argument(p) else _option_label(p)
        if label:
            bullets.append(f"  - {label} - {help_text}")
    return bullets


def render_reference(typer_app: typer.Typer = app) -> str:
    """Build the blockquoted Command Line Reference, grouped by help panel."""
    group = typer.main.get_command(typer_app)
    panels: dict[str, list[str]] = {}
    for name, cmd in group.commands.items():
        if cmd.hidden:
            continue
        panel = getattr(cmd, "rich_help_panel", None) or "Commands"
        lines = panels.setdefault(panel, [])
        lines.append(f"- `{_signature(name, cmd.params)}` - {_markdown(cmd.help)}")
        lines.extend(_param_bullets(cmd.params))

    out: list[str] = [INTRO, ""]
    for panel, lines in panels.items():
        out.append(f"### {panel}")
        out.extend(lines)
        out.append("")
    out.pop()  # trailing blank

    return "\n".join(f"> {line}" if line else ">" for line in out)


def current_block(readme_text: str) -> str | None:
    """The text currently between the markers, or None if the markers are absent."""
    m = re.search(re.escape(BEGIN) + r"\n(.*)\n" + re.escape(END), readme_text, re.DOTALL)
    return m.group(1) if m else None


def inject(readme_text: str, block: str) -> str:
    pattern = re.compile(re.escape(BEGIN) + r".*" + re.escape(END), re.DOTALL)
    if not pattern.search(readme_text):
        raise SystemExit(f"Markers not found in README.md; add:\n{BEGIN}\n...\n{END}")
    return pattern.sub(lambda _m: f"{BEGIN}\n{block}\n{END}", readme_text)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    block = render_reference()
    text = README.read_text(encoding="utf-8")
    if "--check" in argv:
        if current_block(text) != block:
            print("README CLI reference is stale. Run: hatch run dev:gen-docs")
            return 1
        print("README CLI reference is up to date.")
        return 0
    README.write_text(inject(text, block), encoding="utf-8")
    print(f"Wrote CLI reference to {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
