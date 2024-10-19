"""
This script publishes resources on RedGuides Xenforo Resource Manager.

Usage:
    python newscript.py resource_id [--description DESCRIPTION] [--version VERSION] [--message MESSAGE] [--file FILE] [--domain DOMAIN]

Arguments:
    resource_id            The ID of the resource to update

Options:
    --description          Path to the new description file (README)
    --version              New version number (e.g., v1.0.1)
    --message              Version update message or path to CHANGELOG.md
    --file                 Path to the file to upload as a new version
    --domain               Domain to prepend to relative URLs in markdown (e.g., https://raw.githubusercontent.com/yourusername/yourrepo/main/)
                           Requires either --message or --description to be specified

Note: At least one option must be specified along with the resource_id.
"""

import os
import argparse
import requests
from requests.exceptions import RequestException
import keepachangelog  # Import keepachangelog library
from md2bbcode.main import process_readme  # Import md2bbcode for markdown to BBCode conversion

# Constants
XF_API_URL = 'https://www.redguides.com/devtestbaby/api'  # Use the correct API URL
XF_API_KEY = os.environ.get('XF_API_KEY')
XF_API_USER = os.environ.get('XF_API_USER')
URI_MESSAGE = f'{XF_API_URL}/resource-updates'
URI_ATTACHMENT = f'{XF_API_URL}/attachments/new-key'
URI_RESPONSE = f'{XF_API_URL}/resource-versions'

def authenticate():
    if not XF_API_KEY or not XF_API_USER:
        raise ValueError("XF_API_KEY and XF_API_USER must be set in environment variables.")

    session = requests.Session()
    session.headers.update({
        'XF-Api-Key': XF_API_KEY,
        'XF-Api-User': XF_API_USER
    })

    try:
        response = session.get(f"{XF_API_URL}/index")
        response.raise_for_status()
        print("Authentication successful.")
        return session
    except RequestException as e:
        print(f"Authentication failed: {e}")
        raise

def get_resource_details(session, resource_id):
    url = f"{XF_API_URL}/resources/{resource_id}"
    headers = {
        'XF-Api-Key': XF_API_KEY,
        'XF-Api-User': XF_API_USER
    }
    response = session.get(url, headers=headers)
    response.raise_for_status()
    return response.json()['resource']

def update_resource_description(session, resource_id, new_description):
    url = f"{XF_API_URL}/resources/{resource_id}"
    payload = {'description': new_description}
    headers = {
        'XF-Api-Key': XF_API_KEY,
        'XF-Api-User': XF_API_USER
    }
    response = session.post(url, headers=headers, data=payload)
    response.raise_for_status()
    print("Successfully updated the resource description.")

def add_xf_message(session, resource, msg_title, message):
    resource_id = resource['resource_id']
    headers = {
        "XF-Api-Key": XF_API_KEY,
        "XF-Api-User": str(resource['user_id'])
    }

    form_message = {
        'resource_id': resource_id,
        'title': msg_title,
        'message': message
    }
    response = session.post(URI_MESSAGE, headers=headers, data=form_message)
    response.raise_for_status()
    print(f"Response: {response.status_code}, {response.text}")
    return response.json()

def add_xf_attachment(session, resource, upfilename, version=None):
    resource_id = resource['resource_id']
    user_id = resource['user_id']
    headers = {
        "XF-Api-Key": XF_API_KEY,
        "XF-Api-User": str(user_id)
    }

    # Prepare the data for getting an attachment key and uploading the file
    data = {
        "type": "resource_version",
        "context[resource_id]": resource_id
    }

    try:
        # Get an attachment key and also upload the file
        with open(upfilename, "rb") as file:
            files = {"attachment": (os.path.basename(upfilename), file, "application/octet-stream")}
            response = session.post(URI_ATTACHMENT, headers=headers, data=data, files=files)
            response.raise_for_status()
            content = response.json()
            attachKey = content.get("key")
            if attachKey:
                # Now associate the attachment(s) for that key with the resource version
                data_update = {
                    "type": "resource_version",
                    "resource_id": resource_id,
                    "version_attachment_key": attachKey,
                }
                if version:
                    data_update["version_string"] = version
                response_update = session.post(URI_RESPONSE, headers=headers, data=data_update)
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

def update_resource(session, resource, version_info, upfilename=None):
    add_xf_message(session, resource, version_info['version_string'], version_info['message'])
    if upfilename:
        add_xf_attachment(session, resource, upfilename, version_info['version_string'])

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

def update_description(session, resource_id, description_path, domain=None):
    """
    Reads the description file, converts markdown to BBCode if necessary, and updates the resource description.
    """
    new_description = read_file_content(description_path)
    if description_path.lower().endswith('.md'):
        new_description = convert_markdown_to_bbcode(new_description, domain=domain)
    update_resource_description(session, resource_id, new_description)

def main():
    parser = argparse.ArgumentParser(description='Publish resources on RedGuides Xenforo Resource Manager')
    parser.add_argument('resource_id', type=int, help='The ID of the resource to update')
    parser.add_argument('--description', help='Path to the new description file (README)')
    parser.add_argument('--version', help='New version number (e.g., v1.0.1)')
    parser.add_argument('--message', help='Version update message or path to CHANGELOG.md')
    parser.add_argument('--file', help='Path to the file to upload as a new version')
    parser.add_argument('--domain', help='Domain to prepend to relative URLs in markdown (e.g., https://raw.githubusercontent.com/yourusername/yourrepo/main/)')
    args = parser.parse_args()

    if not any([args.description, args.version, args.message, args.file]):
        parser.error("At least one option (--description, --version, --message, or --file) must be specified.")

    if args.domain and not (args.description or args.message):
        parser.error("The --domain option requires either --description or --message to be specified.")

    if args.message and not args.version:
        parser.error("The --message option requires --version to be specified.")

    try:
        session = authenticate()
        resource = get_resource_details(session, args.resource_id)

        if args.description:
            update_description(session, args.resource_id, args.description, domain=args.domain)

        if args.version and args.message:
            message = generate_version_message(args)
            version_info = {'version_string': args.version, 'message': message}
            update_resource(session, resource, version_info, args.file)
        elif args.file:
            add_xf_attachment(session, resource, args.file, None)

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
