#!/usr/bin/env python
# -*- coding: utf-8 -*-
import time
import sys
import json
import broken
import pluginmanager
from collections import OrderedDict
from SimpleXMLRPCServer import list_public_methods
from threading import Timer
import capabilities
# This whole capabilities stuff is a bad idea. And quite poorly executed.
# But I'm really tired right now and I want to have reaver stuff working...
import tempfile
import logging
import inspect
import psutil
import netifaces
import os
import re
import csv
from aircrack import AircrackSession
from SimpleXMLRPCServer import SimpleXMLRPCServer

debug = logging.getLogger(__name__).debug
logging.basicConfig(level=logging.DEBUG)

# Disabled for now
# pluginmanager.load_plugins("plugins.list")


class AiroscriptSessionManager(object):
    """
        Airoscript-ng session manager.

        We can have multiple airoscript-ng sessions running on different
        interfaces

        Each one will have an aircrack-ng session so we can execute only one
        process of each kind in each interface, to avoid collisions.

        A session manager object should be used on xmlrpc sessions (but a
        airoscript session alone can be used too)
    """
    def __init__(self, wifi_iface):
        """
            Initialize empty session list
        """
        self.session_list = {}

    def create_session(self, name=False, sleep_time=2, scan_time=2):
        """
            Create a AiroscriptSession object and assigns it to session_list

            If no name provided it will take current time
            (used to create monitor wireless interface)

            Note that *this does not setup monitor mode* so we have to call
            the session's setup_wifi method each time.
        """
        if not name:
            name = str(time.time()).replace('.', '')

        if name not in self.session_list:
            self.session_list[name] = AiroscriptSession({
                'name': name,
                'sleep_time': sleep_time,
                'scan_time': scan_time
            })
        else:
            raise Exception("Session name already taken")

        return self.session_list[name]

    def get_session(self, session_name):
        """
            XMLRPC method returning a specific session object.
        """
        return self.session_list[session_name]



