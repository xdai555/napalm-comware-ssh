# Copyright 2022 Eric Wu. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

"""
Napalm driver for H3C ComwareV7 devices.

Read https://napalm.readthedocs.io for more information.
"""

from operator import itemgetter
from collections import defaultdict
from typing import Any, Optional, Dict, List
import re
from netmiko.hp.hp_comware import HPComwareBase
from napalm.base.base import NetworkDriver
from napalm.base.exceptions import (
    ConnectionClosedException,
    ConnectionException,
)
import napalm.base.constants as C
from napalm.base.helpers import (
    textfsm_extractor,
    mac,
)
from napalm.base.netmiko_helpers import netmiko_args
from .utils.helpers import (
    canonical_interface_name_comware,
    parse_time,
    parse_null,
    strptime,
    get_value_from_list_of_dict,
)


class ComwareDriver(NetworkDriver):
    """Napalm driver for H3C/HP Comware7 network devices (using ssh)."""

    def __init__(
            self,
            hostname: str,
            username: str,
            password: str,
            timeout: int = 100,
            optional_args: Optional[Dict] = None):

        self.device = None
        if optional_args is None:
            optional_args = {}
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.netmiko_optional_args = netmiko_args(optional_args)

    def open(self):
        """Open a connection to the device."""
        device_type = "hp_comware"
        self.device: HPComwareBase = self._netmiko_open(
            device_type, netmiko_optional_args=self.netmiko_optional_args
        )

    def close(self):
        self._netmiko_close()

    def send_command(self, command, *args, **kwargs):
        return self.device.send_command(command, *args, **kwargs)

    def is_alive(self):
        if self.device is None:
            return {"is_alive": False}
        else:
            return {"is_alive": self.device.is_alive()}

    def _get_structured_output(self, command: str, template_name: str = None):
        """
        Wrapper for `napalm.base.helpers.textfsm_extractor()`.
        """
        if template_name is None:
            template_name = "_".join(command.split())
        raw_output = self.send_command(command)
        result = textfsm_extractor(self, template_name, raw_output)
        return result

    def get_facts(self):
        # default values.
        vendor = "Comware"
        uptime = -1
        serial_number, fqdn, os_version, hostname, fqdn = ("Unknown",) * 5

        # uptime、vender、model
        cmd_sys_info = "display version"
        structured_sys_info = self._get_structured_output(cmd_sys_info)
        if isinstance(structured_sys_info, list) and len(structured_sys_info) == 1:
            structured_sys_info = structured_sys_info[0]
            (uptime_str, vendor, model, os_version) = itemgetter(
                "uptime", "vendor", "model", "os_version"
            )(structured_sys_info)
            uptime = parse_time(uptime_str)

        # os_version. format: `Version.Patch`.
        # Example: "Release 2612P01.2612P01H03"
        # cmd_os_version = "display device"
        # structured_os_version = self._get_structured_output(cmd_os_version)

        # fqdn
        # show_domain = self.send_command("display dns domain")

        # hostname
        hostname = self.device.find_prompt()[1:-1]

        # interfaces
        interface_list = []
        structured_int_info = self._get_structured_output("display interface")
        for interface in structured_int_info:
            interface_list.append(interface.get("interface"))

        # serial number
        cmd_sn = "display device manuinfo"
        structured_sn = self._get_structured_output(cmd_sn)
        if isinstance(structured_sn, list) and len(structured_sn) > 0:
            serial_number = []
            for sn in structured_sn:
                if sn.get("slot_type") == "Slot":
                    # 解析结果为空则跳过记录
                    _sn = sn.get("serial_number")
                    if _sn != "":
                        serial_number.append(_sn)
            if len(serial_number) > 0:
                serial_number = ",".join(serial_number)
            else:
                serial_number = "Unknown"

        return {
            "uptime": uptime,
            "vendor": vendor,
            "os_version": os_version,
            "serial_number": serial_number,
            "model": model,
            "hostname": hostname,
            "fqdn": fqdn,
            "interface_list": interface_list,
        }

    def get_interfaces(self):
        interface_dict = {}
        structured_int_info = self._get_structured_output("display interface")

        for interface in structured_int_info:
            # Physical state:
            #   - Administratively DOWN
            #   - DOWN
            #   - UP
            is_enabled = bool("up" in interface.get("link_status").lower())
            # Protocol state:
            #  - UP
            #  - UP (spoofing)
            #  - DOWN
            #  - DOWN(other protocol):such as DOWN(DLDP)、DOWN(LAGG) ...
            is_up = bool("up" in interface.get("protocol_status").lower())

            (description, speed, mtu, mac_address, last_flapped) = itemgetter(
                "description", "bandwidth", "mtu", "mac_address", "last_flapping",
            )(interface)

            # Never flapping: 0
            # No flapping data: -1
            # Last flapping: int(seconds)
            if "never" in last_flapped.lower():
                last_flapped = 0
            else:
                last_flapped = parse_null(last_flapped, -1, parse_time)

            interface_dict[interface.get("interface")] = {
                "is_enabled": is_enabled,
                "is_up": is_up,
                "description": description,
                "speed": parse_null(speed, -1, int),
                "mtu": parse_null(mtu, -1, int),
                "mac_address": parse_null(mac_address, "unknown", mac),
                "last_flapped": last_flapped,
            }
        return interface_dict

    def get_lldp_neighbors(self):
        lldp = {}
        command = "display lldp neighbor-information verbose"
        structured_output = self._get_structured_output(command)
        for lldp_entry in structured_output:
            (local_interface, remote_system_name, remote_port) = itemgetter(
                "local_interface", "remote_system_name", "remote_port"
            )(lldp_entry)
            if lldp.get(local_interface) is None:
                lldp[local_interface] = [{
                    "hostname": remote_system_name,
                    "port": remote_port
                }]
            else:
                lldp[local_interface].append({
                    "hostname": remote_system_name,
                    "port": remote_port
                })
        return lldp

    def get_bgp_neighbors(self): ...

    def _get_memory(self):

        memory = []
        command = "display memory"
        structured_output = self._get_structured_output(command)
        for mem_entry in structured_output:
            (chassis, slot, total, used, free, free_ratio) = itemgetter(
                "chassis", "slot", "total", "used", "free", "free_ratio"
            )(mem_entry)
            entry = {
                "chassis": parse_null(chassis, -1, int),
                "slot": parse_null(slot, -1, int),
                "total_ram": int(total),
                "used_ram": int(used),
                "available_ram": int(free),
                "free_ratio": float(free_ratio.strip("%")),
            }
            memory.append(entry)
            
        return memory

    def get_environment(self):
        environment = {}

        cmd_cpu = ""

        # 有多个板卡的话，返回使用空间最多的，可以提前预防问题
        # return info of the slot with max memory usage if device has more than one slot. 
        memory = {}
        _mem_all = self._get_memory()
        _mem = get_value_from_list_of_dict(_mem_all, "free_ratio", min)
        memory["used_ram"] = _mem.get("used_ram")
        memory["available_ram"] = _mem.get("available_ram")
        environment["memory"] = memory


        cmd_power = ""

        cmd_fan = ""

        cmd_temp = ""

        return environment

    def get_interfaces_counters(self):
        counters = {}
        keys = (
            "tx_errors", "rx_errors", "rx_crc",
            "tx_discards", "rx_discards",
            "tx_octets", "rx_octets",
            "tx_packets", "rx_packets",
            "tx_unicast_packets", "rx_unicast_packets",
            "tx_multicast_packets", "rx_multicast_packets",
            "tx_broadcast_packets", "rx_broadcast_packets",
        )
        structured_int_info = self._get_structured_output("display interface")

        for interface in structured_int_info:
            values = itemgetter(
                "tx_errors", "rx_errors", "rx_crc",
                "tx_aborts", "rx_aborts",
                "tx_bytes", "rx_bytes",
                "tx_pkts", "rx_pkts",
                "tx_unicast", "rx_unicast",
                "tx_multicast", "rx_multicast",
                "tx_broadcast", "rx_broadcast",
            )(interface)

            values = (parse_null(value, -1, int) for value in values)
            counters[interface.get("interface")] = dict(zip(keys, values))

        return counters

    def get_lldp_neighbors_detail(self, interface=""):
        lldp = {}
        # `parent_interface` is not supported
        parent_interface = ""

        if interface:
            command = "display lldp neighbor-information interface %s verbose" % (
                interface)
        else:
            command = "display lldp neighbor-information verbose"

        structured_output = self._get_structured_output(
            command, "display_lldp_neighbor-information_verbose")

        for lldp_entry in structured_output:
            (local_interface,
             remote_port,
             remote_port_description,
             remote_chassis_id,
             remote_system_name,
             remote_system_description,
             remote_system_capab,
             remote_system_enabled_capab) = itemgetter(
                "local_interface",
                "remote_port",
                "remote_port_desc",
                "remote_chassis_id",
                "remote_system_name",
                "remote_system_desc",
                "remote_system_capab",
                "remote_system_enabled_capab")(lldp_entry)
            _ = {
                "parent_interface": parent_interface,
                "remote_port": remote_port,
                "remote_port_description": remote_port_description,
                "remote_chassis_id": remote_chassis_id,
                "remote_system_name": remote_system_name,
                "remote_system_description": "".join(remote_system_description),
                "remote_system_capab": [i.strip() for i in remote_system_capab.split(",")],
                "remote_system_enabled_capab": [i.strip() for i in remote_system_enabled_capab.split(",")],
            }
            if lldp.get(local_interface) is None:
                lldp[local_interface] = [_]
            else:
                lldp[local_interface].append(_)
        return lldp

    def cli(self, commands: List):

        cli_output = dict()

        if type(commands) is not list:
            raise TypeError("Please enter a valid list of commands!")

        for command in commands:
            output = self.device.send_command(command)
            cli_output.setdefault(command, {})
            cli_output[command] = output

        return cli_output

    def get_arp_table(self, vrf: str = ""):
        arp_table = []
        if vrf:
            command = "display arp vpn-instance %s" % (vrf)
        else:
            command = "display arp"
        structured_output = self._get_structured_output(command, "display_arp")
        for arp_entry in structured_output:
            (interface, mac_address, ip, age, ) = itemgetter(
                "interface", "mac_address", "ip_address", "aging"
            )(arp_entry)
            entry = {
                'interface': canonical_interface_name_comware(interface),
                'mac': mac(mac_address),
                'ip': ip,
                'age': float(age),
            }
            arp_table.append(entry)
        return arp_table

    def get_interfaces_ip(self): ...

    def get_mac_address_move_table(self):
        mac_address_move_table = []
        command = "display mac-address mac-move"
        structured_output = self._get_structured_output(command)
        for mac_move_entry in structured_output:
            (mac_address, vlan, current_port, source_port, last_move, moves,) = itemgetter(
                "mac_address", "vlan", "current_port", "source_port", "last_move", "times",
            )(mac_move_entry)
            entry = {
                "mac": mac(mac_address),
                "vlan": int(vlan),
                "current_port": canonical_interface_name_comware(current_port),
                "source_port": canonical_interface_name_comware(source_port),
                "last_move": last_move,
                "moves": int(moves),
            }
            mac_address_move_table.append(entry)

        return mac_address_move_table

    def get_mac_address_table(self):
        mac_address_table = []
        command = "display mac-address"
        structured_output = self._get_structured_output(command)
        mac_address_move_table = self.get_mac_address_move_table()

        def _get_mac_move(mac_address, mac_address_move_table):
            last_move = float(-1)
            moves = -1
            for mac_move in mac_address_move_table:
                if mac_address == mac_move.get("mac_address"):
                    last_move = strptime(mac_move.get("last_move"))
                    moves = mac_move.get("times")
            return {
                "last_move": float(last_move),
                "moves": int(moves)
            }

        for mac_entry in structured_output:
            (mac_address, vlan, state, interface) = itemgetter(
                "mac_address", "vlan", "state", "interface"
            )(mac_entry)
            entry = {
                "mac": mac(mac_address),
                "interface": canonical_interface_name_comware(interface),
                "vlan": int(vlan),
                "static": True if "tatic" in state.lower() else False,
                "state": state,
                "active": True,
            }
            entry.update(_get_mac_move(mac_address, mac_address_move_table))
            mac_address_table.append(entry)

        return mac_address_table

    def get_route_to(self, destination="", protocol="", longer=False): ...

    def get_config(self, retrieve="all", full=False, sanitized=False):
        configs = {"startup": "", "running": "", "candidate": ""}
        # Not Supported
        if full:
            pass
        if retrieve.lower() in ("running", "all"):
            command = "display current-configuration"
            configs["running"] = self.send_command(command)
        if retrieve.lower() in ("startup", "all"):
            command = "display saved-configuration"
            configs["startup"] = self.send_command(command)
        # Ignore, plaintext will be encrypted.
        # Remove secret data ? Not Implemented.
        if sanitized:
            pass
        return configs

    def get_network_instances(self, name=""): ...

    def get_vlans(self):
        """
        Return structure being spit balled is as follows.
            * vlan_id (int)
                * name (text_type)
                * interfaces (list)

        By default, `vlan_name` == `vlan_description`. If both are default or not, \
        use `vlan_name`. If one of them is not the default value, use user-configured \
        value.

        Example::

            {
                1: {
                    "name": "default",
                    "interfaces": ["GigabitEthernet0/0/1", "GigabitEthernet0/0/2"]
                },
                2: {
                    "name": "vlan2",
                    "interfaces": []
                }
            }
        """
        vlans = {}
        command = "display vlan all"
        structured_output = self._get_structured_output(command)
        for vlan_entry in structured_output:
            vlan_name = vlan_entry.get("name")
            vlan_desc = vlan_entry.get("description")
            vlans[int(vlan_entry.get("vlan_id"))] = {
                "name": vlan_desc if "VLAN " not in vlan_desc and "VLAN " in vlan_name else vlan_name,
                "interfaces": vlan_entry.get("interfaces")
            }
        return vlans

    def get_irf_config(self):
        """
        Returns a dictionary of dictionaries where the first key is irf member ID, 
        and the internal dictionary uses the irf port type as the key and port member as the value.

        Example::
            {
                1: {
                    'irf-port1': ['FortyGigE1/0/53', 'FortyGigE1/0/54'],
                    'irf-port2': [],
                }
                2: {
                    'irf-port1': [],
                    'irf-port2': ['FortyGigE2/0/53', 'FortyGigE2/0/54'],
                }
            }
        """
        irf_config = defaultdict(dict)
        command = "display current-configuration configuration irf-port"
        structured_output = self._get_structured_output(command)
        for config in structured_output:
            (member_id, port_id, port_member) = itemgetter(
                "member_id", "port_id", "port_member"
            )(config)
            irf_config[int(member_id)]["irf-port%s" % port_id] = port_member
        return irf_config

    def is_irf(self):
        """
        Returns True if the IRF is setup.
        """
        config = self.get_irf_config()
        if config:
            return {"is_irf": True}
        else:
            return {"is_irf": False}
