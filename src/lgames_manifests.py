import os
import platform
import logging
import xml.etree.ElementTree as ET
from enum import Flag
import subprocess
from typing import Iterator, List, Optional, Set, Tuple, Dict, Any
from functools import lru_cache
from pathlib import Path

# Configure logger
logger = logging.getLogger(__name__)

# Import platform-specific modules
if platform.system() == "Windows":
    try:
        import winreg
        from ctypes import byref, sizeof, windll, create_unicode_buffer, FormatError, WinError
        from ctypes.wintypes import DWORD
    except ImportError as e:
        logger.error(f"Failed to import Windows-specific modules: {str(e)}")
else:
    try:
        import psutil
    except ImportError:
        logger.warning("psutil module not available. Process detection might be limited.")
    
    if platform.system() == "Darwin":
        try:
            from AppKit import NSWorkspace
        except ImportError:
            logger.warning("AppKit module not available. Some macOS functionality might be limited.")

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
    try:
        return sum(os.path.getsize(os.path.join(dirpath, f))
                for dirpath, _, filenames in os.walk(filepath)
                for f in filenames)
    except (OSError, FileNotFoundError) as e:
        logger.error(f"Error calculating directory size: {str(e)}")
        return 0


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
        try:
            reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
            keyname = winreg.OpenKey(reg, r'SOFTWARE\WOW6432Node\GOG.com\GalaxyClient\paths')
            for i in range(1024):
                try:
                    valname = winreg.EnumKey(keyname, i)
                    open_key = winreg.OpenKey(keyname, valname)
                    python_path = winreg.QueryValueEx(open_key, "client")
                except EnvironmentError:
                    break
        except Exception as e:
            logger.error(f"Failed to get Python path from Windows registry: {str(e)}")
    elif platform_id == "Darwin":
        # macOS implementation - typically in ~/Library/Application Support/GOG.com/Galaxy
        home = os.path.expanduser("~")
        possible_paths = [
            os.path.join(home, "Library", "Application Support", "GOG.com", "Galaxy"),
            "/Applications/GOG Galaxy.app/Contents/Resources"
        ]
        for path in possible_paths:
            if os.path.exists(path):
                python_path = path
                break
    else:
        python_path = ""  # fallback for testing on another platform

    return python_path


def get_local_content_path():
    platform_id = platform.system()
    if platform_id == "Windows":
        local_content_path = os.path.join(os.environ.get("ProgramData", os.environ.get("SystemDrive", "C:") + R"\ProgramData"), "EA Desktop", "InstallData")
    elif platform_id == "Darwin":
        # First check user's home directory for EA Desktop data
        home = os.path.expanduser("~")
        paths = [
            os.path.join(home, "Library", "Application Support", "EA Desktop", "InstallData"),
            os.path.join(os.sep, "Library", "Application Support", "EA Desktop", "InstallData"),
            os.path.join(home, "Library", "Application Support", "Electronic Arts", "EA Desktop", "InstallData")
        ]
        
        for path in paths:
            if os.path.exists(path):
                return path
                
        # Default fallback path
        local_content_path = os.path.join(home, "Library", "Application Support", "EA Desktop", "InstallData")
    else:
        local_content_path = "."  # fallback for testing on another platform

    return local_content_path


@lru_cache(maxsize=128)
def find_game_executables(game_path: str) -> List[str]:
    """Find potential game executables in the given path with caching for performance"""
    if not game_path or not os.path.exists(game_path):
        return []
        
    executables = []
    extensions = ['.exe'] if platform.system() == "Windows" else ['.app', '']
    
    # Utiliser Path pour une manipulation plus simple des chemins
    path_obj = Path(game_path)
    
    for item in path_obj.glob('**/*'):
        if item.is_file():
            if any(item.name.endswith(ext) for ext in extensions):
                # Pour macOS, vérifier si c'est une application
                if platform.system() == "Darwin" and item.name.endswith('.app'):
                    if item.is_dir():
                        executables.append(str(item))
                elif os.access(str(item), os.X_OK):
                    executables.append(str(item))
    
    return executables


def game_is_running_by_path(game_path: str) -> bool:
    """Check if a game is running based on its installation path with optimized process matching"""
    if not game_path:
        return False
        
    executables = find_game_executables(game_path)
    if not executables:
        logger.debug(f"No executables found in path: {game_path}")
        return False
    
    # Get the directory name from the game path to check for launcher processes
    game_dir_name = os.path.basename(os.path.normpath(game_path)).lower()
    
    # Prépare les noms à vérifier une seule fois (optimisation)
    check_names = set()
    exec_bases = set()
    
    for executable in executables:
        exe_name = os.path.basename(executable).lower()
        exe_name_no_ext = os.path.splitext(exe_name)[0].lower()
        
        check_names.add(exe_name)
        check_names.add(exe_name_no_ext)
        check_names.add(executable.lower())
        check_names.add(f"{exe_name_no_ext}.exe")
        exec_bases.add(exe_name_no_ext)
        
    # Obtient les processus seulement une fois
    running_processes = {exe.lower() for _, exe in process_iter() if exe}
    
    # Test direct sur les noms exacts (plus rapide)
    for proc in running_processes:
        # 1. Vérification exacte (plus rapide)
        if proc in check_names:
            logger.info(f"Found running process exact match: {proc}")
            return True
            
        # 2. Vérification des fins de chemins
        proc_name = os.path.basename(proc)
        if proc_name in check_names:
            logger.info(f"Found running process filename match: {proc_name}")
            return True
            
        # 3. Vérification du nom du jeu dans le processus
        if proc.endswith('.exe'):
            for base in exec_bases:
                if base in proc or game_dir_name in proc:
                    logger.info(f"Found running process containing game name: {proc} contains {base} or {game_dir_name}")
                    return True
            
            # 4. Vérification des processus avec paramètres
            if ' ' in proc:
                proc_base = proc.split(' ')[0].lower()
                if os.path.basename(proc_base) in check_names:
                    logger.info(f"Found running process match with parameters: {proc} with base {proc_base}")
                    return True
    
    return False