class Airoscript(object):
    """
        Main airoscript object.
        Contains not-that-direct aircrack-ng interaction methods and attacks
    """

    pids = {}

    def rebump(self, pid):
        """
            Launches sigint to a process.
            In airodump-ng this means updating the csv contents
        """
        return os.kill(pid, 2)

    def on_scan_bumped(self, pid):
        """
            Callback to execute each time we rebump on scan().
            This schedulles a re-rebump so it's made periodically.
            That's how we'll get periodically (each second) updated csv files

            This implements the plugin system, calling on_after_scan
        """
        self.rebump(pid)
        time.sleep(1)
        Timer(int(self.config['scan_time']), self.on_scan_bumped, (pid))
        return pluginmanager.trigger_event(
            "on_after_scan",
            target=self.target,
            session=self,
        )

    def end_scan(self):
        """
            We send a kill signal to airodump-ng
            As aircrack object is not aware of this,
            we must manually change the status
        """
        try:
            self.aircrack.executing.pop('airodump-ng')
            return os.kill(self.pids['airodump-ng'], 9)
        except KeyError, err:
            logging.debug("Received keyerror on {}".format(err))
        return False

    def scan(self, options=OrderedDict()):
        """
            Main scanning function.
            Launches airodump-ng, saves its pid,
            and makes itself xmlrpc representable.

            This implements the plugin system, calling on_before_scan
        """
        pluginmanager.trigger_event(
            "on_before_scan",
            target=self._target,
            session=self,
        )
        final_options = OrderedDict([
            ('dump_prefix', self.target_dir + "/" + self.config["name"]),
            ('wireless', self.mon_iface)
        ])
        final_options.update(options.items())

        result = self.aircrack.airodump(final_options, lambda x: True)

        # We wait default scan time and ask for airodump-ng to re-bump.
        # With this we can have an airodump-ng continuously scanning
        # on background until we want to get to a fixed channel
        # TODO Maybe magic with fixed / hoping channels and different cards?
        pid = result.result().result.pid
        Timer(int(self.config['scan_time']), self.on_scan_bumped, (pid))
        self.pids['airodump-ng'] = pid

        clean_self = clean_to_xmlrpc(self, ['extra_capabilities'])
        clean_self['_target'] = clean_to_xmlrpc(
            clean_self['_target'], ['parent'])
        return clean_self

    def crack(self):
        """
            Launches aircrack-ng in infinite mode against current target.
            :TODO: This should probably be better managed by set_target.
        """
        # Launch aircrack to crack indefinitely
        aircrack = self.aircrack.aircrack(OrderedDict([
            ('key_file', self._target.key_file),
            ('filter_bssid', self._target.bssid),
            ('cap_file', os.path.join(self.target_dir, self.config["name"] + "-01.cap"))
        ]), lambda x: True)
        return aircrack

    def pyrit(self):
        return "NOT IMPLEMENTED"

    def generic_dissasociation(self):
        """
            This does a generic dissasociation attack.
            Meaning that this attack is both useful on WEP and WPA.

            This can be used both to get ARP replays and WPA handshake.

            See : http://www.aircrack-ng.org/doku.php?id=deauthentication
        """
        self.end_scan()
        # TODO: end_crack too?
        self.scan(OrderedDict([
            ('filter_bssid', self._target.bssid),
        ]))

        aireplay_options = OrderedDict([
            ('replay_ap_bssid', self._target.bssid),
            ('deauth', True)
        ])
        if len(self._target.clients) > 0:
            aireplay_options.update([
                ('replay_ap_destination', self._target.clients[0]['bssid'])
            ])  # TODO: that bssid is probably not clean

        # Launch aireplay in dissasoc mode
        aireplay = self.aircrack.aireplay(aireplay_options, lambda x: True)
        aireplay_pid = aireplay.result().result.pid

        aircrack = self.crack()
        aircrack_pid = aircrack.result().result.pid

        self._target.pids = {
            'aicrack': aircrack_pid,
            'aireplay': aireplay_pid
        }

        return {
            'status': 'on',
            'pids': self._target.pids
        }

    def get_wpa_handshake(self):
        """
            Get WPA handshake, this right now is an alias for generic_dissasociation.
        """
        return self.generic_dissasociation()

    def do_automated_hacking(self):
        """
            Tries every possible tech reported to work by hackability status.

            This means that every tech must be part of THIS class.
            External modules (capabilities) loaded in extra_capabilities will need
            to have a "crack" function that will be invoked when
            session._capability_name_ is invoked
        """
        return [getattr(self, tech) for tech in self._target.hackability["techs"]]


