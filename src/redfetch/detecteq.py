import sys

# Only import winreg on Windows
if sys.platform == 'win32':
    import winreg as reg
else:
    reg = None

def find_everquest_uninstall_location():
    # Return None immediately if not on Windows
    if sys.platform != 'win32':
        return None
        
    import os
    uninstall_path = None
    base_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

    # First, try HKEY_CURRENT_USER to prioritize the current user's EverQuest
    with reg.ConnectRegistry(None, reg.HKEY_CURRENT_USER) as hkey_current_user:
        everquest_key_path = rf"{base_key}\DGC-EverQuest"
        try:
            with reg.OpenKey(hkey_current_user, everquest_key_path) as everquest_key:
                uninstall_path, _ = reg.QueryValueEx(everquest_key, "UninstallString")
        except FileNotFoundError:
            pass  # Not found under current user

    # If not found under HKEY_CURRENT_USER, search under HKEY_USERS
    if not uninstall_path:
        with reg.ConnectRegistry(None, reg.HKEY_USERS) as hkey_users:
            # Enumerate through HKEY_USERS
            i = 0
            while True:
                try:
                    # Get each subkey name (user SID)
                    sid = reg.EnumKey(hkey_users, i)
                    i += 1

                    # Try to access the EverQuest uninstall path under this SID
                    everquest_key_path = rf"{sid}\{base_key}\DGC-EverQuest"
                    try:
                        with reg.OpenKey(hkey_users, everquest_key_path) as everquest_key:
                            uninstall_path, _ = reg.QueryValueEx(everquest_key, "UninstallString")
                            break  # If found, exit the loop
                    except FileNotFoundError:
                        # EverQuest key not found under this SID, continue
                        continue

                except OSError:
                    # No more SIDs to enumerate
                    break

    if uninstall_path:
        # Remove 'Uninstaller.exe' from the end of the path
        install_dir = os.path.dirname(uninstall_path)

        # Verify that 'eqgame.exe' exists in this directory
        eqgame_path = os.path.join(install_dir, 'eqgame.exe')
        if os.path.isfile(eqgame_path):
            #print("EverQuest install path found:", install_dir)
            return install_dir

    return None

# Run the function
everquest_install_path = find_everquest_uninstall_location()
