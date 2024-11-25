# standard imports
import argparse
import sys
import os

# third-party imports
from dynaconf import ValidationError
from rich import print as rprint
from rich.prompt import Confirm, InvalidResponse
from rich_argparse import RichHelpFormatter

# local imports
from redfetch import api
from redfetch import auth
from redfetch import config
from redfetch import meta
from redfetch import db
from redfetch import download
from redfetch import utils
from redfetch import push

def parse_arguments():
    parser = argparse.ArgumentParser(description="redfetch CLI.", formatter_class=RichHelpFormatter)

    parser.add_argument('--logout', action='store_true', help='Log out and clear cached token.')
    parser.add_argument('--download-resource', metavar='RESOURCE_ID | URL', help='Download a resource by its ID or URL', type=utils.parse_resource_id)
    parser.add_argument('--download-watched', action='store_true', help='Download all watched & special resources.')
    parser.add_argument('--force-download', action='store_true', help='Force download all watched resources.')
    parser.add_argument('--list-resources', action='store_true', help='List all resources in the cache.')
    parser.add_argument('--serve', action='store_true', help='Run as a server to handle download requests.')
    parser.add_argument('--update-setting', nargs='+', metavar=('SETTING_PATH VALUE [ENVIRONMENT]'), help='Update a setting by specifying the path and value. Path should be dot-separated. Environment is optional. Example: --update-setting SPECIAL_RESOURCES.1974.opt_in false LIVE')
    parser.add_argument('--switch-env', metavar='ENVIRONMENT', help='Change the server type. LIVE, TEST, EMU.')
    parser.add_argument('--version', action='version', version=f'redfetch {meta.get_current_version()}')
    parser.add_argument('--uninstall', action='store_true', help='Uninstall redfetch and clean up data.')

    # Subparsers for commands (Only for 'push')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Push Command Parser
    push_parser = subparsers.add_parser('push', help='Publish resources on RedGuides. (see also the github action for this)', formatter_class=parser.formatter_class)
    push_parser.add_argument('resource_id', type=int, help='The ID of the resource to update. Must already exist on RedGuides.')
    push_parser.add_argument('--description', metavar='README.md', help='Path to a description file (e.g. README.md) which will become the "overview" description of your resource.')
    push_parser.add_argument('--version', help='New version number (e.g., v1.0.1)')
    push_parser.add_argument('--message', metavar='FILE | MESSAGE', help='Version update message or path to CHANGELOG.md in "keep a changelog" format. Requires --version.')
    push_parser.add_argument('--file', metavar='FILE.zip', help='Path to the your zipped release file')
    push_parser.add_argument('--domain', help='If using a description or message file, this is the domain to prepend to relative URLs (e.g., https://raw.githubusercontent.com/yourusername/yourrepo/main/)')

    return parser.parse_args()

def validate_settings():
    try:
        config.settings.validators.validate()
    except ValidationError as e:
        print(f"Validation error: {e}")
        sys.exit(1)
    print("Server:", config.settings.current_env)

def get_special_resource_status(resource_ids=None):
    resource_ids = utils.filter_and_fetch_dependencies(resource_ids)
    special_resource_status = {}
    for res_id in resource_ids:
        is_special, is_dependency, parent_ids = utils.is_special_or_dependency(res_id)
        special_resource_status[res_id] = {
            'is_special': is_special,
            'is_dependency': is_dependency,
            'parent_ids': set(parent_ids)
        }
    #print(f"special_resource_status: {special_resource_status}")
    return special_resource_status

def process_resources(cursor, resources):
    current_ids = set()
    for resource in resources:
        if not resource:
            continue  # Skip None resources
        # Only add to the db if it's in an MQ category
        if resource['Category']['parent_category_id'] in config.CATEGORY_MAP:
            db.insert_prepared_resource(
                cursor,
                resource,
                is_special=False,
                is_dependency=False,
                parent_id=None,
                license_details=None
            )
            current_ids.add((None, resource['resource_id']))  # Add to current IDs
    return current_ids

def process_licensed_resources(cursor, licensed_resources):
    current_ids = set()
    for license_info in licensed_resources:
        resource = license_info['resource']
        license_details = {
            'active': license_info['active'],
            'start_date': license_info.get('start_date'),
            'end_date': license_info.get('end_date'),
            'license_id': license_info['license_id']
        }
        if resource['Category']['parent_category_id'] in config.CATEGORY_MAP:
            db.insert_prepared_resource(
                cursor,
                resource,
                is_special=False,
                is_dependency=False,
                parent_id=None,
                license_details=license_details
            )
            current_ids.add((None, resource['resource_id']))  # Add to current IDs
    return current_ids

