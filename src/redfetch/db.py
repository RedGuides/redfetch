#standard
import sqlite3
import time
import os

def get_db_connection(db_name):
    """Establishes a database connection to the specified SQLite database in the script directory."""
    config_dir = os.getenv('REDFETCH_CONFIG_DIR')
    db_path = os.path.join(config_dir, db_name)
    return sqlite3.connect(db_path)

def ensure_column_exists(cursor, table_name, column_name, data_type):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [info[1] for info in cursor.fetchall()]
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {data_type}")

def initialize_db(db_name):
    with get_db_connection(db_name) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS resources (
                resource_id INTEGER PRIMARY KEY,
                parent_category_id INTEGER,
                remote_version INTEGER,
                local_version INTEGER DEFAULT 0,
                title TEXT,
                tag_line TEXT,
                view_url TEXT,
                filename TEXT,
                download_url TEXT,
                is_watching BOOLEAN DEFAULT FALSE,
                is_special BOOLEAN DEFAULT FALSE,
                lic_active BOOLEAN,
                lic_start_date INTEGER,
                lic_end_date INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dependencies (
                parent_resource_id INTEGER,
                dependency_resource_id INTEGER,
                remote_version INTEGER,
                local_version INTEGER DEFAULT 0,
                title TEXT,
                tag_line TEXT,
                view_url TEXT,
                filename TEXT,
                download_url TEXT,
                is_watching BOOLEAN DEFAULT FALSE,
                lic_active BOOLEAN,
                lic_start_date INTEGER,
                lic_end_date INTEGER,
                PRIMARY KEY (parent_resource_id, dependency_resource_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY,
                last_fetch_time INTEGER
            )
        ''')

        # Ensure new columns exist
        ensure_column_exists(cursor, "resources", "remote_version", "INTEGER")
        ensure_column_exists(cursor, "dependencies", "remote_version", "INTEGER")
        ensure_column_exists(cursor, "resources", "is_special", "BOOLEAN")

        # Initialize metadata with a very old timestamp if not already set
        cursor.execute("INSERT INTO metadata (id, last_fetch_time) SELECT 1, 0 WHERE NOT EXISTS (SELECT 1 FROM metadata WHERE id = 1)")

def insert_resource_or_dependency(cursor, table, data):
    columns = ', '.join(data.keys())
    placeholders = ', '.join(['?'] * len(data))
    values = list(data.values())

    if table == "resources":
        cursor.execute(f"""
            INSERT INTO resources ({columns}) VALUES ({placeholders})
            ON CONFLICT(resource_id) DO UPDATE SET
            {', '.join([f"{key} = excluded.{key}" for key in data.keys() if key != 'resource_id'])}
        """, values)

    elif table == "dependencies":
        cursor.execute(f"""
            INSERT INTO dependencies ({columns}) VALUES ({placeholders})
            ON CONFLICT(parent_resource_id, dependency_resource_id) DO UPDATE SET
            {', '.join([f"{key} = excluded.{key}" for key in data.keys() if key not in ('parent_resource_id', 'dependency_resource_id')])}
        """, values)

def prepare_resource_data(resource, is_special=False, license_details=None):
    """ Prepare the data dictionary for inserting a resource, including license details. """
    data = {
        'resource_id': resource['resource_id'],
        'parent_category_id': resource['Category']['parent_category_id'],
        'remote_version': resource['current_files'][0]['id'],
        'title': resource['title'],
        'tag_line': resource['tag_line'] if resource['tag_line'] else '',
        'view_url': resource['view_url'] if resource['view_url'] else '',
        'is_watching': resource['is_watching'],
        'is_special': is_special,
        'filename': resource['current_files'][0]['filename'],
        'download_url': resource['current_files'][0]['download_url']
    }
    if license_details:
        data.update({
            'lic_active': license_details.get('active', False),
            'lic_start_date': license_details.get('start_date', None),
            'lic_end_date': license_details.get('end_date', None)
        })
    return data

def prepare_dependency_data(resource, parent_id, license_details=None):
    """ Prepare the data dictionary for inserting a dependency, including license details. """
    data = {
        'parent_resource_id': parent_id,
        'dependency_resource_id': resource['resource_id'],
        'remote_version': resource['current_files'][0]['id'],
        'title': resource['title'],
        'tag_line': resource['tag_line'] if resource['tag_line'] else '',
        'view_url': resource['view_url'] if resource['view_url'] else '',
        'is_watching': resource['is_watching'],
        'filename': resource['current_files'][0]['filename'],
        'download_url': resource['current_files'][0]['download_url']
    }
    if license_details:
        data.update({
            'lic_active': license_details.get('active', False),
            'lic_start_date': license_details.get('start_date', None),
            'lic_end_date': license_details.get('end_date', None)
        })
    return data

def insert_prepared_resource(cursor, resource, is_special, is_dependency, parent_id, current_ids=None, license_details=None):
    resource_id = resource['resource_id']

    if is_special or not is_dependency:
        #for normal and special resources
        data = prepare_resource_data(resource, is_special, license_details)  
        insert_resource_or_dependency(cursor, "resources", data)
        if current_ids is not None:
            current_ids.add((None, resource_id))  # None signifies no parent

    if is_dependency:
        dependency_data = prepare_dependency_data(resource, parent_id, license_details)
        insert_resource_or_dependency(cursor, "dependencies", dependency_data)
        dependency_info = (parent_id, resource_id)
        if current_ids is not None:
            current_ids.add(dependency_info)
        return dependency_info

    return (None, resource_id) if not is_dependency else dependency_info

def clean_up_unnecessary_resources(cursor, current_ids):
    # Extract resource and dependency IDs from current_ids
    resource_ids = {rid for pid, rid in current_ids if pid is None}
    parent_ids = {pid for pid, rid in current_ids if pid is not None}
    all_resource_ids = resource_ids.union(parent_ids)
    dependency_ids = {(pid, rid) for pid, rid in current_ids if pid is not None}

    # Update resources table to set is_watching to FALSE for those not in all_resource_ids
    if all_resource_ids:
        # Fetch the IDs of resources erroneously marked as watching
        cursor.execute("SELECT resource_id FROM resources WHERE is_watching = TRUE AND resource_id NOT IN ({})".format(','.join('?' * len(all_resource_ids))), tuple(all_resource_ids))
        erroneously_watching_resources = cursor.fetchall()
        if erroneously_watching_resources:
            print(f"Resources erroneously marked as watching: {[res[0] for res in erroneously_watching_resources]}")

        # Perform the update to correct the is_watching flag
        cursor.execute("UPDATE resources SET is_watching = FALSE WHERE resource_id NOT IN ({})".format(','.join('?' * len(all_resource_ids))), tuple(all_resource_ids))

    # Delete licensed resources that aren't downloadable (not in all_resource_ids, with a non-null lic_start_date)
    if all_resource_ids:
        cursor.execute("SELECT resource_id FROM resources WHERE lic_start_date IS NOT NULL AND resource_id NOT IN ({})".format(','.join('?' * len(all_resource_ids))), tuple(all_resource_ids))
        resources_to_delete = cursor.fetchall()
        if resources_to_delete:
            print(f"Licensed resources to be removed from db: {[res[0] for res in resources_to_delete]}")

        cursor.execute("DELETE FROM resources WHERE lic_start_date IS NOT NULL AND resource_id NOT IN ({})".format(','.join('?' * len(all_resource_ids))), tuple(all_resource_ids))
    
    # Delete special resources that are not in current_ids
    if all_resource_ids:
        cursor.execute("SELECT resource_id FROM resources WHERE is_special = TRUE AND resource_id NOT IN ({})".format(','.join('?' * len(all_resource_ids))), tuple(all_resource_ids))
        special_resources_to_delete = cursor.fetchall()
        if special_resources_to_delete:
            print(f"Special resources to be removed from db: {[res[0] for res in special_resources_to_delete]}")
        cursor.execute("DELETE FROM resources WHERE is_special = TRUE AND resource_id NOT IN ({})".format(','.join('?' * len(all_resource_ids))), tuple(all_resource_ids))

    # Clean up dependencies table by deleting entries not in current_ids
    if dependency_ids:
        # Fetch the IDs of dependencies about to be deleted for logging
        placeholders = ', '.join(['(?, ?)'] * len(dependency_ids))
        cursor.execute(f"SELECT parent_resource_id, dependency_resource_id FROM dependencies WHERE (parent_resource_id, dependency_resource_id) NOT IN ({placeholders})", [item for sublist in dependency_ids for item in sublist])
        dependencies_to_delete = cursor.fetchall()
        if dependencies_to_delete:
            print(f"Dependencies to be removed from db: {dependencies_to_delete}")
        # Perform the deletion
        cursor.execute(f"DELETE FROM dependencies WHERE (parent_resource_id, dependency_resource_id) NOT IN ({placeholders})", [item for sublist in dependency_ids for item in sublist])

    # As the final function in the update logic, we'll set last run time here.
    cursor.execute("UPDATE metadata SET last_fetch_time = ? WHERE id = 1", (int(time.time()),))

def reset_download_dates(cursor):
    cursor.execute("UPDATE resources SET local_version = 0")
    cursor.execute("UPDATE dependencies SET local_version = 0")
    print("Download dates reset. All resources will be re-downloaded.")

def reset_download_date_for_resource(cursor, resource_id):
    # Reset in the resources table
    cursor.execute("UPDATE resources SET local_version = 0 WHERE resource_id = ?", (resource_id,))
    if cursor.rowcount > 0:
        print(f"Resource {resource_id} will be re-downloaded.")

    # Reset in the dependencies table where the resource is a parent
    cursor.execute("UPDATE dependencies SET local_version = 0 WHERE parent_resource_id = ?", (resource_id,))

def fetch_dependencies_for_parent(parent_resource_id, cursor):
    """Fetch detailed information for all dependencies of a specific parent resource."""
    cursor.execute("""
        SELECT d.dependency_resource_id as resource_id, r.parent_category_id, d.remote_version, d.local_version, 
               d.parent_resource_id, d.download_url, d.filename
        FROM dependencies d
        JOIN resources r ON d.parent_resource_id = r.resource_id
        WHERE d.parent_resource_id = ?
    """, (parent_resource_id,))
    return cursor.fetchall()

def fetch_single_resource_details(resource_id, cursor):
    """Fetch the detailed information about a single resource from the database."""
    cursor.execute("""
        SELECT resource_id, parent_category_id, remote_version, local_version, NULL as parent_resource_id, download_url, filename
        FROM resources
        WHERE resource_id = ?
    """, (resource_id,))
    return cursor.fetchone()

def fetch_watched_resource_details(cursor):
    """Fetch detailed information about resources from the database that are either watched, special, or have a license start date."""
    cursor.execute("""
        SELECT resource_id, parent_category_id, remote_version, local_version, NULL as parent_resource_id, download_url, filename
        FROM resources
        WHERE is_watching = TRUE OR is_special = TRUE OR lic_start_date IS NOT NULL
    """)
    return cursor.fetchall()

def fetch_all_dependency_details(cursor):
    """Fetch detailed information about dependencies from the database."""
    cursor.execute("""
        SELECT d.dependency_resource_id as resource_id, r.parent_category_id, d.remote_version, d.local_version, d.parent_resource_id, d.download_url, d.filename
        FROM dependencies d
        JOIN resources r ON d.parent_resource_id = r.resource_id
    """)
    return cursor.fetchall()

def fetch_watched_db_resources(cursor):
    """Fetch IDs of watched resources and dependencies from the db."""
    resources = fetch_watched_resource_details(cursor)
    dependencies = fetch_all_dependency_details(cursor)
    return resources + dependencies

def fetch_single_db_resource(resource_id, cursor):
    """Fetch detailed information about a single resource and all its dependencies from the database."""
    resource = fetch_single_resource_details(resource_id, cursor)
    dependencies = fetch_dependencies_for_parent(resource_id, cursor)
    
    # Ensure resource is a list before concatenation.
    resource_list = [resource] if resource else []
    return resource_list + dependencies

def update_download_date(resource_id, remote_version, is_dependency=False, parent_resource_id=None, cursor=None):
    if cursor is None:
        raise ValueError("Database cursor is not provided")
    if is_dependency:
        # Update the local_version for a dependency
        cursor.execute("""
            UPDATE dependencies
            SET local_version = ?
            WHERE parent_resource_id = ? AND dependency_resource_id = ?""",
            (remote_version, parent_resource_id, resource_id))
    else:
        # Update the local_version for a normal resource
        cursor.execute("""
            UPDATE resources
            SET local_version = ?
            WHERE resource_id = ?""",
            (remote_version, resource_id))
        
def list_resources(cursor):
    cursor.execute("SELECT resource_id, title FROM resources")
    resources = cursor.fetchall()
    print("Resources:")
    for resource_id, title in resources:
        print(f"ID: {resource_id}, Title: {title}")

def list_dependencies(cursor):
    cursor.execute("SELECT dependency_resource_id, title FROM dependencies")
    dependencies = cursor.fetchall()
    print("Dependencies:")
    for resource_id, title in dependencies:
        print(f"ID: {resource_id}, Title: {title}")

def get_resource_title(cursor, resource_id):
    """Get the title for a resource ID from either resources or dependencies table."""
    # Try resources table first
    cursor.execute("""
        SELECT title FROM resources 
        WHERE resource_id = ?
    """, (resource_id,))
    result = cursor.fetchone()
    
    if result:
        return result[0]
        
    # If not found, try dependencies table
    cursor.execute("""
        SELECT title FROM dependencies 
        WHERE dependency_resource_id = ?
    """, (resource_id,))
    result = cursor.fetchone()
    
    return result[0] if result else None