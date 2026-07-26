"""Tests for version message generation."""

from pathlib import Path
from unittest.mock import patch

import pytest

from redfetch import push


CHANGELOG = """\
# Changelog

## [1.1.0] - 2026-07-02

### Added

- Multipath emu sync.

### Fixed

- Header duplication.

## [1.0.0] - 2026-07-01

### Fixed

- First bug.
"""


def _bbcode_stub(markdown_text, domain=None):
    return f"[BBCODE domain={domain}]{markdown_text}"


@pytest.fixture
def changelog_file(tmp_path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(CHANGELOG, encoding="utf-8")
    return p


def test_changelog_entry_for_version(changelog_file):
    with patch("redfetch.push.process_readme", side_effect=_bbcode_stub):
        result = push.generate_version_message(changelog_file, "v1.1.0", domain="https://example.com/")

    assert result.startswith("[BBCODE domain=https://example.com/]")
    assert "### Added" in result
    assert "Multipath emu sync." in result
    assert "First bug." not in result


def test_changelog_missing_version_falls_back_to_whole_file(changelog_file, capsys):
    with patch("redfetch.push.process_readme", side_effect=_bbcode_stub):
        result = push.generate_version_message(changelog_file, "v9.9.9")

    assert "not found" in capsys.readouterr().out
    assert "Multipath emu sync." in result
    assert "First bug." in result


def test_plain_markdown_file_converted_whole(tmp_path):
    md = tmp_path / "notes.md"
    md.write_text("# Release notes\n\nJust some notes.", encoding="utf-8")

    with patch("redfetch.push.process_readme", side_effect=_bbcode_stub):
        result = push.generate_version_message(md, "v1.0.0")

    assert result == "[BBCODE domain=None]# Release notes\n\nJust some notes."


def test_plain_text_file_posted_as_is(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("Raw text notes.", encoding="utf-8")

    with patch("redfetch.push.process_readme", side_effect=_bbcode_stub) as converter:
        result = push.generate_version_message(txt, "v1.0.0")

    assert result == "Raw text notes."
    converter.assert_not_called()


def test_literal_string_message_passes_through():
    assert push.generate_version_message("Small fix.", "v1.0.0") == "Small fix."


def test_literal_message_as_path_from_typer():
    result = push.generate_version_message(Path("Fixed the bug with spaces"), "v1.0.0")
    assert result == "Fixed the bug with spaces"


def test_long_message_truncated():
    long_message = "x" * (push.MAX_MESSAGE_CHARS + 500)
    result = push.generate_version_message(long_message, "v1.0.0")

    assert len(result) <= push.MAX_MESSAGE_CHARS
    assert result.endswith("(truncated)")