def process_special_resources(cursor, special_resource_status, special_resources_data):
    current_ids = set()
    for resource in special_resources_data:
        res_id = str(resource['resource_id'])
        if res_id not in special_resource_status:
            continue
        status = special_resource_status[res_id]
        is_special = status['is_special']
        is_dependency = status['is_dependency']
        parent_ids = status['parent_ids']

        if not parent_ids and is_special:  # Handle special resources with no dependencies
            db.insert_prepared_resource(cursor, resource, is_special, is_dependency, parent_id=None, license_details=None)
            current_ids.add((None, res_id))  # Add to current IDs without a parent ID

        for parent_id in parent_ids:
            current_ids.add((parent_id, res_id))
            db.insert_prepared_resource(cursor, resource, is_special, is_dependency, parent_id, license_details=None)
    return current_ids

def fetch_from_api(headers, resource_ids=None):
    if resource_ids is None:
        # Fetch all watched resources if no specific IDs are provided
        watched_resources = api.fetch_watched_resources(headers)
        licensed_resources = api.fetch_licenses(headers)
        special_resource_status = get_special_resource_status()
    else:
        # Fetch only the specified resources
        watched_resources = [api.fetch_single_resource(rid, headers) for rid in resource_ids]
        licensed_resources = []  # Assuming no licenses for specific resource fetches
        special_resource_status = get_special_resource_status(resource_ids)
        
    # fetch each resource only once
    special_resources_data = api.fetch_single_resource_batch(list(special_resource_status.keys()), headers)

    return {
        'watched_resources': watched_resources,
        'licensed_resources': licensed_resources,
        'special_resource_status': special_resource_status,
        'special_resources_data': special_resources_data
    }

def store_fetched_data(cursor, fetched_data):
    current_ids = process_resources(cursor, fetched_data['watched_resources'])
    current_ids.update(process_licensed_resources(cursor, fetched_data['licensed_resources']))
    current_ids.update(process_special_resources(cursor, fetched_data['special_resource_status'], fetched_data['special_resources_data']))

    return current_ids

def handle_resource_download(cursor, headers, resource):
    try:
        resource_id, parent_category_id, remote_version, local_version, parent_resource_id, download_url, filename = resource
        resource_id = str(resource_id)
        if parent_resource_id is not None:
            parent_resource_id = str(parent_resource_id)

        # Get the resource title if available
        title = db.get_resource_title(cursor, resource_id)
        resource_display = f"{title} (ID: {resource_id})" if title else f"resource {resource_id}"

        if local_version is None or local_version < remote_version:
            print(f"Downloading updates for {resource_display}.")
            success = download.download_resource(
                resource_id,
                parent_category_id,
                download_url,
                filename,
                headers,
                is_dependency=bool(parent_resource_id),
                parent_resource_id=parent_resource_id,
            )
            if success:
                db.update_download_date(
                    resource_id,
                    remote_version,
                    bool(parent_resource_id),
                    parent_resource_id,
                    cursor,
                )
                return 'downloaded'  # Indicate successful download
            else:
                print(f"Error occurred while downloading {resource_display}.")
                return 'error'  # Indicate download error
        else:
            print(f"Skipping download for {resource_display} - no new updates since last download.")
            return 'skipped'  # Indicate resource was up-to-date
    except KeyboardInterrupt:
        print(f"\nDownload of {resource_display} cancelled by user.")
        return 'cancelled'  # Indicate download was cancelled

