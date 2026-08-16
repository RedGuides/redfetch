"""Publish resource updates to RedGuides."""

# standard
import asyncio
import os
from pathlib import Path

# third-party
import httpx
import keepachangelog
import typer
from md2bbcode.main import process_readme

# local
from redfetch import api
from redfetch import auth
from redfetch.net import BASE_URL

XF_API_URL = f'{BASE_URL}/api'
URI_MESSAGE = f'{XF_API_URL}/resource-updates'
URI_ATTACHMENT = f'{XF_API_URL}/attachments/new-key'
URI_RESOURCE_VERSIONS = f'{XF_API_URL}/resource-versions'
MAX_MESSAGE_CHARS = 10_000


def handle_cli(
    resource_id: int,
    *,
    description: str | Path | None = None,
    version: str | None = None,
    message: str | Path | None = None,
    file: str | Path | None = None,
    domain: str | None = None,
) -> None:
    """Publish the requested resource updates."""
    if not any([description, version, message, file]):
        print("At least one option (--description, --version, --message, or --file) must be specified.")
        raise typer.Exit(code=1)

    if message and not version:
        print("The --message option requires --version to be specified.")
        raise typer.Exit(code=1)

    auth.initialize_keyring()
    auth.authorize()

    # Reuse one set of headers for every request.
    headers, resource = asyncio.run(_fetch_headers_and_resource(resource_id))
    resource_id = resource['resource_id']

    if description:
        publish_description(resource_id, description, headers, domain=domain)

    if version and message:
        publish_message(resource_id, version, message, headers, domain=domain)

    if file:
        publish_file(resource_id, file, headers, version=version)


async def _fetch_headers_and_resource(resource_id: int) -> tuple[dict[str, str], dict]:
    headers = await auth.get_api_headers()
    resource = await api.get_resource_details(resource_id, headers)
    return headers, resource


def publish_description(
    resource_id: int,
    description_path: str | Path,
    headers: dict[str, str],
    domain: str | None = None,
) -> None:
    """Publish a description file."""
    path = Path(description_path)
    description = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".md":
        description = process_readme(description, domain=domain)

    url = f"{XF_API_URL}/resources/{resource_id}"
    response = httpx.post(url, headers=headers, data={'description': description}, timeout=30.0)
    response.raise_for_status()
    print("Successfully updated the resource description.")


def publish_message(
    resource_id: int,
    version: str,
    message: str | Path,
    headers: dict[str, str],
    domain: str | None = None,
) -> None:
    """Publish a version message."""
    text = generate_version_message(message, version, domain=domain)
    if not text.strip():
        print("Warning: No message content provided, skipping update post.")
        return

    form = {
        'resource_id': resource_id,
        'title': version,
        'message': text,
    }
    response = httpx.post(URI_MESSAGE, headers=headers, data=form, timeout=30.0)
    response.raise_for_status()
    print(f"Successfully posted update '{version}' to resource {resource_id}.")


def publish_file(
    resource_id: int,
    file_path: str | Path,
    headers: dict[str, str],
    version: str | None = None,
) -> None:
    """Publish a release file."""
    try:
        upload_form = {"type": "resource_version", "context[resource_id]": resource_id}
        with open(file_path, "rb") as f:
            files = {"attachment": (Path(file_path).name, f, "application/octet-stream")}
            upload = httpx.post(URI_ATTACHMENT, headers=headers, data=upload_form, files=files, timeout=60.0)
        upload.raise_for_status()

        attach_key = upload.json().get("key")
        if not attach_key:
            print("[ERROR] No attachment key received from the server.")
            raise RuntimeError("No attachment key received from the server.")

        version_form = {
            "type": "resource_version",
            "resource_id": resource_id,
            "version_attachment_key": attach_key,
        }
        if version:
            version_form["version_string"] = version
        response = httpx.post(URI_RESOURCE_VERSIONS, headers=headers, data=version_form, timeout=60.0)
        response.raise_for_status()
        print(f"Successfully added attachment for resource {resource_id}")
    except httpx.HTTPStatusError as e:
        print(f"HTTP Error: {e.response.status_code} - {e.response.text}")
        raise
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        raise


def generate_version_message(
    message: str | Path,
    version: str | None,
    domain: str | None = None,
) -> str:
    """Build a version message from text or a file."""
    if not message or not os.path.isfile(message):
        return _truncate_text(str(message))

    message_path = Path(message)

    if message_path.suffix.lower() != ".md":
        return _truncate_text(message_path.read_text(encoding="utf-8", errors="replace"))

    changes = _try_parse_keepachangelog_dict(message_path)
    if changes is not None:
        try:
            return _truncate_text(parse_changelog(message_path, version, domain=domain, changes=changes))
        except ValueError as e:
            print(f"Warning: {e}. Posting full file contents instead.")

    markdown_text = message_path.read_text(encoding="utf-8", errors="replace")
    return _truncate_text(process_readme(markdown_text, domain=domain))


def parse_changelog(
    changelog_path: str | Path,
    version: str,
    domain: str | None = None,
    changes: dict[str, dict] | None = None,
) -> str:
    """Convert one changelog version to BBCode."""
    if changes is None:
        changes = keepachangelog.to_dict(changelog_path)

    version_key = version.removeprefix('v')
    if version_key not in changes:
        raise ValueError(f"Version {version} not found in {changelog_path}")

    markdown_lines = []
    for section, notes in changes[version_key].items():
        if section == 'metadata':
            continue
        markdown_lines.append(f"### {section.capitalize()}")
        markdown_lines.extend(f"- {note}" for note in notes)
        markdown_lines.append("")

    return process_readme("\n".join(markdown_lines), domain=domain)


def _try_parse_keepachangelog_dict(changelog_path: str | Path) -> dict[str, dict] | None:
    """Parse a keep-a-changelog file; None if it doesn't look like one."""
    try:
        changes = keepachangelog.to_dict(changelog_path)
    except Exception:
        return None

    if not isinstance(changes, dict) or not changes:
        return None

    return changes


def _truncate_text(text: str, max_chars: int = MAX_MESSAGE_CHARS, suffix: str = "\n\n(truncated)") -> str:
    """Truncate text to fit the message limit."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    suffix = suffix or ""
    if len(suffix) >= max_chars:
        return suffix[:max_chars]

    allowed = max_chars - len(suffix)
    return text[:allowed].rstrip() + suffix
