import os
import asyncio
import httpx
import keepachangelog
from md2bbcode.main import process_readme

from redfetch import api
from redfetch.net import BASE_URL
from redfetch import auth
import sys

XF_API_URL = f'{BASE_URL}/api'
URI_MESSAGE = f'{XF_API_URL}/resource-updates'
URI_ATTACHMENT = f'{XF_API_URL}/attachments/new-key'
URI_RESOURCE_VERSIONS = f'{XF_API_URL}/resource-versions'


def _get_api_headers_blocking() -> dict:
    """Synchronous helper to obtain API headers from the async API client."""
    return asyncio.run(api.get_api_headers())


def update_resource_description(resource_id, new_description):
    """Update resource description."""
    url = f"{XF_API_URL}/resources/{resource_id}"
    payload = {'description': new_description}
    headers = _get_api_headers_blocking()
    response = httpx.post(url, headers=headers, data=payload, timeout=30.0)
    response.raise_for_status()
    print("Successfully updated the resource description.")


def add_xf_message(resource_id, msg_title, message):
    """Post a new resource update message."""
    headers = _get_api_headers_blocking()
    form_message = {
        'resource_id': resource_id,
        'title': msg_title,
        'message': message
    }
    response = httpx.post(URI_MESSAGE, headers=headers, data=form_message, timeout=30.0)
    response.raise_for_status()
    print(f"Response: {response.status_code}, {response.text}")
    return response.json()


def add_xf_attachment(resource_id, upfilename, version=None):
    """Upload a file and attach it as a new resource version."""
    headers = _get_api_headers_blocking()

    # Prepare the data for getting an attachment key and uploading the file
    data = {
        "type": "resource_version",
        "context[resource_id]": resource_id
    }

    try:
        # Get an attachment key and also upload the file
        with open(upfilename, "rb") as file:
            files = {"attachment": (os.path.basename(upfilename), file, "application/octet-stream")}
            response = httpx.post(URI_ATTACHMENT, headers=headers, data=data, files=files, timeout=60.0)
            response.raise_for_status()
            content = response.json()
            attach_key = content.get("key")
            if attach_key:
                # Now associate the attachment(s) with the resource version
                data_update = {
                    "type": "resource_version",
                    "resource_id": resource_id,
                    "version_attachment_key": attach_key,
                }
                if version:
                    data_update["version_string"] = version
                response_update = httpx.post(URI_RESOURCE_VERSIONS, headers=headers, data=data_update, timeout=60.0)
                response_update.raise_for_status()
                print(f"Successfully added attachment for resource {resource_id}")
            else:
                print("[ERROR] No attachment key received from the server.")
                raise RuntimeError("No attachment key received from the server.")
    except httpx.HTTPStatusError as e:
        print(f"HTTP Error: {e.response.status_code} - {e.response.text}")
        raise
    except FileNotFoundError:
        print(f"Error: File '{upfilename}' not found.")
        raise
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        raise


def update_resource(resource_id, version_info, upfilename=None):
    """Create a version update (message + optional attachment)."""
    message = version_info.get('message', '')
    if message.strip():
        add_xf_message(resource_id, version_info['version_string'], message)
    else:
        print("Warning: No message content provided, skipping update post.")

    if upfilename:
        add_xf_attachment(resource_id, upfilename, version_info['version_string'])


def convert_markdown_to_bbcode(markdown_text, domain=None):
    """Convert markdown text to BBCode."""
    bbcode_output = process_readme(markdown_text, domain=domain)
    return bbcode_output


def parse_changelog(changelog_path, version, domain=None):
    """
    Parses the changelog file and returns the changelog entry for the given version as BBCode.
    """
    # Use keepachangelog to parse the changelog file
    changes = keepachangelog.to_dict(changelog_path)
    # Remove 'v' prefix if present
    version_key = version.lstrip('v')
    if version_key in changes:
        # Flatten the change notes into a markdown string
        version_data = changes[version_key]
        markdown_lines = []
        for section, notes in version_data.items():
            if section != 'metadata':
                markdown_lines.append(f"### {section.capitalize()}")
                for note in notes:
                    markdown_lines.append(f"- {note}")
                markdown_lines.append("")  # Add a newline
        markdown_message = "\n".join(markdown_lines)
        # Convert markdown to BBCode
        bbcode_message = convert_markdown_to_bbcode(markdown_message, domain=domain)
        return bbcode_message
    else:
        raise ValueError(f"Version {version} not found in {changelog_path}")


def generate_version_message(args):
    """Build version message; optionally parse changelog markdown and convert to BBCode."""
    if os.path.isfile(args.message):
        if args.message.lower().endswith('.md'):
            # If it's a markdown file (e.g., CHANGELOG.md)
            message = parse_changelog(args.message, args.version, domain=args.domain)
        else:
            # Raise an error for non-markdown files
            raise ValueError(f"The --message file '{args.message}' must end with '.md' and follow keepachangelog format.")
    else:
        # If --message is a regular string, use it directly
        message = args.message
    return message


def update_description(resource_id, description_path, domain=None):
    """Read description, convert markdown to BBCode if needed, then update."""
    with open(description_path, 'r') as f:
        new_description = f.read()
    if description_path.lower().endswith('.md'):
        new_description = convert_markdown_to_bbcode(new_description, domain=domain)
    update_resource_description(resource_id, new_description)


def handle_cli(args):
    """Handle the push subcommand using existing push helpers."""

    if not any([args.description, args.version, args.message, args.file]):
        print("At least one option (--description, --version, --message, or --file) must be specified.")
        sys.exit(1)

    if args.domain and not (args.description or args.message):
        print("The --domain option requires either --description or --message to be specified.")
        sys.exit(1)

    if args.message and not args.version:
        print("The --message option requires --version to be specified.")
        sys.exit(1)

    try:
        # Ensure the user is authorized
        auth.initialize_keyring()
        auth.authorize()

        # Blocking call is fine here; push is a short-lived CLI operation.
        resource = asyncio.run(api.get_resource_details(args.resource_id))
        resource_id = resource['resource_id']

        if args.description:
            update_description(resource_id, args.description, domain=args.domain)

        if args.version and args.message:
            message = generate_version_message(args)
            version_info = {'version_string': args.version, 'message': message}
            update_resource(resource_id, version_info, args.file)
        elif args.file:
            # Allow publishing a version with a file but no changelog message.
            add_xf_attachment(resource_id, args.file, args.version)

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)