def synchronize_db_and_download(cursor, headers, resource_ids=None, worker=None):
    # Save the original resource_ids (if provided) to download their correct dependencies
    original_resource_ids = resource_ids[:] if resource_ids is not None else None
    # Fetch latest from RG plus locally-defined special resources
    fetched_data = fetch_from_api(headers, resource_ids)
    if resource_ids and not fetched_data['watched_resources']:
        print(f"No valid resources found for IDs: {resource_ids}")
        return False
    # Store fetched data in the database
    current_ids = store_fetched_data(cursor, fetched_data)
    
    # Fetch and download specific resource(s)
    if resource_ids is not None:
        resource_data = []
        for rid in original_resource_ids:
            single_resource_data = db.fetch_single_db_resource(rid, cursor)
            resource_data.extend(single_resource_data)  # Flatten the list of resources and dependencies
    else:
        # Clean up the database when downloading watched resources
        db.clean_up_unnecessary_resources(cursor, current_ids)
        # Fetch and download watched, special, and licensed resources from the database
        resource_data = db.fetch_watched_db_resources(cursor)
    
    print(f"Total resources to process: >>> {len(resource_data)} <<<")
    
    download_results = []
    try:
        for resource in resource_data:
            # Check if the worker has been cancelled
            if worker and worker.is_cancelled:
                print("\nCancelling remaining downloads.")
                return False  # Exit the function gracefully
            result = handle_resource_download(cursor, headers, resource)
            if result == 'cancelled':
                print("\nCancelling remaining downloads.")
                return False
            download_results.append((resource[0], result))  # Store resource_id and result
    except KeyboardInterrupt:
        print("\nDownload process was cancelled by user.")
        return False
    
    # Separate resources based on the result
    downloaded_resources = [res_id for res_id, res in download_results if res == 'downloaded']
    skipped_resources = [res_id for res_id, res in download_results if res == 'skipped']
    errored_resources = [res_id for res_id, res in download_results if res == 'error']

    if errored_resources:
        print("One or more resources failed to download.")
        print(f"Failed resources: {errored_resources}")
        return False
    elif downloaded_resources:
        print("All resources downloaded successfully.")
        return True
    else:
        print("All resources are up-to-date; no downloads were necessary.")
        return True

def handle_push(args):
    API_KEY = os.environ.get('REDGUIDES_API_KEY')
    # Ensure the user is authorized
    if not API_KEY:
        auth.initialize_keyring()
        auth.authorize()
    else:
        print("Using API key from environment variable. Skipping OAuth.")

    if not any([args.description, args.version, args.message, args.file]):
        print("At least one option (--description, --version, --message, or --file) must be specified.")
        return

    if args.domain and not (args.description or args.message):
        print("The --domain option requires either --description or --message to be specified.")
        return

    if args.message and not args.version:
        print("The --message option requires --version to be specified.")
        return

    try:
        resource = push.get_resource_details(args.resource_id)

        if args.description:
            push.update_description(args.resource_id, args.description, domain=args.domain)

        if args.version and args.message:
            message = push.generate_version_message(args)
            version_info = {'version_string': args.version, 'message': message}
            push.update_resource(resource, version_info, args.file)
        elif args.file:
            push.add_xf_attachment(resource, args.file, None)

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1) #exit with a non-zero code to indicate failure

def handle_fetch(args):
    API_KEY = os.environ.get('REDGUIDES_API_KEY')

    if args.logout:
        if not API_KEY:
            # Initialize keyring to ensure access to stored credentials
            auth.initialize_keyring()
            auth.logout()
            print("Logged out successfully.")
        else:
            print("Cannot logout when using API key from environment variable.")
        return

    if not API_KEY:
        # Only initialize keyring and authorize if API_KEY not provided
        auth.initialize_keyring()
        auth.authorize()
    else:
        print("Using API key from environment variable. Skipping OAuth.")

    validate_settings()

    if args.switch_env:
        ENV = args.switch_env.upper()
        config.switch_environment(ENV)
        print(f"Environment updated to {ENV}.")
        print("New complete configuration:", config.settings.from_env(ENV).as_dict())
        return

    if args.update_setting:
        num_args = len(args.update_setting)
        if num_args == 2:
            setting_path, value = args.update_setting
            env = None
        elif num_args == 3:
            setting_path, value, env = args.update_setting
        else:
            print("Error: --update-setting requires 2 or 3 arguments: SETTING_PATH VALUE [ENVIRONMENT]")
            return
        setting_path_list = setting_path.split('.')
        config.update_setting(setting_path_list, value, env)
        print(f"Updated setting {setting_path} to {value}{' in environment ' + env if env else ''}.")
        return

    if args.serve or args.download_resource or args.download_watched or args.force_download or args.list_resources:
        # These variables are now set only if needed
        db_name = f"{config.settings.ENV}_resources.db"
        db.initialize_db(db_name)
        headers = api.get_api_headers()
        special_resources = config.settings.SPECIAL_RESOURCES
        if not api.is_kiss_downloadable(headers):
            print("You're not level 2 on RedGuides, so some resources will not be downloadable.")

    if args.serve:
        from .listener import run_server
        run_server(config.settings, db_name, headers, special_resources, config.CATEGORY_MAP)
        return

    if not any(vars(args).values()):
        print("No arguments provided, launching UI.")
        from redfetch.terminal_ui import run_textual_ui
        run_textual_ui() 
        return

    with db.get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        if args.force_download:
            print("Force download requested. All watched resources will be re-downloaded.")
            db.reset_download_dates(cursor)
        if args.list_resources:
            db.list_resources(cursor)
            db.list_dependencies(cursor)
            return
        if args.download_resource:
            print(f"Downloading resource {args.download_resource}.")
            synchronize_db_and_download(cursor, headers, [args.download_resource])
        elif args.download_watched:
            handle_download_watched(cursor, headers)

