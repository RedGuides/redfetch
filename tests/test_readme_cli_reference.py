"""Guard against README drift: the Command Line Reference is generated from the
typer app (see cli_reference.py). This fails if README.md is out of date, i.e.
someone changed a command/option in main.py but didn't regenerate the docs.

Fix a failure with:  hatch run dev:gen-docs
"""
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # import the sibling helper

from cli_reference import README, current_block, render_reference


def test_readme_cli_reference_is_current():
    readme = README.read_text(encoding="utf-8")
    block = current_block(readme)
    assert block is not None, (
        "CLI reference markers are missing from README.md. Restore them and run "
        "`hatch run dev:gen-docs`."
    )

    expected = render_reference()
    if block != expected:
        diff = "\n".join(
            difflib.unified_diff(
                block.splitlines(), expected.splitlines(),
                fromfile="README.md (committed)", tofile="generated from main.py",
                lineterm="",
            )
        )
        raise AssertionError(
            "README.md Command Line Reference is stale. Regenerate with "
            "`hatch run dev:gen-docs` (command help lives in src/redfetch/main.py).\n" + diff
        )
