import os
import requests
import keepachangelog  # For parsing changelog
from md2bbcode.main import process_readme  # For markdown to BBCode conversion

# Import authentication functions
from redfetch.api import get_api_headers

# Constants
BASE_URL = os.environ.get('REDFETCH_BASE_URL', 'https://www.redguides.com/community')
XF_API_URL = f'{BASE_URL}/api'
URI_MESSAGE = f'{XF_API_URL}/resource-updates'
URI_ATTACHMENT = f'{XF_API_URL}/attachments/new-key'
URI_RESPONSE = f'{XF_API_URL}/resource-versions'

def get_resource_details(resource_id):
    """
    Retrieves details of a specific resource.
    """
    url = f"{XF_API_URL}/resources/{resource_id}"
    headers = get_api_headers()
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()['resource']

def update_resource_description(resource_id, new_description):
    """
    Updates the description of a resource.
    """
    url = f"{XF_API_URL}/resources/{resource_id}"
    payload = {'description': new_description}
    headers = get_api_headers()
    response = requests.post(url, headers=headers, data=payload)
    response.raise_for_status()
    print("Successfully updated the resource description.")

def add_xf_message(resource, msg_title, message):
    """
    Adds a new message (update) to the resource.
    """
    resource_id = resource['resource_id']
    headers = get_api_headers()
    form_message = {
        'resource_id': resource_id,
        'title': msg_title,
        'message': message
    }
    response = requests.post(URI_MESSAGE, headers=headers, data=form_message)
    response.raise_for_status()
    print(f"Response: {response.status_code}, {response.text}")
    return response.json()

def add_xf_attachment(resource, upfilename, version=None):
    """
    Adds an attachment (file upload) to the resource.
    """
    resource_id = resource['resource_id']
    headers = get_api_headers()

    # Prepare the data for getting an attachment key and uploading the file
    data = {
        "type": "resource_version",
        "context[resource_id]": resource_id
    }

    try:
        # Get an attachment key and also upload the file
        with open(upfilename, "rb") as file:
            files = {"attachment": (os.path.basename(upfilename), file, "application/octet-stream")}
            response = requests.post(URI_ATTACHMENT, headers=headers, data=data, files=files)
            response.raise_for_status()
            content = response.json()
            attachKey = content.get("key")
            if attachKey:
                # Now associate the attachment(s) with the resource version
                data_update = {
                    "type": "resource_version",
                    "resource_id": resource_id,
                    "version_attachment_key": attachKey,
                }
                if version:
                    data_update["version_string"] = version
                response_update = requests.post(URI_RESPONSE, headers=headers, data=data_update)
                response_update.raise_for_status()
                print(f"Successfully added attachment for resource {resource_id}")
            else:
                print("[ERROR] No attachment key received from the server.")
    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e.response.status_code} - {e.response.text}")
    except FileNotFoundError:
        print(f"Error: File '{upfilename}' not found.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

def update_resource(resource, version_info, upfilename=None):
    """
    Updates the resource with a new version and message.
    """
    add_xf_message(resource, version_info['version_string'], version_info['message'])
    if upfilename:
        add_xf_attachment(resource, upfilename, version_info['version_string'])

def convert_markdown_to_bbcode(markdown_text, domain=None):
    """
    Converts markdown text to BBCode using md2bbcode library.
    """
    bbcode_output = process_readme(markdown_text, domain=domain)
    return bbcode_output

def read_file_content(file_path):
    """
    Reads the content of a file.
    """
    with open(file_path, 'r') as f:
        content = f.read()
    return content

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
    """
    Generates the version message, converting markdown to BBCode if necessary.
    """
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
    """
    Reads the description file, converts markdown to BBCode if necessary, and updates the resource description.
    """
    new_description = read_file_content(description_path)
    if description_path.lower().endswith('.md'):
        new_description = convert_markdown_to_bbcode(new_description, domain=domain)
    update_resource_description(resource_id, new_description)