def handle_download_watched(cursor, headers):
    if utils.is_mq_down():
        rprint("[bold yellow]Warning:[/bold yellow] [blink bold red]MQ appears to be down[/blink bold red] for a patch, so it's not likely to work.")
        continue_download = Confirm.ask("Do you want to continue with the download?", default=False)
        if not continue_download:
            print("Download cancelled by user.")
            return False

    mq_folder = utils.get_base_path()
    # Check if MQ or any other executable is running
    if utils.are_executables_running_in_folder(mq_folder):
        # Check auto-terminate setting
        auto_terminate = config.settings.from_env(config.settings.ENV).get('AUTO_TERMINATE_PROCESSES', None)
        if auto_terminate:
            utils.terminate_executables_in_folder(mq_folder)
        else:
            # Use the custom prompt
            try:
                user_choice = utils.TerminationPrompt.ask(
                    "Processes are running from the folder. Attempt to close them? [yes/no/always/never]",
                    default="yes"
                )
                if user_choice == "yes":
                    utils.terminate_executables_in_folder(mq_folder)
                elif user_choice == "always":
                    utils.terminate_executables_in_folder(mq_folder)
                    config.update_setting(['AUTO_TERMINATE_PROCESSES'], True)
                    print("Updated settings to always terminate processes.")
                elif user_choice == "never":
                    print("Continuing update without closing processes...")
                    config.update_setting(['AUTO_TERMINATE_PROCESSES'], False)
                    print("Updated settings to never terminate processes.")
                else:  # user_choice == "no"
                    print("Continuing update without closing processes...")
            except InvalidResponse:
                print("Invalid input. Continuing update without closing processes...")

    # Perform the download
    success = synchronize_db_and_download(cursor, headers)
    if success:
        handle_auto_run_macroquest()

def handle_auto_run_macroquest():
    # Skip if we're in CI or not on Windows
    if os.environ.get('CI') == 'true' or sys.platform != 'win32':
        return

    auto_run = config.settings.from_env(config.settings.ENV).get('AUTO_RUN_VVMQ', None)
    if auto_run is None:
        # Use the custom prompt
        try:
            user_choice = utils.TerminationPrompt.ask(
                "Do you want to start MacroQuest now? [yes/no/always/never]",
                default="yes"
            )
            if user_choice == "yes":
                pass  # Proceed to run MacroQuest
            elif user_choice == "always":
                config.update_setting(['AUTO_RUN_VVMQ'], True)
                print("Updated settings to always run MacroQuest after updates.")
            elif user_choice == "never":
                config.update_setting(['AUTO_RUN_VVMQ'], False)
                print("Updated settings to never run MacroQuest after updates.")
                return  # Do not run MQ
            else:  # user_choice == "no"
                print("Not starting MacroQuest.")
                return  # Do not run MQ
        except InvalidResponse:
            print("Invalid input. Not starting MacroQuest.")
            return  # Do not run MQ
    elif auto_run:
        pass  # Proceed to run MacroQuest
    else:
        # AUTO_RUN_VVMQ is False; do not run MacroQuest
        return

    # Proceed to run MacroQuest
    mq_path = utils.get_vvmq_path()
    if mq_path:
        exe_name = "MacroQuest.exe"
        utils.run_executable(mq_path, exe_name)
    else:
        print("MacroQuest path not found. Please check your configuration.")

def main():
    args = parse_arguments()
    config.initialize_config()

    # Handle uninstall command early if needed
    if args.uninstall:
        meta.uninstall()
        return

    # Skip update check in CI environment
    if os.environ.get('CI') != 'true':
        # Check for updates
        update_available = meta.check_for_update()

    # Check if the push command was called
    if args.command == 'push':
        handle_push(args)
    else:
        # Proceed with fetch-related operations
        handle_fetch(args)

if __name__ == "__main__":
    main()
