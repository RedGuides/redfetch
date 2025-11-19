import os
import sys

# Only import winreg on Windows
if sys.platform == 'win32':
    import winreg as reg
else:
    reg = None


def _extract_dir_from_value(raw_value: str) -> str:
    """Turn uninstall/display value into a directory."""
    if not raw_value:
        return ""

    s = str(raw_value).strip()
    # Common DisplayIcon form: C:\...\eqgame.exe,0 -> strip icon index
    if "," in s and s.lower().split(",", 1)[0].endswith(".exe"):
        s = s.split(",", 1)[0].strip()

    # Remove surrounding quotes if present
    s = s.strip().strip('"').strip()

    lower = s.lower()
    exe_idx = lower.find(".exe")
    if exe_idx != -1:
        exe_path = s[: exe_idx + 4].strip().strip('"')
        return os.path.dirname(exe_path)

    # Assume it's a directory already
    return s


def _is_valid_eq_dir(path: str) -> bool:
    if not path:
        return False
    eqgame_path = os.path.join(path, "eqgame.exe")
    return os.path.isfile(eqgame_path)


def _read_reg_value(hive_or_handle, subkey: str, name: str, view: int = 0):
    """Read a registry value safely; view can be 0/32/64."""
    if reg is None:
        return None

    access = reg.KEY_READ
    if view == 32 and hasattr(reg, "KEY_WOW64_32KEY"):
        access |= reg.KEY_WOW64_32KEY
    if view == 64 and hasattr(reg, "KEY_WOW64_64KEY"):
        access |= reg.KEY_WOW64_64KEY

    try:
        with reg.OpenKey(hive_or_handle, subkey, 0, access) as k:
            return reg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def find_everquest_uninstall_location():
    """Return EverQuest install dir if found, else None."""
    # Return None immediately if not on Windows
    if sys.platform != 'win32' or reg is None:
        return None

    base_uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\DGC-EverQuest"

    # 1) HKCU: prefer per-user installation
    try:
        with reg.ConnectRegistry(None, reg.HKEY_CURRENT_USER) as hkcu:
            for valname in ("InstallLocation", "DisplayIcon", "UninstallString"):
                val = _read_reg_value(hkcu, base_uninstall, valname)
                if val:
                    candidate = _extract_dir_from_value(str(val))
                    if _is_valid_eq_dir(candidate):
                        return candidate
    except OSError:
        pass

    # 2) HKLM: system-wide installation (try both 64/32 views)
    try:
        with reg.ConnectRegistry(None, reg.HKEY_LOCAL_MACHINE) as hklm:
            for view in (64, 32):
                for valname in ("InstallLocation", "DisplayIcon", "UninstallString"):
                    val = _read_reg_value(hklm, base_uninstall, valname, view=view)
                    if val:
                        candidate = _extract_dir_from_value(str(val))
                        if _is_valid_eq_dir(candidate):
                            return candidate
    except OSError:
        pass

    # 3) HKU: enumerate SIDs as final fallback
    try:
        with reg.ConnectRegistry(None, reg.HKEY_USERS) as hku:
            i = 0
            while True:
                try:
                    sid = reg.EnumKey(hku, i)
                    i += 1
                    subkey = rf"{sid}\{base_uninstall}"
                    for valname in ("InstallLocation", "DisplayIcon", "UninstallString"):
                        val = _read_reg_value(hku, subkey, valname)
                        if val:
                            candidate = _extract_dir_from_value(str(val))
                            if _is_valid_eq_dir(candidate):
                                return candidate
                except OSError:
                    break
    except OSError:
        pass

    return None


if __name__ == "__main__":
    print(find_everquest_uninstall_location() or "")
