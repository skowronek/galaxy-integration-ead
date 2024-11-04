###
#----------------------------------------------------PC SIGN----------------------------------------------------#
# EA Desktop way of linking your login info to your PC. This is a hash of your hardware info and a timestamp.
# The hash is signed with a secret key to prevent tampering. The server can verify the hash with the secret key.
# The server can also generate the hash itself and compare it to the one sent by the client.
# It, then, can decide if the client is allowed to log in.
#---------------------------------------------------------------------------------------------------------------#
# Kudos to @imLinguin for the necessary info.
###

import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
import json
import base64
import hmac
import hashlib
import datetime

class PCSignVersion(Enum):
    V1 = "v1"
    V2 = "v2"

@dataclass
class PCSign:
    bsn: str = field(init=False)  # BIOS Serial Number
    gid: int = field(init=False)  # GPU device ID
    hsn: str = field(init=False)  # Disk Serial Number
    mac: Optional[str] = None  # MAC Address
    mid: str = field(init=False)  # FNV1a hash of hardware info
    msn: str = field(init=False)  # Motherboard Serial Number
    ts: str = field(init=False)  # Timestamp

    sv: PCSignVersion = None # Secret Key Version
    av: str = "v1"  # Always v1

    def __post_init__(self):
        self.gather_hardware_info()
        self.mid = self.calculate_fnv1a_hash()
        self.ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S:%f")[:-3]

    def gather_hardware_info(self):
        if os.name == 'nt':  # Windows
            self.gather_windows_info()
        elif os.name == 'posix':  # macOS/Linux
            self.gather_macos_info()
        else:
            raise OSError("Unsupported operating system")

    def run_wmic(self, command):
        return subprocess.check_output(f"wmic {command}", shell=True).decode().split('\n')[1]

    def gather_windows_info(self):
        self.bsn = self.run_wmic("bios get serialnumber").strip()
        self.gid = int(self.run_wmic("path win32_videocontroller get pnpdeviceid").split('DEV_')[1].split('&')[0], 16)
        self.hsn = self.run_wmic("diskdrive get serialnumber").strip()
        self.msn = self.run_wmic("baseboard get serialnumber").strip()
        self.mac = self.run_wmic("nic where physicaladapter=true get macaddress").strip()

    def gather_macos_info(self):
        self.bsn = self.run_macos_command("system_profiler SPHardwareDataType | awk '/Serial Number/' | cut -d: -f2").strip()
        self.gid = int(self.run_macos_command("system_profiler SPDisplaysDataType | awk '/Device ID:/' | cut -d: -f2").strip(), 16)
        self.hsn = self.run_macos_command("diskutil info /dev/disk0 | awk '/Device Identifier:/' | cut -d: -f2").strip()
        self.msn = self.run_macos_command("system_profiler SPHardwareDataType | awk '/Hardware UUID:/' | cut -d: -f2").strip()
        self.mac = self.run_macos_command("ifconfig en0 | awk '/ether/' | cut -d' ' -f2").strip()

    def run_macos_command(self, command):
        return subprocess.check_output(command, shell=True).decode().strip()

    @staticmethod
    def hash_fnv1a(input_bytes: bytes) -> int:
        OFFSET = 0xcbf29ce484222325
        FNV_PRIME = 0x100000001b3
        hash_value = OFFSET
        for byte in input_bytes:
            hash_value ^= byte
            hash_value = (hash_value * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF  # Ensure 64-bit
        return hash_value

    def calculate_fnv1a_hash(self) -> str:
        hardware_info = f"{self.bsn}{self.gid}{self.hsn}{self.msn}".encode()
        hash_value = self.hash_fnv1a(hardware_info)
        return format(hash_value, '016x')  # Convert to 16-character hex string

    def sign_key(self) -> bytes:
        if self.sv == PCSignVersion.V2:
            return b"nt5FfJbdPzNcl2pkC3zgjO43Knvscxft"
        elif self.sv == PCSignVersion.V1:
            return b"ISa3dpGOc8wW7Adn4auACSQmaccrOyR2"
        else:
            raise ValueError(f"Unsupported PCSignVersion: {self.sv}")

    def to_dict(self):
        result = {
            "av": self.av,
            "bsn": self.bsn,
            "gid": self.gid,
            "hsn": self.hsn,
            "mid": self.mid,
            "msn": self.msn,
            "sv": self.sv.value,
            "ts": self.ts
        }
        if self.mac is not None:
            result["mac"] = self.mac
        return result

    @staticmethod
    def base64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b'=').decode('utf-8')

    def generate_pc_sign(self) -> str:
        json_formatted_sign = json.dumps(self.to_dict())
        payload = self.base64url_encode(json_formatted_sign.encode())
        key = self.sign_key()
        signature = hmac.new(key, payload.encode(), hashlib.sha256).digest()
        return f"{payload}.{self.base64url_encode(signature)}"