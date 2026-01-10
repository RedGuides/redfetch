from unittest.mock import patch

import pytest


class _Args:
    def __init__(self, message, version="v1.0.0", domain=None):
        self.message = message
        self.version = version
        self.domain = domain


def test_generate_version_message_passthrough_string():
    from redfetch import push

    args = _Args("Hello world", version="v1.2.3")
    assert push.generate_version_message(args) == "Hello world"


def test_generate_version_message_md_file_falls_back_when_not_keepachangelog(tmp_path):
    from redfetch import push

    p = tmp_path / "message.md"
    p.write_text("# Release notes\n\n- Added foo\n", encoding="utf-8")

    with patch("redfetch.push.keepachangelog.to_dict", side_effect=Exception("not a changelog")), patch(
        "redfetch.push.convert_markdown_to_bbcode",
        side_effect=lambda s, domain=None: f"BB:{s}",
    ):
        args = _Args(str(p), version="v1.0.0")
        out = push.generate_version_message(args)

    assert out.startswith("BB:")
    assert "- Added foo" in out


def test_generate_version_message_md_file_falls_back_when_keepachangelog_dict_is_empty(tmp_path):
    from redfetch import push

    p = tmp_path / "message.md"
    p.write_text("Just some notes\n", encoding="utf-8")

    with patch("redfetch.push.keepachangelog.to_dict", return_value={}), patch(
        "redfetch.push.convert_markdown_to_bbcode",
        side_effect=lambda s, domain=None: f"BB:{s}",
    ):
        args = _Args(str(p), version="v1.0.0")
        out = push.generate_version_message(args)

    assert out.startswith("BB:")
    assert "Just some notes" in out


def test_generate_version_message_keepachangelog_extracts_version_entry(tmp_path):
    from redfetch import push

    p = tmp_path / "CHANGELOG.md"
    p.write_text("does not matter; parser is mocked\n", encoding="utf-8")

    changes = {
        "1.0.0": {
            "Added": ["One thing", "Another thing"],
            "Fixed": ["A bug"],
            "metadata": {"date": "2026-01-01"},
        }
    }

    # Identity conversion so we can assert on the generated markdown structure.
    with patch("redfetch.push.keepachangelog.to_dict", return_value=changes), patch(
        "redfetch.push.convert_markdown_to_bbcode",
        side_effect=lambda s, domain=None: s,
    ):
        args = _Args(str(p), version="v1.0.0")
        out = push.generate_version_message(args)

    assert "### Added" in out
    assert "- One thing" in out
    assert "- Another thing" in out
    assert "### Fixed" in out
    assert "- A bug" in out
    # metadata should be ignored
    assert "metadata" not in out


def test_generate_version_message_keepachangelog_missing_version_raises(tmp_path):
    from redfetch import push

    p = tmp_path / "CHANGELOG.md"
    p.write_text("# Notes\n\nNot keep-a-changelog (but parser is mocked)\n", encoding="utf-8")

    changes = {"0.1.0": {"Added": ["Nope"], "metadata": {}}}

    with patch("redfetch.push.keepachangelog.to_dict", return_value=changes), patch(
        "redfetch.push.convert_markdown_to_bbcode",
        side_effect=lambda s, domain=None: f"BB:{s}",
    ):
        args = _Args(str(p), version="v1.0.0")
        out = push.generate_version_message(args)

    assert out.startswith("BB:")
    assert "Not keep-a-changelog" in out


def test_generate_version_message_non_md_file_posts_as_plain_text(tmp_path):
    from redfetch import push

    p = tmp_path / "message.txt"
    p.write_text("Plain text message\nLine2\n", encoding="utf-8")

    # Ensure we don't accidentally try to parse as keep-a-changelog.
    with patch("redfetch.push.keepachangelog.to_dict", side_effect=AssertionError("should not be called")):
        args = _Args(str(p), version="v1.0.0")
        out = push.generate_version_message(args)

    assert "Plain text message" in out
    assert "Line2" in out


def test_generate_version_message_truncates_very_large_file(tmp_path):
    from redfetch import push

    p = tmp_path / "message.md"
    p.write_text("x" * 20_000, encoding="utf-8")

    with patch("redfetch.push.keepachangelog.to_dict", side_effect=Exception("not a changelog")), patch(
        "redfetch.push.convert_markdown_to_bbcode",
        side_effect=lambda s, domain=None: s,
    ):
        args = _Args(str(p), version="v1.0.0")
        out = push.generate_version_message(args)

    assert len(out) <= 10_000
    assert "truncated" in out


def test_handle_cli_allows_domain_with_file_only_publish(tmp_path):
    """
    Regression test: --domain is only used for resolving relative URLs when converting markdown.
    It should not make a file-only publish fail when no --message/--description is provided.
    """
    from redfetch import push

    class _Args:
        resource_id = 123
        description = None
        version = "v1.2.3"
        message = None
        file = str(tmp_path / "payload.zip")
        domain = "https://raw.githubusercontent.com/org/repo/main/"

    with patch("redfetch.push.auth.initialize_keyring"), patch("redfetch.push.auth.authorize"), patch(
        "redfetch.push.asyncio.run", return_value={"resource_id": _Args.resource_id}
    ), patch("redfetch.push.add_xf_attachment") as attach:
        push.handle_cli(_Args())

    attach.assert_called_once_with(_Args.resource_id, _Args.file, _Args.version)