def discover_games_from_offer_cache(offer_cache: Dict[str, Any]) -> List[LocalGame]:
    """Discover installed games using the offer cache data"""
    if not offer_cache:
        return []
        
    local_games = []
    
    for offer_id, game_data in offer_cache.items():
        state = LocalGameState.None_
        # Check locations in game_data
        install_path = None
        
        # Look for various location indicators in the game data
        for key in ["installPath", "installLocation", "path", "installCheckOverride", "executePathOverride"]:
            if key in game_data and game_data[key] and os.path.exists(game_data[key]):
                install_path = game_data[key]
                break
        
        # If there's an install path, game is installed
        if install_path:
            state = LocalGameState.Installed
            # Check if the game is running
            if game_is_running_by_path(install_path):
                state |= LocalGameState.Running
                
        local_games.append(LocalGame(offer_id, state))
        
    return local_games


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

        def try_get_info_list(list_size) -> List[int]:
            result_size = DWORD()
            proc_id_list = (_PROC_ID_T * list_size)()

            if not windll.psapi.EnumProcesses(byref(proc_id_list), sizeof(proc_id_list), byref(result_size)):
                raise WinError(descr="Failed to get process ID list: %s" % FormatError())

            size = int(result_size.value / sizeof(_PROC_ID_T()))
            return proc_id_list[:size]

        while True:
            proc_id_list = try_get_info_list(list_size)
            if len(proc_id_list) < list_size:
                return set(proc_id_list)
            # if returned collection is not smaller than list size it indicates that some pids have not fitted
            list_size *= 2


    def process_iter() -> Iterator[Tuple[int, str]]:
        try:
            for pid in get_process_ids():
                yield get_process_info(pid)
        except OSError:
            logger.exception("Failed to iterate over the process list")
            pass

elif platform.system() == "Darwin":
    @lru_cache(maxsize=32)
    def get_macos_running_apps() -> Dict[str, str]:
        """Get dictionary of running applications on macOS with caching for performance"""
        try:
            if 'NSWorkspace' in globals():
                running_apps = {}
                for app in NSWorkspace.sharedWorkspace().runningApplications():
                    if app.bundleIdentifier() and app.executableURL():
                        path = app.executableURL().path()
                        if path:
                            running_apps[app.bundleIdentifier()] = path
                return running_apps
            else:
                # Fallback to command line with optimized command
                result = subprocess.run(['ps', '-eo', 'comm'], capture_output=True, text=True)
                processes = {}
                for line in result.stdout.splitlines()[1:]:  # Skip header
                    if line.strip():
                        proc_name = line.strip()
                        processes[proc_name] = proc_name
                return processes
        except Exception as e:
            logger.error(f"Failed to get macOS running applications: {str(e)}")
            return {}

    def process_iter() -> Iterator[Tuple[int, str]]:
        try:
            # First try psutil
            for proc in psutil.process_iter(['pid', 'exe']):
                try:
                    proc_info = proc.info
                    yield proc_info['pid'], proc_info['exe']
                except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                    pass
        except Exception as e:
            logger.error(f"Error using psutil on macOS: {str(e)}")
            # Fallback to ps command
            try:
                result = subprocess.run(['ps', '-eo', 'pid,comm'], capture_output=True, text=True)
                for line in result.stdout.splitlines()[1:]:  # Skip header
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2:
                        try:
                            pid = int(parts[0])
                            process_path = parts[1]
                            yield pid, process_path
                        except ValueError:
                            pass
            except Exception as e:
                logger.error(f"Failed to get process information via ps command: {str(e)}")

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

    # Windows registry handling
    if platform.system() == "Windows":
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
    
@lru_cache(maxsize=64)
def parse_install_manifest(installer_path: str) -> Optional[str]:
    """Parse installerdata.xml to find the game install path with caching for better performance"""
    try:
        if not os.path.exists(installer_path):
            return None
            
        # Utiliser Path pour une meilleure gestion des chemins
        path_obj = Path(installer_path)
        if not path_obj.exists() or not path_obj.is_file():
            return None
            
        tree = ET.parse(installer_path)
        root = tree.getroot()
        
        # Find DiPManifest/installManifest/filePath with optimized xpath
        manifest = root.find(".//DiPManifest/installManifest/filePath")
        if manifest is not None:
            return manifest.text
        return None
    except Exception as e:
        logger.error(f"Error parsing installer manifest: {str(e)}")
        return None