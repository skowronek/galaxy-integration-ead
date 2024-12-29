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
import datetime
import base64
import hmac
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class PCSignVersion(Enum):
    V1 = "v1"
    V2 = "v2"

@dataclass
class PCSign:
    bsn: str = field(init=False)
    gid: int = field(init=False)
    hsn: str = field(init=False)
    msn: str = field(init=False)
    mac: Optional[str] = None
    mid: str = field(init=False)
    ts: str = field(init=False)
    av: str = "v1"
    sv: PCSignVersion = PCSignVersion.V1
    
    def __post_init__(self):
        self.bsn, self.gid, self.hsn, self.msn, self.mac = self.gather_hardware_info()
        self.mid = self.calculate_fnv1a_hash(self.bsn, self.gid, self.hsn, self.msn)
        self.ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S:%f")[:-3]

    def gather_hardware_info(self):
        if os.name == "nt":
            return self._gather_windows_info()
        elif os.name == "posix":
            return self._gather_macos_info()
        else:
            raise OSError("Unsupported OS")

    def _run_cmd(self, cmd):
        try:
            output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT)
            return output.decode().strip()
        except subprocess.CalledProcessError:
            return ""

    def _gather_windows_info(self):
        bsn = self._run_cmd("wmic bios get serialnumber").splitlines()[1].strip()
        gpu_line = self._run_cmd("wmic path win32_videocontroller get pnpdeviceid").splitlines()[1]
        gid = int(gpu_line.split('DEV_')[1].split('&')[0], 16) if "DEV_" in gpu_line else 0
        hsn = self._run_cmd("wmic diskdrive get serialnumber").splitlines()[1].strip()
        msn = self._run_cmd("wmic baseboard get serialnumber").splitlines()[1].strip()
        mac_line = self._run_cmd("wmic nic where physicaladapter=true get macaddress").splitlines()
        mac = mac_line[1].strip() if len(mac_line) > 1 else None
        return bsn, gid, hsn, msn, mac

    def _gather_macos_info(self):
        bsn = self._run_cmd("system_profiler SPHardwareDataType | awk '/Serial Number/ {print $NF}'")
        gid = int(self._run_cmd("system_profiler SPDisplaysDataType | awk '/Device ID:/ {print $NF}'"), 16) or 0
        hsn = self._run_cmd("diskutil info /dev/disk0 | awk '/Device Identifier:/ {print $NF}'")
        msn = self._run_cmd("system_profiler SPHardwareDataType | awk '/Hardware UUID:/ {print $NF}'")
        mac = self._run_cmd("ifconfig en0 | awk '/ether/ {print $2}'")
        return bsn, gid, hsn, msn, mac
    
    @staticmethod
    def calculate_fnv1a_hash(bsn, gid, hsn, msn):
        hardware_bytes = f"{bsn}{gid}{hsn}{msn}".encode()
        offset = 0xcbf29ce484222325
        prime = 0x100000001b3
        for b in hardware_bytes:
            offset ^= b
            offset = (offset * prime) & 0xFFFFFFFFFFFFFFFF
        return f"{offset:016x}"

    def sign_key(self):
        keys = {
            PCSignVersion.V1: b"ISa3dpGOc8wW7Adn4auACSQmaccrOyR2",
            PCSignVersion.V2: b"nt5FfJbdPzNcl2pkC3zgjO43Knvscxft"
        }
        return keys.get(self.sv)

    def to_dict(self):
        d = {
            "av": self.av, "bsn": self.bsn, "gid": self.gid,
            "hsn": self.hsn, "mid": self.mid, "msn": self.msn,
            "sv": self.sv.value, "ts": self.ts
        }
        if self.mac: d["mac"] = self.mac
        return d

    @staticmethod
    def base64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

    def generate_pc_sign(self) -> str:
        payload = self.base64url_encode(json.dumps(self.to_dict()).encode())
        signature = hmac.new(self.sign_key(), payload.encode(), hashlib.sha256).digest()
        return f"{payload}.{self.base64url_encode(signature)}"
