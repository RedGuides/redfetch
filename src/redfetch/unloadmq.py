import sys
import ctypes
import ctypes.wintypes
import psutil
import os
from contextlib import contextmanager

if sys.platform == 'win32':
    import win32api
    import win32process

    # Constants
    PROCESS_ALL_ACCESS = 0x1F0FFF
    INFINITE = 0xFFFFFFFF

    # Load kernel32.dll
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    @contextmanager
    def open_process(pid, access):
        try:
            h_process = win32api.OpenProcess(access, False, pid)
            if not h_process:
                raise ctypes.WinError(ctypes.get_last_error())
            yield h_process
        except Exception as e:
            print(f"Failed to open process {pid}: {e}")
            yield None
        finally:
            if 'h_process' in locals() and h_process:
                win32api.CloseHandle(h_process)

    def get_eqgame_process_pids():
        """Get all PIDs of running EverQuest processes."""
        pids = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if proc.info['name'].lower() == 'eqgame.exe':
                    pids.append(proc.info['pid'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    def get_module_base_address(process_handle, module_name):
        """Get the base address of the specified module in the remote process."""
        try:
            modules = win32process.EnumProcessModules(process_handle)
            for module in modules:
                module_filename = win32process.GetModuleFileNameEx(process_handle, module)
                if os.path.basename(module_filename).lower() == module_name.lower():
                    return module, module_filename  # Return both module handle and full path
            print(f"{module_name} not found in the process.")
            return None, None
        except Exception as e:
            print(f"Failed to enumerate modules in the remote process: {e}")
            return None, None

    def get_remote_function_address(process_handle, module_path, module_base_address, function_name):
        """Get the address of the function in the remote process."""
        try:
            if not os.path.exists(module_path):
                print(f"Local module not found: {module_path}")
                return None

            # Load the module locally using the full path
            local_module = ctypes.WinDLL(module_path)
        except Exception as e:
            print(f"Failed to load {module_path} locally: {e}")
            return None

        try:
            func_address_local = getattr(local_module, function_name)
        except AttributeError:
            print(f"Function {function_name} not found in {os.path.basename(module_path)}.")
            return None

        offset = ctypes.cast(func_address_local, ctypes.c_void_p).value - local_module._handle
        func_address_remote = module_base_address + offset
        return func_address_remote

    def force_remote_unload_mq2(pid):
        with open_process(pid, PROCESS_ALL_ACCESS) as process_handle:
            if not process_handle:
                print(f"Skipping process {pid} due to insufficient permissions or other errors.")
                return

            module_handle, module_path = get_module_base_address(process_handle, 'mq2main.dll')
            if not module_handle or not module_path:
                print(f"mq2main.dll not found in process {pid}. MacroQuest may not be loaded.")
                return

            func_address = get_remote_function_address(
                process_handle, module_path, module_handle, 'MQ2End')
            if not func_address:
                print(f"MQ2End function not found in process {pid}.")
                return

            try:
                # Create a remote thread to execute MQ2End
                hThread = kernel32.CreateRemoteThread(
                    process_handle.handle,
                    None,
                    0,
                    ctypes.c_void_p(func_address),
                    None,
                    0,
                    None
                )

                if not hThread:
                    error_code = ctypes.get_last_error()
                    raise ctypes.WinError(error_code)

                kernel32.WaitForSingleObject(hThread, 20000)
                kernel32.CloseHandle(hThread)
                print(f"Successfully unloaded MacroQuest from process {pid}.")
            except Exception as e:
                print(f"Failed to unload MacroQuest from process {pid}: {e}")

    def force_remote_unload():
        pids = get_eqgame_process_pids()
        if not pids:
            return

        for pid in pids:
            force_remote_unload_mq2(pid)

else:
    def force_remote_unload():
        print("unloading MacroQuest is not supported on this platform.")

if __name__ == '__main__':
    force_remote_unload()