class AiroscriptSession(Airoscript):
    """
        Basic airoscriptng session object.
        This is the basic airoscriptng object.
        Handles network interfaces.
        Main interaction with outer world will be here.

    """
    def __init__(self, config={}):
        """
            Sets up main file.

            :TODO:
                - Make parameter_file default value configurable, or shorter.
                - Extra-capabilities stuff is still confusing
        """
        self.config = config
        self._target = Target()
        self._mon_iface = None
        self.target_dir = tempfile.mkdtemp()
        self.resources_dir = os.path.join(os.path.dirname(unicode(__file__, sys.getfilesystemencoding())), "resources")
        if 'parameter_file' not in self.config:
            self.config['parameter_file'] = os.path.join(self.resources_dir,
                                                         "parameters.json")
        self.parameters = json.load(open(self.config['parameter_file']))
        self.aircrack = AircrackSession(self.parameters)
        self.extra_capabilities = dict([(extra, getattr(getattr(capabilities, extra), 'main')(self)) for extra in capabilities.__all__])
        self.reaver_targets = []
        for cap_name in capabilities.__all__:
            # This is so we can have external capabilities to manage attacks in hackability stuff.
            # Right now, to use reaver =P
            setattr(self, cap_name, lambda x, _name=cap_name:
                    self.extra_capabilities[_name].hack(x))

    def list_wifi(self):
        """
            Returns a list of all the available wireless networks
        """
        # If the driver is not using the new stack, screw them.
        return [iface for iface in netifaces.interfaces() if "wlan" in iface]

    def setup_wifi(self, iface):
        """
            Starts monitor mode interface and checks it's ok.

            :TODO:
                - Injection test.
        """
        self.config['wifi'] = iface
        os.environ['MON_PREFIX'] = self.config["name"]
        self.should_be_mon_iface = self.config["name"] + "0"
        ifaddr = netifaces.ifaddresses(self.config['wifi'])
        self.mac_addr = ifaddr[netifaces.AF_LINK][0]['addr']

        if self.should_be_mon_iface not in netifaces.interfaces():
            self.aircrack.airmon(OrderedDict([('command', "start"),
                ('wireless', self.config["wifi"])]), self.set_mon_iface)
        else:
            self._mon_iface = self.should_be_mon_iface
        return self._mon_iface

    def get_mac_addr(self):
        """
            Return mac address of the interface
        """
        return self.mac_addr

    @property
    def mon_iface(self):
        """
            Return current monitor interface name
        """
        return self._mon_iface

    @mon_iface.setter
    def mon_iface(self, mon_iface):
        """
            Sets monitor interface (setter)
        """
        self._mon_iface = mon_iface

    def set_mon_iface(self, result):
        """
            Sets monitor interface.
            Checks that final monitor interface is really what it should be.
        """
        mon_result = result.communicate()
        for line in mon_result[0].splitlines():
            mon_regex = '(.*)\((.*)monitor mode enabled on (.*)\)(.*)'
            monitor_test = re.match(mon_regex, line)
            if monitor_test:
                self._mon_iface = monitor_test.group(3)
                if not self.mon_iface == self.should_be_mon_iface:
                    debug("Monitor interface is {} and should be {}".format(
                        self.mon_iface, self.should_be_mon_iface))
        return True

    def del_mon_iface(self):
        """
            Deletes own monitor interface from system (cleanup)
        """
        return self.aircrack.launch("iw", [self.mon_iface, "del"])

    @property
    def target(self):
        """
            Returns currently selected target (getter)
        """
        return self.get_target

    def get_target(self):
        """
            Returns currently selected target, clean to send it via xmlrpc
        """
        return clean_to_xmlrpc(self._target, ['properties', 'parent'])

    @target.setter
    def target(self, target):
        """
            target setter
        """
        return self.set_target(target)

    def set_target(self, target):
        """
            This way we only have to do something like
            self.target = current_targets[10] and it'll automatically
            make an object from it.
        """
        if not isinstance(target, Target):
            if isinstance(target, list):
                target = dict(target)
            self._target = Target(self)
            self._target.from_dict(target)
        else:
            self._target = target

    @property
    def current_targets(self):
        """
            Returns current targets (getter)
        """
        return self.get_current_targets()

    def get_current_targets(self):
        """
            Parses airodump-ng's output file, creating a Target object
            for each AP found, with its currently detected clients.
        """
        aps = []
        clients = []

        scan_file = "{}/{}-01.csv".format(self.target_dir, self.config["name"])
        with open(scan_file) as f:
            dictcsv = [a for a in csv.DictReader(f, skipinitialspace=True)]

        if "reaver" in self.extra_capabilities:
            self.reaver_targets = self.extra_capabilities['reaver'].scan(
                scan_file)

        currently_processing_aps = True
        for element in dictcsv:
            element = element[None]
            if currently_processing_aps:
                if element[0] == "Station MAC":
                    currently_processing_aps = False
                    clients.append(element)
                else:
                    aps.append(element)
            else:
                clients.append(element)

        if len(aps) == 0:
            return False

        ap_headers = aps.pop(0)
        client_headers = [a.lstrip(" ") for a in clients.pop(0)]
        clients = [dict(zip(client_headers, client)) for client in clients]
        a = [Target(self).from_dict(dict(zip(ap_headers, ap)), clients) for ap in aps]
        return sorted(a, key=lambda x: x['hackability']['value'], reverse=True)

    def listMethods(self):
        """
            Hack to return public methods of this object via XMLRPC

            :TODO:
                - Make this work
        """
        return list_public_methods(self)

    def _methodHelp(self, method):
        """
            Hack to return public methods' help of this object via XMLRPC

            :TODO:
                - Make this work
        """

        f = getattr(self, method)
        return inspect.getdoc(f)


def clean_to_xmlrpc(element, to_clean):
    """
        Cleans certain properties from dict representation of given object
    """
    if not isinstance(element, dict):
        res = element.__dict__.copy()
    else:
        res = element.copy()
    for el in to_clean:
        res.pop(el)
    return res


