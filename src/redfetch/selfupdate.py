import sys
import os
import subprocess
import api
from __about__ import __version__
from packaging import version

#todo: make sure we can use __version__ in pyinstaller

REDFETCH_RESOURCE_ID = "1337"

def get_current_version():
    return __version__

def check_for_update():
    current_version = get_current_version()
    headers = api.get_api_headers()
    
    try:
        versions_info = api.fetch_versions_info(REDFETCH_RESOURCE_ID, headers)
        if versions_info and 'versions' in versions_info and versions_info['versions']:
            latest_version = versions_info['versions'][0]['version_string']
            
            if version.parse(latest_version) > version.parse(current_version):
                print(f"\nAn update for RedFetch is available! 🚡")
                print(f"Local version: {current_version}")
                print(f"Latest version: {latest_version}")
                
                user_input = input("Would you like to update now via pip? (y/n): ").lower().strip()
                if user_input == 'y':
                    return update_redfetch(latest_version)
                else:
                    print("Update skipped. You can manually update later.")
                    print("You can download the latest version from: https://www.redguides.com/community/resources/redfetch.1337/")
    except Exception as e:
        print(f"Error checking for updates: {e}")
    
    return False

def update_redfetch(latest_version):
    try:
        # Get the directory of the current script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Construct the update command using TestPyPI
        update_command = [
            sys.executable, 
            '-m', 
            'pip', 
            'install', 
            '--upgrade', 
            '--index-url', 'https://test.pypi.org/simple/', 
            '--extra-index-url', 'https://pypi.org/simple/', # remove for production
            'redfetch'
        ]
        
        print(f"Updating RedFetch to version {latest_version} in {script_dir}")
        
        # Run the update command
        result = subprocess.run(update_command, capture_output=True, text=True)
        
        if result.returncode == 0:
            print("RedFetch has been successfully updated. 🫎")
            return True
        else:
            print(f"Error updating RedFetch: {result.stderr}")
            print("You can manually download the latest version from: https://www.redguides.com/community/resources/redfetch.1337/")
            return False
    except Exception as e:
        print(f"Error during update process: {e}")
        print("You can manually download the latest version from: https://www.redguides.com/community/resources/redfetch.1337/")
        return False
