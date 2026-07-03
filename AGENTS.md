# AGENTS.md

**redfetch** — a CLI/TUI for downloading and publishing MacroQuest and EverQuest scripts and software via the RedGuides API.

## Running tests

Use `hatch test` — not `pytest`, `uv`, or an ad-hoc venv

```sh
hatch test                       # full suite
hatch test tests/test_check.py   # single file
```
## Libraries

- [Textual](https://textual.textualize.io/) - TUI
- [Hatch](https://hatch.pypa.io/latest/) - Build system
- [PYPA](https://www.pypa.io/en/latest/specifications/) - Package index

## Conventions

- Use the `dev` environment (`hatch shell dev`) for development
