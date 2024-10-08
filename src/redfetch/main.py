# standard
import argparse
import sys

#external
from dynaconf import ValidationError

# local
from . import api
from . import auth
from . import config
from . import selfupdate
from . import db
from . import download
from . import utils

# Global constant
CATEGORY_MAP = config.CATEGORY_MAP

def parse_arguments():
    # Initialize the parser for operational arguments
    parser = argparse.ArgumentParser(description="RedFetch CLI.")
    
    # Operational arguments
    parser.add_argument('--logout', action='store_true', help='Log out and clear cached token.')
    parser.add_argument('--download-resource', help='Force download a resource by its ID.')
    parser.add_argument('--download-watched', action='store_true', help='Download all watched & special resources.')
    parser.add_argument('--force-download', action='store_true', help='Force download all watched resources.')
    parser.add_argument('--list-resources', action='store_true', help='List all resources in the cache.')
    parser.add_argument('--serve', action='store_true', help='Run as a server to handle download requests.')
    parser.add_argument('--update-setting', nargs=2, metavar=('SETTING_PATH', 'VALUE'), help='Update a setting by specifying the path and value. Path should be dot-separated.')
    parser.add_argument('--switch-env', metavar='ENVIRONMENT', help='Chage the server type. Live, Test, Emu.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {selfupdate.get_current_version()}')

    # Parse the arguments
    args = parser.parse_args()

    return args

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
    print(f"special_resource_status: {special_resource_status}")
    return special_resource_status

def process_resources(cursor, resources):
    current_ids = set()
    for resource in resources:
        current_ids.add((None, resource['resource_id']))  # Add to current IDs
        if resource['Category']['parent_category_id'] in CATEGORY_MAP:
            db.insert_prepared_resource(cursor, resource, is_special=False, is_dependency=False, parent_id=None, license_details=None)
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
        if resource['Category']['parent_category_id'] in CATEGORY_MAP:
            db.insert_prepared_resource(cursor, resource, is_special=False, is_dependency=False, parent_id=None, license_details=license_details)
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
        # Convert resource_id to string
        resource_id = str(resource_id)
        # Convert parent_resource_id to string if it's not None
        if parent_resource_id is not None:
            parent_resource_id = str(parent_resource_id)

        if local_version is None or local_version < remote_version:
            print(f"Downloading updates for resource {resource_id}.")
            success = download.download_resource(resource_id, parent_category_id, download_url, filename, headers, is_dependency=bool(parent_resource_id), parent_resource_id=parent_resource_id)
            if success:
                db.update_download_date(resource_id, remote_version, bool(parent_resource_id), parent_resource_id, cursor) # Update database after successful download
                return True
            else:
                print(f"Error occurred while downloading resource {resource_id}.")
                return False
        else:
            print(f"Skipping download for resource {resource_id} - no new updates since last download.")
            return True  # Consider skipping as a success
    except KeyboardInterrupt:
        print(f"\nDownload of resource {resource_id} cancelled by user.")
        return 'cancelled'

def synchronize_db_and_download(cursor, headers, resource_ids=None):
    # Save the original resource IDs to download the correct dependencies
    original_resource_ids = resource_ids[:] if resource_ids is not None else None
    # Fetch latest from RG plus local special resources
    fetched_data = fetch_from_api(headers, resource_ids)
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
            result = handle_resource_download(cursor, headers, resource)
            if result == 'cancelled':
                print("\nCancelling remaining downloads...")
                return False
            download_results.append(result)
    except KeyboardInterrupt:
        print("\nDownload process was cancelled by user.")
        return False
    
    if all(download_results):
        print("All resources downloaded successfully.")
        return True
    else:
        print("One or more resources failed to download.")
        failed_resources = [resource[0] for resource, result in zip(resource_data, download_results) if not result]
        print(f"Failed resources: {failed_resources}")
        return False

def main():
    update_available = selfupdate.check_for_update()
    args = parse_arguments()
    auth.initialize_keyring()
    auth.authorize()

    if args.logout:
        auth.logout()
        print("Logged out successfully.")
        return
    
    validate_settings()

    if args.switch_env:
        config.switch_environment(args.switch_env)
        print(f"Environment updated to {args.switch_env}.")
        print("New complete configuration:", config.settings.from_env(args.switch_env).as_dict())
        return

    if args.update_setting:
        setting_path, value = args.update_setting
        setting_path_list = setting_path.split('.')
        config.update_setting(setting_path_list, value)
        print(f"Updated setting {setting_path} to {value}.")
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
        run_server(config.settings, db_name, headers, special_resources, CATEGORY_MAP)
        return

    if not any(vars(args).values()):
        print("No arguments provided, launching UI.")
        from .terminal_ui import run_textual_ui
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
            synchronize_db_and_download(cursor, headers)

if __name__ == "__main__":
    main()