class Target(object):
    """
        Target object, this represents an access point
    """
    def __init__(self, parent=False):
        """
            We pass on parent so we can access some of its methods.
            :TODO:
                - That might not be the best, have it in mind
        """
        self.pids = {}
        self.parent = parent
        self.properties = [
            'bssid',
            'essid',
            'power',
            'encryption',
            'associated',
        ]

        for element in self.properties:
            setattr(self.__class__, element, '')

    def is_attack_running(self):
        """
            Returns True if the ALL the attack processes are still executing
            This means that:

                If part of the attack (I.E replaying) has stopped, will
                consider the attack finished.

                If the processes don't die after the attack is successful,
                it wont consider the attack finished.

            That's why we'll combine it with is_cracked and key
        """
        return all([psutil.pid_exists(pid) for pid in self.pids.values()])

    def is_attack_finished(self):
        """
            Return if the attack is finished.
            This is a more complete check that is_attack_running,
            it has in account if the network has been cracked and
            if the attack has actually started.
        """
        if self.is_network_cracked():
            return True
        return self.pids is not {} and not self.is_attack_running()

    @property
    def key_file(self):
        return os.path.join(self.parent.target_dir, "{}.{}".format(
            self.bssid.replace(':', '_'), "key"))

    def is_network_cracked(self):
        """
            If the network has been cracked we'll save the key to a key file
            for that specific target. This function just asks if the network
            has been cracked. Possibly not going to be used as network_key
            returns False if not cracked.
        """
        return os.exists(self.key_file)

    def cleanup(self):
        """
            kill all related PIDS and clean them.
        """
        pids = self.pids.copy()
        self.pids = {}
        return [os.kill(pid) for pid in pids.values()]

    @property
    def key(self):
        """
            Return network key or False if network has not been cracked.
        """
        return self.get_key()

    def get_key(self):
        """
            XMLRPC function for key()
        """
        if not self.is_network_cracked():
            return False
        with open(self.key_file) as keyf:
            return keyf.readlines()

    def from_dict(self, dict_, clients=[]):
        """
            Do some magic, get only its clients from the client list,
            strip extra whitespace in properties, and get its hackability
        """
        if "BSSID" in dict_:
            self.bssid = dict_['BSSID'].strip()
            self.essid = dict_['ESSID'].strip()
            self.power = dict_['Power'].strip()
            self.encryption = dict_['Privacy'].strip(),
        else:
            self.bssid = dict_['bssid']
            self.essid = dict_['essid']
            self.power = dict_['power']
            self.encryption = dict_['encryption']
        self.hackability = self.get_hackability()
        self.clients = [client for client in clients if client['Station MAC'] == self.bssid]
        return clean_to_xmlrpc(self, ['properties', 'parent'])

    def get_hackability(self):
        """
            This assets a network hackability based on:

            * Network power
            * Network encryption
            * WPS availability
            * Dictionary availability
        """
        points = 0
        techs = []
        for essid in broken.ESSIDS:
            if essid in self.essid:
                points += 50
        points += - (int(self.power) * 10)

        if self.encryption[0] in broken.PRIVACY:
            points += broken.PRIVACY[self.encryption[0]][0]
            techs += broken.PRIVACY[self.encryption[0]][1]

        if "reaver" in self.parent.extra_capabilities:
            if self.bssid in [a['bssid'] for a in self.parent.reaver_targets]:
                points += 800
                techs.insert(1, "reaver")

        return {
            'name': broken.get_hackability_name(points/10),
            'value': int(points/20),
            'techs': techs
        }


def airoscriptxmlrpc():
    """
        Simple xmlrpc server for airoscriptsession.
        :TODO: - Make it multisession
    """
    server = SimpleXMLRPCServer(("localhost", 8000), allow_none=True)
    server.register_instance(AiroscriptSession({'name': 'one',
                                                'wifi': 'wlan0',
                                                'scan_time': '10'}
                                               ), allow_dotted_names=True)
    server.register_introspection_functions()
    server.serve_forever()

if __name__ == "__main__":
    airoscriptxmlrpc()
