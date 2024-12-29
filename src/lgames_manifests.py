from asyncio.log import logger
from enum import Flag
import os
import platform
if platform.system() == "Windows":
    from ctypes import byref, sizeof, windll, create_unicode_buffer, FormatError, WinError
    from ctypes.wintypes import DWORD
    from typing import Optional, Set, List
else:
    import psutil
from typing import Iterator, List, Optional, Set, Tuple
import winreg
import xml.etree.ElementTree as ET

from galaxy.api.types import (
     LocalGame, LocalGameState
)

# Helpers for the Local Games data

class EAGameState(Flag):
    None_ = 0
    Installed = 1
    Playable = 2

def parse_total_size(filepath) -> int:
    if not filepath:
        return 0
    return sum(os.path.getsize(os.path.join(dirpath, f))
               for dirpath, _, filenames in os.walk(filepath)
               for f in filenames)


def get_state_changes(old_list, new_list):
    old_dict = {x.game_id: x.local_game_state for x in old_list}
    new_dict = {x.game_id: x.local_game_state for x in new_list}
    result = []
    # removed games
    result.extend(LocalGame(game_id, LocalGameState.None_) for game_id in old_dict.keys() - new_dict.keys())
    # added games
    result.extend(local_game for local_game in new_list if local_game.game_id in new_dict.keys() - old_dict.keys())
    # state changed
    result.extend(
        LocalGame(game_id, new_dict[game_id])
        for game_id in new_dict.keys() & old_dict.keys()
        if new_dict[game_id] != old_dict[game_id]
    )
    return result


def get_python_path():
    platform_id = platform.system()
    python_path = ""
    if platform_id == "Windows":
        reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)

        keyname = winreg.OpenKey(reg, r'SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths')
        for i in range(1024):
            try:
                valname = winreg.EnumKey(keyname, i)
                open_key = winreg.OpenKey(keyname, valname)
                python_path = winreg.QueryValueEx(open_key, "client")
            except EnvironmentError:
                break
    else:
        python_path = ""  # fallback for testing on another platform
        # raise NotImplementedError("Not implemented on {}".format(platform_id))

    return python_path


def get_local_content_path():
    platform_id = platform.system()
    if platform_id == "Windows":
        local_content_path = os.path.join(os.environ.get("ProgramData", os.environ.get("SystemDrive", "C:") + R"\ProgramData"), "EA Desktop", "InstallData")
    elif platform_id == "Darwin":
        local_content_path = os.path.join(os.sep, "Library", "Application Support", "EA Desktop", "InstallData")
    else:
        local_content_path = "."  # fallback for testing on another platform
        # raise NotImplementedError("Not implemented on {}".format(platform_id))

    return local_content_path


if platform.system() == "Windows":
    def get_process_info(pid) -> Tuple[int, Optional[str]]:
        _MAX_PATH = 260
        _PROC_QUERY_LIMITED_INFORMATION = 0x1000
        _WIN32_PATH_FORMAT = 0x0000

        h_process = windll.kernel32.OpenProcess(_PROC_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_process:
            return pid, None

        def get_process_file_name() -> Optional[str]:
            try:
                file_name_buffer = create_unicode_buffer(_MAX_PATH)
                file_name_len = DWORD(len(file_name_buffer))

                return file_name_buffer[:file_name_len.value] if windll.kernel32.QueryFullProcessImageNameW(
                    h_process, _WIN32_PATH_FORMAT, file_name_buffer, byref(file_name_len)
                ) else None

            finally:
                windll.kernel32.CloseHandle(h_process)

        return pid, get_process_file_name()


    def get_process_ids() -> Set[int]:
        _PROC_ID_T = DWORD
        list_size = 4096

        def try_get_info_list(list_size) -> Tuple[int, List[int]]:
            result_size = DWORD()
            proc_id_list = (_PROC_ID_T * list_size)()

            if not windll.psapi.EnumProcesses(byref(proc_id_list), sizeof(proc_id_list), byref(result_size)):
                raise WinError(descr="Failed to get process ID list: %s" % FormatError())

            size = int(result_size.value / sizeof(_PROC_ID_T()))
            return proc_id_list[:size]

        while True:
            proc_id_list = try_get_info_list(list_size)
            if len(proc_id_list) < list_size:
                return proc_id_list
            # if returned collection is not smaller than list size it indicates that some pids have not fitted
            list_size *= 2

        return set(proc_id_list)


    def process_iter() -> Iterator[Tuple[int, str]]:
        try:
            for pid in get_process_ids():
                yield get_process_info(pid)
        except OSError:
            logger.exception("Failed to iterate over the process list")
            pass

else:
    def process_iter() -> Iterator[Tuple[int, str]]:
        for pid in psutil.pids():
            try:
                yield pid, psutil.Process(pid=pid).as_dict(attrs=["exe"])["exe"]
            except psutil.NoSuchProcess:
                pass
            except StopIteration:
                raise
            except Exception:
                logger.exception("Failed to get information for PID=%s" % pid)


def get_install_location_rkeyxml(base_key=None, regkey_path=None, part=None):
    """Get install location from registry or XML manifest"""
    # If called with single argument, treat as full path
    if regkey_path is None and part is None:
        installer_path = os.path.join(os.path.dirname(os.path.dirname(base_key)), "__Installer", "installerdata.xml")
        return parse_install_manifest(installer_path)

    # Otherwise handle as registry lookup with fallback to XML
    try:
        with winreg.OpenKey(base_key, regkey_path) as key:
            install_location, _ = winreg.QueryValueEx(key, part)
            if install_location and os.path.exists(install_location):
                return install_location
    except Exception as e:
        logger.debug(f"Registry lookup failed: {str(e)}")

    # Try XML manifest
    try:
        installer_path = os.path.join(os.path.dirname(os.path.dirname(regkey_path)), "__Installer", "installerdata.xml")
        install_path = parse_install_manifest(installer_path)
        if install_path and os.path.exists(install_path):
            return install_path
    except Exception as e:
        logger.debug(f"XML manifest lookup failed: {str(e)}")

    return None
    
def parse_install_manifest(installer_path: str) -> Optional[str]:
    """Parse installerdata.xml to find the game install path"""
    try:
        if not os.path.exists(installer_path):
            return None
            
        tree = ET.parse(installer_path)
        root = tree.getroot()
        
        # Find DiPManifest/installManifest/filePath 
        manifest = root.find(".//DiPManifest/installManifest/filePath")
        if manifest is not None:
            return manifest.text
        return None
    except Exception as e:
        logger.error(f"Error parsing installer manifest: {str(e)}")
        return None