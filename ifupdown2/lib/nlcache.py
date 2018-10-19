#!/usr/bin/env python
#
# Copyright (C) 2017, 2018 Cumulus Networks, Inc. all rights reserved
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; version 2.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.
#
# https://www.gnu.org/licenses/gpl-2.0-standalone.html
#
# Author:
#       Julien Fortin, julien@cumulusnetworks.com
#
# Netlink cache --
#

import os
import socket
import struct
import signal
import inspect
import threading
import traceback

from collections import OrderedDict

from logging import DEBUG, WARNING

try:
    from ifupdown2.ifupdownaddons.cache import *
    from ifupdown2.lib.dry_run import DryRun
    from ifupdown2.ifupdown.log import log

    import ifupdown2.ifupdown.ifupdownflags as ifupdownflags

    import ifupdown2.nlmanager.nlpacket as nlpacket
    import ifupdown2.nlmanager.nllistener as nllistener
    import ifupdown2.nlmanager.nlmanager as nlmanager
except:
    from ifupdownaddons.cache import *
    from lib.dry_run import DryRun
    from ifupdown.log import log

    import ifupdown.ifupdownflags as ifupdownflags

    import nlmanager.nlpacket as nlpacket
    import nlmanager.nllistener as nllistener
    import nlmanager.nlmanager as nlmanager


class NetlinkError(Exception):
    def __init__(self, exception, prefix=None, ifname=None):
        netlink_exception_message = []

        if ifname:
            netlink_exception_message.append(ifname)

        if prefix:
            netlink_exception_message.append(prefix)

        netlink_exception_message.append(str(exception))
        super(NetlinkError, self).__init__(": ".join(netlink_exception_message))


class NetlinkCacheError(Exception):
    pass


class NetlinkCacheIfnameNotFoundError(NetlinkCacheError):
    pass


class NetlinkCacheIfindexNotFoundError(NetlinkCacheError):
    pass


class _NetlinkCache:
    """ Netlink Cache Class """

    _ifa_attributes = nlpacket.Address.attribute_to_class.keys()
    # nlpacket.Address.attribute_to_class.keys() =
    # (
    #     nlpacket.Address.IFA_ADDRESS,
    #     nlpacket.Address.IFA_LOCAL,
    #     nlpacket.Address.IFA_LABEL,
    #     nlpacket.Address.IFA_BROADCAST,
    #     nlpacket.Address.IFA_ANYCAST,
    #     nlpacket.Address.IFA_CACHEINFO,
    #     nlpacket.Address.IFA_MULTICAST,
    #     nlpacket.Address.IFA_FLAGS
    # )
    # we need to store these attributes in a static list to be able to iterate
    # through it when comparing Address objects in add_address()

    def __init__(self):
        self._link_cache = {}
        self._addr_cache = {}

        # helper dictionaries
        # ifindex: ifname
        # ifname: ifindex
        self._ifname_by_ifindex  = {}
        self._ifindex_by_ifname  = {}

        self._ifname_by_ifindex_sysfs = {}
        self._ifindex_by_ifname_sysfs = {}

        # master/slave(s) dictionary
        # master_ifname: [slave_ifname, slave_ifname]
        self._masters_and_slaves = {}

        # RLock is needed because we don't want to have separate handling in
        # get_ifname, get_ifindex and all the API function
        self._cache_lock = threading.RLock()

        # After sending a RTM_DELLINK request (ip link del DEV) we don't
        # automatically receive an RTM_DELLINK notification but instead we
        # have 3 to 5 RTM_NEWLINK notifications (first the device goes
        # admin-down then, goes through other steps that send notifications...
        # Because of this behavior the cache is out of sync and may cause
        # issues. To work-around this behavior we can ignore RTM_NEWLINK for a
        # given ifname until we receive the RTM_DELLINK. That way our cache is
        # not stale. When deleting a link, ifupdown2 uses:
        #   - NetlinkListenerWithCache:link_del(ifname)
        # Before sending the RTM_DELLINK netlink packet we:
        #   - register the ifname in the _ignore_rtm_newlinkq
        #   - force purge the cache because we are not notified right away
        #   - for every RTM_NEWLINK notification we check _ignore_rtm_newlinkq
        # to see if we need to ignore that packet
        #   - for every RTM_DELLINK notification we check if we have a
        # corresponding entry in _ignore_rtm_newlinkq and remove it
        self._ignore_rtm_newlinkq = list()
        self._ignore_rtm_newlinkq_lock = threading.Lock()

        # In the scenario of NetlinkListenerWithCache, the listener thread
        # decode netlink packets and perform caching operation based on their
        # respective msgtype add_link for RTM_NEWLINK, remove_link for DELLINK
        # In some cases the main thread is creating a new device with:
        #   NetlinkListenerWithCache.link_add()
        # the request is sent and the cache won't have any knowledge of this
        # new link until we receive a NEWLINK notification on the listener
        # socket meanwhile the main thread keeps going. The main thread may
        # query the cache for the newly created device but the cache might not
        # know about it yet thus creating unexpected situation in the main
        # thread operations. We need to provide a mechanism to block the main
        # thread until the desired notification is processed. The main thread
        # can call:
        #   register_wait_event(ifname, netlink_msgtype)
        # to register an event for device name 'ifname' and netlink msgtype.
        # The main thread should then call wait_event to sleep until the
        # notification is received the NetlinkListenerWithCache provides the
        # following API:
        #   tx_nlpacket_get_response_with_error_and_wait_for_cache(ifname, nl_packet)
        # to handle both packet transmission, error handling and cache event
        self._wait_event = None
        self._wait_event_alarm = threading.Event()

    def __handle_type_error(self, func_name, data, exception, return_value):
        """
        TypeError shouldn't happen but if it does, we are prepared to log and recover
        """
        log.debug('nlcache: %s: %s: TypeError: %s' % (func_name, data, str(exception)))
        return return_value

    def append_to_ignore_rtm_newlinkq(self, ifname):
        """
        Register device 'ifname' to the ignore_rtm_newlinkq list pending
        RTM_DELLINK (see comments above _ignore_rtm_newlinkq declaration)
        """
        with self._ignore_rtm_newlinkq_lock:
            self._ignore_rtm_newlinkq.append(ifname)

    def remove_from_ignore_rtm_newlinkq(self, ifname):
        """ Unregister ifname from ignore_newlinkq list """
        try:
            with self._ignore_rtm_newlinkq_lock:
                self._ignore_rtm_newlinkq.remove(ifname)
        except ValueError:
            pass

    def register_wait_event(self, ifname, msgtype):
        """
        Register a cache "wait event" for device named 'ifname' and packet
        type msgtype

        We only one wait_event to be registered. Currently we don't support
        multi-threaded application so we need to had a strict check. In the
        future we could have a wait_event queue for multiple thread could
        register wait event.
        :param ifname: target device
        :param msgtype: netlink message type (RTM_NEWLINK, RTM_DELLINK etc.)
        :return: boolean: did we successfully register a wait_event?
        """
        with self._cache_lock:
            if self._wait_event:
                return False
            self._wait_event = (ifname, msgtype)
        return True

    def wait_event(self):
        """
        Sleep until cache event happened in netlinkq thread or timeout expired
        :return: None

        We set an arbitrary timeout at 1sec in case the kernel doesn't send
        out a notification for the event we want to wait for.
        """
        if not self._wait_event_alarm.wait(1):
            log.debug('nlcache: wait event alarm timeout expired for device "%s" and netlink packet type: %s'
                      % (self._wait_event[0], nlpacket.NetlinkPacket.type_to_string.get(self._wait_event[1], str(self._wait_event[1]))))
        with self._cache_lock:
            self._wait_event = None
            self._wait_event_alarm.clear()

    def unregister_wait_event(self):
        """
        Clear current wait event (cache can only handle one at once)
        :return:
        """
        with self._cache_lock:
            self._wait_event = None
            self._wait_event_alarm.clear()

    def force_admin_status_update(self, ifname, status):
        # TODO: dont override all the flags just turn on/off IFF_UP
        try:
            with self._cache_lock:
                self._link_cache[ifname].flags = status
        except:
            pass

    def DEBUG_IFNAME(self, ifname, with_addresses=False):
        """
        A very useful function to use while debugging, it dumps the netlink
        packet with debug and color output.
        """
        try:
            nllistener.log.setLevel(DEBUG)
            nlpacket.log.setLevel(DEBUG)
            nlmanager.log.setLevel(DEBUG)
            with self._cache_lock:
                obj = self._link_cache[ifname]
                save_debug = obj.debug
                obj.debug = True
                obj.dump()
                obj.debug = save_debug

                if with_addresses:
                    addrs = self._addr_cache.get(ifname, [])
                    log.error('ADDRESSES=%s' % addrs)

                    for addr in addrs:
                        save_debug = addr.debug
                        addr.debug = True
                        addr.dump()
                        addr.debug = save_debug

                        log.error('-----------')
                        log.error('-----------')
                        log.error('-----------')
        except:
            traceback.print_exc()
        # TODO: save log_level at entry and re-apply it after the dump
        nllistener.log.setLevel(WARNING)
        nlpacket.log.setLevel(WARNING)
        nlmanager.log.setLevel(WARNING)

    def _populate_sysfs_ifname_ifindex_dicts(self):
        ifname_by_ifindex_dict = {}
        ifindex_by_ifname_dict = {}
        try:
            for dir_name in os.listdir('/sys/class/net/'):
                try:
                    with open('/sys/class/net/%s/ifindex' % dir_name) as f:
                        ifindex = int(f.readline())
                        ifname_by_ifindex_dict[ifindex] = dir_name
                        ifindex_by_ifname_dict[dir_name] = ifindex
                except (IOError, ValueError):
                    pass
        except OSError:
            pass
        with self._cache_lock:
            self._ifname_by_ifindex_sysfs = ifname_by_ifindex_dict
            self._ifindex_by_ifname_sysfs = ifindex_by_ifname_dict

    def get_ifindex(self, ifname):
        """
        Return device index or raise NetlinkCacheIfnameNotFoundError
        :param ifname:
        :return: int
        :raise: NetlinkCacheIfnameNotFoundError(NetlinkCacheError)
        """
        try:
            with self._cache_lock:
                return self._ifindex_by_ifname[ifname]
        except KeyError:
            # We assume that if the user requested a valid device ifindex but
            # for some reason we don't find any trace of it in our cache, we
            # then use sysfs to make sure that this device exists and fill our
            # internal help dictionaries.
            ifindex = self._ifindex_by_ifname_sysfs.get(ifname)

            if ifindex:
                return ifindex
            self._populate_sysfs_ifname_ifindex_dicts()
            try:
                return self._ifindex_by_ifname_sysfs[ifname]
            except KeyError:
                # if we still haven't found any trace of the requested device
                # we raise a custom exception
                raise NetlinkCacheIfnameNotFoundError('ifname %s not present in cache' % ifname)

    def get_ifname(self, ifindex):
        """
        Return device name or raise NetlinkCacheIfindexNotFoundError
        :param ifindex:
        :return: str
        :raise: NetlinkCacheIfindexNotFoundError (NetlinkCacheError)
        """
        try:
            with self._cache_lock:
                return self._ifname_by_ifindex[ifindex]
        except KeyError:
            # We assume that if the user requested a valid device ifname but
            # for some reason we don't find any trace of it in our cache, we
            # then use sysfs to make sure that this device exists and fill our
            # internal help dictionaries.
            ifname = self._ifname_by_ifindex_sysfs.get(ifindex)

            if ifname:
                return ifname
            self._populate_sysfs_ifname_ifindex_dicts()
            try:
                return self._ifname_by_ifindex_sysfs[ifindex]
            except KeyError:
                # if we still haven't found any trace of the requested device
                # we raise a custom exception
                raise NetlinkCacheIfindexNotFoundError('ifindex %s not present in cache' % ifindex)

    def link_exists(self, ifname):
        """
        Check if we have a cache entry for device 'ifname'
        :param ifname: device name
        :return: boolean
        """
        with self._cache_lock:
            return ifname in self._link_cache

    def link_is_up(self, ifname):
        """
        Check if device 'ifname' has IFF_UP flag
        :param ifname:
        :return: boolean
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].flags & nlpacket.Link.IFF_UP
        except (KeyError, TypeError):
            # ifname is not present in the cache
            return False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=False)

    def link_is_loopback(self, ifname):
        """
        Check if device has IFF_LOOPBACK flag
        :param ifname:
        :return: boolean
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].flags & nlpacket.Link.IFF_LOOPBACK
                # IFF_LOOPBACK should be enough, otherwise we can also check for
                # link.device_type & nlpacket.Link.ARPHRD_LOOPBACK
        except (KeyError, AttributeError):
            return False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=False)

    def link_exists_and_up(self, ifname):
        """
        Check if device exists and has IFF_UP flag set
        :param ifname:
        :return: tuple (boolean, boolean) -> (link_exists, link_is_up)
        """
        try:
            with self._cache_lock:
                return True, self._link_cache[ifname].flags & nlpacket.Link.IFF_UP
        except KeyError:
            # ifname is not present in the cache
            return False, False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=(False, False))

    def link_is_bridge(self, ifname):
        return self.get_link_kind(ifname) == 'bridge'

    def get_link_kind(self, ifname):
        """
        Return link IFLA_INFO_KIND
        :param ifname:
        :return: string
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_KIND]
        except (KeyError, AttributeError):
            return None
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=None)

    def get_link_mtu(self, ifname):
        """
        Return link IFLA_MTU
        :param ifname:
        :return: int
        """
        return self.get_link_attribute(ifname, nlpacket.Link.IFLA_MTU, default=0)

    def get_link_mtu_str(self, ifname):
        """
        Return link IFLA_MTU as string
        :param ifname:
        :return: str
        """
        return str(self.get_link_mtu(ifname))

    def get_link_address(self, ifname):
        """
        Return link IFLA_ADDRESS
        :param ifname:
        :return: str
        """
        return self.get_link_attribute(ifname, nlpacket.Link.IFLA_ADDRESS, default='').lower()

    def get_link_alias(self, ifname):
        """
        Return link IFLA_IFALIAS
        :param ifname:
        :return: str
        """
        return self.get_link_attribute(ifname, nlpacket.Link.IFLA_IFALIAS)

    def get_link_attribute(self, ifname, attr, default=None):
        """
        Return link attribute 'attr'
        :param ifname:
        :param attr:
        :param default:
        :return:
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[attr].value
        except (KeyError, AttributeError):
            return default
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=default)

    def get_link_slave_kind(self, ifname):
        """
        Return device slave kind
        :param ifname:
        :return: str
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_KIND]
        except (KeyError, AttributeError):
            return None
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=None)

    def get_link_info_data_attribute(self, ifname, info_data_attribute, default=None):
        """
        Return device linkinfo:info_data attribute or default value
        :param ifname:
        :param info_data_attribute:
        :param default:
        :return:
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_DATA][info_data_attribute]
        except (KeyError, AttributeError):
            return default
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=default)

    def get_link_info_data(self, ifname):
        """
        Return device linkinfo:info_data attribute or default value
        :param ifname:
        :param info_data_attribute:
        :param default:
        :return:
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_DATA]
        except (KeyError, AttributeError):
            return {}
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value={})

    def get_link_info_slave_data_attribute(self, ifname, info_slave_data_attribute, default=None):
        """
        Return device linkinfo:info_slave_data attribute or default value
        :param ifname:
        :param info_data_attribute:
        :param default:
        :return:
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_DATA][info_slave_data_attribute]
        except (KeyError, AttributeError):
            return default
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=default)

    ################
    # MASTER & SLAVE
    ################
    def get_master(self, ifname):
        """
        Return device master's ifname
        :param ifname:
        :return: str
        """
        try:
            with self._cache_lock:
                return self._ifname_by_ifindex[self._link_cache[ifname].attributes[nlpacket.Link.IFLA_MASTER].value]
        except (KeyError, AttributeError):
            return None
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=None)

    def get_slaves(self, master):
        """
        Return all devices ifname enslaved to master device
        :param master:
        :return: list of string
        """
        with self._cache_lock:
            return list(self._masters_and_slaves.get(master, []))

    def get_lower_device_ifname(self, ifname):
        """
        Return the lower-device (IFLA_LINK) name or raise KeyError
        :param ifname:
        :return: string
        """
        try:
            with self._cache_lock:
                return self.get_ifname(self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINK].value)
        except (NetlinkCacheIfnameNotFoundError, AttributeError, KeyError):
            return None
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=None)

    ##########################################################################
    # BOND ###################################################################
    ##########################################################################

    def bond_exists(self, ifname):
        """
        Check if bond 'ifname' exists
        :param ifname: bond name
        :return: boolean
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_KIND] == 'bond'
        except (KeyError, AttributeError):
            return False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=False)

    ##########################################################################
    # BRIDGE #################################################################
    ##########################################################################

    def bridge_is_vlan_aware(self, ifname):
        """
        Return IFLA_BR_VLAN_FILTERING value
        :param ifname:
        :return: boolean
        """
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_DATA][nlpacket.Link.IFLA_BR_VLAN_FILTERING]
        except (KeyError, AttributeError):
            return False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=False)

    def bridge_port_exists(self, bridge_name, brport_name):
        try:
            with self._cache_lock:
                # we are assuming that bridge_name is a valid bridge?
                return brport_name in self._masters_and_slaves[bridge_name]
        except (KeyError, AttributeError):
            return False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, bridge_name, str(e), return_value=False)

    def get_bridge_name_from_port(self, bridge_port_name):
        bridge_name = self.get_master(bridge_port_name)
        # now that we have the master's name we just need to double check
        # if the master is really a bridge
        return bridge_name if self.link_is_bridge(bridge_name) else None

    def link_is_bridge_port(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_KIND] == 'bridge'
        except (KeyError, AttributeError):
            return False
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=False)

    #def is_link_slave_kind(self, ifname, _type):
    #    try:
    #      with self._cache_lock:
    #            return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_KIND] == _type
    #    except (KeyError, AttributeError):
    #        return False

    ########################################

    def get_link_ipv6_addrgen_mode(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_AF_SPEC].value[socket.AF_INET6][nlpacket.Link.IFLA_INET6_ADDR_GEN_MODE]
        except (KeyError, AttributeError):
            # default to 0 (eui64)
            return 0
        except TypeError as e:
            return self.__handle_type_error(inspect.currentframe().f_code.co_name, ifname, str(e), return_value=0)

    ##########################################################################
    # VRF ####################################################################
    ##########################################################################

    def get_vrfs(self):
        vrfs = []
        with self._cache_lock:
            try:
                for link in self._link_cache.itervalues():
                    linkinfo = link.attributes.get(nlpacket.Link.IFLA_LINKINFO)
                    if linkinfo and linkinfo.value.get(nlpacket.Link.IFLA_INFO_KIND, None) == 'vrf':
                        vrfs.append(link)
            except (KeyError, AttributeError):
                return vrfs
            except TypeError as e:
                return self.__handle_type_error(inspect.currentframe().f_code.co_name, 'NONE', str(e), return_value=vrfs)
        return vrfs


    # old
    def get_vrf_table(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_DATA][nlpacket.Link.IFLA_VRF_TABLE]
        except (KeyError, AttributeError):
            return 0

    #####################################################
    #####################################################
    #####################################################
    #####################################################
    #####################################################

    def add_link(self, link):
        """
        Cache RTM_NEWLINK packet
        :param link:
        :return:
        """
        ifindex = link.ifindex
        ifname = link.get_attribute_value(nlpacket.Link.IFLA_IFNAME)

        # check if this device is registered in the ignore list
        with self._ignore_rtm_newlinkq_lock:
            if ifname in self._ignore_rtm_newlinkq:
                return

        # we need to check if the device was previously enslaved
        # so we can update the _masters_and_slaves dictionary if
        # the master has changed or was un-enslaved.
        old_ifla_master = None

        with self._cache_lock:

            # do we have a wait event registered for RTM_NEWLINK this ifname
            if self._wait_event and self._wait_event == (ifname, nlpacket.RTM_NEWLINK):
                self._wait_event_alarm.set()

            try:
                ifla_master_attr = self._link_cache[ifname].attributes.get(nlpacket.Link.IFLA_MASTER)

                if ifla_master_attr:
                    old_ifla_master = ifla_master_attr.get_pretty_value()
            except KeyError:
                # link is not present in the cache
                pass
            except AttributeError:
                # if this code is ever reached, this is very concerning and
                # should never happen as _link_cache should always contains
                # nlpacket.NetlinkPacket... maybe have some extra handling
                # here just in case?
                pass
            self._link_cache[ifname] = link

            ######################################################
            # update helper dictionaries and handle link renamed #
            ######################################################
            self._ifindex_by_ifname[ifname] = ifindex

            rename_detected                 = False
            old_ifname_entry_for_ifindex    = self._ifname_by_ifindex.get(ifindex)

            if old_ifname_entry_for_ifindex and old_ifname_entry_for_ifindex != ifname:
                # The ifindex was reused for a new interface before we got the
                # RTM_DELLINK notification or the device using that ifindex was
                # renamed. We need to update the cache accordingly.
                rename_detected = True

            self._ifname_by_ifindex[ifindex] = ifname

            if rename_detected:
                # in this case we detected a rename... It should'nt happen has we should get a RTM_DELLINK before that.
                # if we still detect a rename the opti is to get rid of the stale value directly
                try:
                    del self._ifindex_by_ifname[old_ifname_entry_for_ifindex]
                except KeyError:
                    log.debug('update_helper_dicts: del _ifindex_by_ifname[%s]: KeyError ifname: %s'
                              % (old_ifname_entry_for_ifindex, old_ifname_entry_for_ifindex))
                try:
                    del self._link_cache[old_ifname_entry_for_ifindex]
                except KeyError:
                    log.debug('update_helper_dicts: del _link_cache[%s]: KeyError ifname: %s'
                              % (old_ifname_entry_for_ifindex, old_ifname_entry_for_ifindex))
            ######################################################
            ######################################################

            link_ifla_master_attr = link.attributes.get(nlpacket.Link.IFLA_MASTER)
            if link_ifla_master_attr:
                link_ifla_master = link_ifla_master_attr.get_pretty_value()
            else:
                link_ifla_master = None

            # if the link has a master we need to store it in an helper dictionary, where
            # the key is the master ifla_ifname and the value is a list of slaves, example:
            # _masters_slaves_dict = {
            #       'bond0': ['swp21', 'swp42']
            # }
            # this will be useful in the case we need to iterate on all slaves of a specific link

            if old_ifla_master:
                if old_ifla_master != link_ifla_master:
                    # the link was previously enslaved but master is now unset on this device
                    # we need to reflect that on the _masters_and_slaves dictionary
                    try:
                        self._masters_and_slaves[self.get_ifname(old_ifla_master)].remove(ifname)
                    except (KeyError, NetlinkCacheIfindexNotFoundError):
                        pass
                else:
                    # the master status didn't change we can assume that our internal
                    # masters_slaves dictionary is up to date and return here
                    return

            if not link_ifla_master:
                return

            master_ifname = self._ifname_by_ifindex.get(link_ifla_master)

            if not master_ifname:
                # in this case we have a link object with IFLA_MASTER set to a ifindex
                # but this ifindex is not in our _ifname_by_ifindex dictionary thus it's
                # not in the _link_cache yet. This situation may happen when getting the
                # very first link dump. The kernel dumps device in the "ifindex" order.
                #
                # So let's say you have a box with 4 ports (lo, eth0, swp1, swp2), then
                # manually create a bond (bond0) and enslave swp1 and swp2, bond0 will
                # have ifindex 5 but when getting the link dump swp1 will be handled first
                # so at this point the cache has no idea if ifindex 5 is valid or not.
                # But since we've made it this far we can assume that this is probably a
                # valid device and will use sysfs to confirm.
                master_device_path = '/sys/class/net/%s/master' % ifname

                if os.path.exists(master_device_path):
                    # this check is necessary because realpath doesn't return None on error
                    # it returns it's input argument...
                    # >>> os.path.realpath('/sys/class/net/device_not_found')
                    # '/sys/class/net/device_not_found'
                    # >>>
                    master_ifname = os.path.basename(os.path.realpath(master_device_path))

            if master_ifname in self._masters_and_slaves:
                self._masters_and_slaves[master_ifname].add(ifname)
            else:
                self._masters_and_slaves[master_ifname] = set([ifname])

    def force_remove_link(self, ifname):
        """
        When calling link_del (RTM_DELLINK) we need to manually remove the
        associated cache entry - the RTM_DELLINK notification isn't received
        instantaneously - we don't want to keep stale value in our cache
        :param ifname:
        :return:
        """
        try:
            ifindex = self.get_ifindex(ifname)
        except (KeyError, NetlinkCacheIfnameNotFoundError):
            ifindex = None
        self.remove_link(None, link_ifname=ifname, link_ifindex=ifindex)

    def remove_link(self, link, link_ifname=None, link_ifindex=None):
        """ Process RTM_DELLINK packet and purge the cache accordingly """
        if link:
            ifindex = link.ifindex
            ifname = link.get_attribute_value(nlpacket.Link.IFLA_IFNAME)
            try:
                # RTM_DELLINK received - we can now remove ifname from the
                # ignore_rtm_newlinkq list. We don't bother checkin if the
                # if name is present in the list (because it's likely in)
                with self._ignore_rtm_newlinkq_lock:
                    self._ignore_rtm_newlinkq.remove(ifname)
            except ValueError:
                pass
        else:
            ifname = link_ifname
            ifindex = link_ifindex

        try:
            del linkCache.links[ifname]
        except:
            pass

        link_ifla_master = None
        # when an enslaved device is removed we receive the RTM_DELLINK
        # notification without the IFLA_MASTER attribute, we need to
        # get the previous cached value in order to correctly update the
        # _masters_and_slaves dictionary

        with self._cache_lock:
            try:
                try:
                    ifla_master_attr = self._link_cache[ifname].attributes.get(nlpacket.Link.IFLA_MASTER)
                    if ifla_master_attr:
                        link_ifla_master = ifla_master_attr.get_pretty_value()
                except KeyError:
                    # link is not present in the cache
                    pass
                except AttributeError:
                    # if this code is ever reached this is very concerning and
                    # should never happen as _link_cache should always contains
                    # nlpacket.NetlinkPacket maybe have some extra handling here
                    # just in case?
                    pass
                finally:
                    del self._link_cache[ifname]
            except KeyError:
                # KeyError means that the link doesn't exists in the cache
                log.debug('del _link_cache: KeyError ifname: %s' % ifname)

            try:
                del self._ifname_by_ifindex[ifindex]
            except KeyError:
                log.debug('del _ifname_by_ifindex: KeyError ifindex: %s' % ifindex)

            try:
                del self._ifindex_by_ifname[ifname]
            except KeyError:
                log.debug('del _ifindex_by_ifname: KeyError ifname: %s' % ifname)

            try:
                del self._addr_cache[ifname]
            except KeyError:
                log.debug('del _addr_cache: KeyError ifname: %s' % ifname)

            try:
                del self._masters_and_slaves[ifname]
            except KeyError:
                log.debug('del _masters_and_slaves: KeyError ifname: %s' % ifname)

            # if the device was enslaved to another device we need to remove
            # it's entry from our _masters_and_slaves dictionary
            if link_ifla_master > 0:
                try:
                    master_ifname = self.get_ifname(link_ifla_master)
                    self._masters_and_slaves[master_ifname].remove(ifname)
                except (NetlinkCacheIfindexNotFoundError, ValueError) as e:
                    log.debug('cache: remove_link: %s: %s' % (ifname, str(e)))
                except KeyError:
                    log.debug('_masters_and_slaves[%s].remove(%s): KeyError' % (master_ifname, ifname))

    def _address_get_ifname_and_ifindex(self, addr):
        ifindex = addr.ifindex
        label = addr.get_attribute_value(nlpacket.Address.IFA_LABEL)

        if not label:
            try:
                label = self.get_ifname(ifindex)
            except NetlinkCacheIfindexNotFoundError:
                pass

        return label, ifindex

    def add_address(self, addr):
        ifname, ifindex = self._address_get_ifname_and_ifindex(addr)

        if not ifname:
            log.debug('nlcache: add_address: cannot cache addr for ifindex %s' % ifindex)
            return

        with self._cache_lock:
            if ifname in self._addr_cache:
                self._addr_cache[ifname].append(addr)
            else:
                self._addr_cache[ifname] = [addr]

    def remove_address(self, addr_to_remove):
        ifname, ifindex = self._address_get_ifname_and_ifindex(addr_to_remove)

        with self._cache_lock:
            # iterate through the interface addresses
            # to find which one to remove from the cache
            addrs_for_interface = self._addr_cache.get(ifname, [])

            if not addrs_for_interface:
                return

            list_addr_to_remove = []

            for addr in addrs_for_interface:
                # compare each object attribute to see if they match
                addr_match = False

                for ifa_attr in self._ifa_attributes:
                    if addr.get_attribute_value(ifa_attr) != addr_to_remove.get_attribute_value(ifa_attr):
                        addr_match = False
                        break
                    addr_match = True
                    # if the address attribute matches we need to remove this one

                if addr_match:
                    list_addr_to_remove.append(addr)

            for addr in list_addr_to_remove:
                try:
                    addrs_for_interface.remove(addr)
                except ValueError as e:
                    log.debug('nlcache: remove_address: exception: %s' % e)

    def get_addresses_list(self, ifname):
        addresses = []
        try:
            with self._cache_lock:
                for addr in self._addr_cache[ifname] or []:
                    addresses.append(addr.attributes[nlpacket.Address.IFA_ADDRESS].value)
                return addresses
        except (KeyError, AttributeError):
            return addresses


    # old

    def get_link_obj(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname]
        except KeyError:
            return None

    def get_link_info_slave_data(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_DATA]
        except (KeyError, AttributeError):
            return {}

    def is_link_kind(self, ifname, _type):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_KIND] == _type
        except (KeyError, AttributeError):
            return False

    def is_link_slave_kind(self, ifname, _type):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_KIND] == _type
        except (KeyError, AttributeError):
            return False

    ##########################################################################
    # BRIDGE #################################################################
    ##########################################################################

    def is_link_brport(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_KIND] == 'bridge'
        except (KeyError, AttributeError):
            return False

    def brport_exists(self, bridge_ifname, brport_ifname):
        try:
            with self._cache_lock:
                return brport_ifname in self._masters_and_slaves[bridge_ifname]
        except KeyError:
            return False

    def get_brport_learning(self, ifname):
        try:
            with self._cache_lock:
                return self._link_cache[ifname].attributes[nlpacket.Link.IFLA_LINKINFO].value[nlpacket.Link.IFLA_INFO_SLAVE_DATA][nlpacket.Link.IFLA_BRPORT_LEARNING]
        except (KeyError, AttributeError):
            return 0


if ifupdownflags.flags.DRYRUN:
    # When possible we shouldn't have dryrun checks in the code
    # but have DryRunNetlinkCache to inherit NetlinkCache and overrides
    # all the methods - not sure it's optimal for maintenance and we might
    # forgot to add new methods there but at least we won't waste time checking for dryrun everywhere
    #raise NotImplementedError('nlcache.py: see comment in the code')
    pass


class NetlinkListenerWithCache(nllistener.NetlinkManagerWithListener, DryRun):

    __instance = None

    @staticmethod
    def get_instance(log_level=None):
        if not NetlinkListenerWithCache.__instance:
            try:
                NetlinkListenerWithCache.__instance = NetlinkListenerWithCache(log_level=WARNING)
            except Exception as e:
                log.error('NetlinkListenerWithCache: getInstance: %s' % e)
                traceback.print_exc()
        return NetlinkListenerWithCache.__instance

    def __init__(self, log_level):
        """

        :param log_level:
        """
        if NetlinkListenerWithCache.__instance:
            raise RuntimeError("NetlinkListenerWithCache: invalid access. Please use NetlinkListenerWithCache.getInstance()")
        else:
            NetlinkListenerWithCache.__instance = self

        nllistener.NetlinkManagerWithListener.__init__(self, (
            nlpacket.RTMGRP_ALL
                #nlpacket.RTMGRP_LINK
                #| nlpacket.RTMGRP_NOTIFY
                #| nlpacket.RTMGRP_IPV4_IFADDR
                #| nlpacket.RTMGRP_IPV6_IFADDR
        ), error_notification=True)

        DryRun.__init__(self)

        signal.signal(signal.SIGTERM, self.signal_term_handler)
        signal.signal(signal.SIGINT, self.signal_int_handler)

        ### DEBUG
        #self.debug_link(True)
        ###

        # we need to proctect the access to the old cache with a lock
        self.OLD_CACHE_LOCK = threading.Lock()
        self.FILL_OLD_CACHE = True

        self.cache = _NetlinkCache()

        # set specific log level to lower-level API
        #nllistener.log.setLevel(log_level)
        #nlpacket.log.setLevel(log_level)
        #nlmanager.log.setLevel(log_level)

        nlpacket.mac_int_to_str = lambda mac_int: ':'.join(('%012x' % mac_int)[i:i + 2] for i in range(0, 12, 2))
        # Override the nlmanager's mac_int_to_str function
        # Return an integer in MAC string format: xx:xx:xx:xx:xx:xx instead of xxxx.xxxx.xxxx

        self.listener.supported_messages = (
            nlpacket.RTM_NEWLINK,
            nlpacket.RTM_DELLINK,
            nlpacket.RTM_NEWADDR,
            nlpacket.RTM_DELADDR
        )
        self.listener.ignore_messages = (
            nlpacket.RTM_GETLINK,
            nlpacket.RTM_GETADDR,
            nlpacket.RTM_GETNEIGH,
            nlpacket.RTM_GETROUTE,
            nlpacket.RTM_GETQDISC,
            nlpacket.RTM_NEWNEIGH,
            nlpacket.RTM_DELNEIGH,
            nlpacket.RTM_NEWROUTE,
            nlpacket.RTM_DELROUTE,
            nlpacket.NLMSG_ERROR,  # should be in supported_messages ?
            nlpacket.NLMSG_DONE  # should be in supported_messages ?
        )

        self.workq_handler = {
            self.WORKQ_SERVICE_NETLINK_QUEUE: self.service_netlinkq,
        }

        # NetlinkListenerWithCache first dumps links and addresses then start
        # a worker thread before returning. The worker thread processes the
        # workq mainly to service (process) the netlinkq which contains our
        # netlink packet (notification coming from the Kernel).
        # When the main thread is making netlin requests (i.e. bridge add etc
        # ...) the main thread will sleep (thread.event.wait) until we notify
        # it when receiving an ack associated with the request. The request
        # may fail and the kernel won't return an ACK but instead return a
        # NLMSG_ERROR packet. We need to store those packet separatly because:
        #   - we could have several NLMSG_ERROR for different requests from
        #     different threads (in a multi-threaded ifupdown2 case)
        #   - we want to decode the packet and tell the user/log or even raise
        #     an exception with the appropriate message.
        #     User must check the return value of it's netlink requests and
        #     catch any exceptions, for that purpose please use API:
        #        - tx_nlpacket_get_response_with_error
        self.errorq = list()
        self.errorq_lock = threading.Lock()
        self.errorq_enabled = True

        # when ifupdown2 starts, we need to fill the netlink cache
        # GET_LINK/ADDR request are asynchronous, we need to block
        # and wait for the cache to be filled. We are using this one
        # time netlinkq_notify_event to wait for the cache completion event
        self.netlinkq_notify_event = None

        # get all links and wait for the cache to be filled
        self.get_all_links_wait_netlinkq()
        # get all addresses and wait for cache to be filled
        self.get_all_addresses_wait_netlinkq()

        # another threading event to make sure that the netlinkq worker thread is ready
        self.is_ready = threading.Event()

        # TODO: on ifquery we shoudn't start any thread (including listener in NetlinkListener)
        # only for standalone code.
        import sys
        for arg in sys.argv:
            if 'ifquery' in arg:
                self.worker = None
                return

        # start the netlinkq worker thread
        self.worker = threading.Thread(target=self.main, name='NetlinkListenerWithCache')
        self.worker.start()
        self.is_ready.wait()

    def __str__(self):
        return "NetlinkListenerWithCache"

    def cleanup(self):
        # passing 0, 0 to the handler so it doesn't log.info
        self.signal_term_handler(0, 0)

        if self.worker:
            self.worker.join()

    def main(self):
        self.is_ready.set()

        # This loop has two jobs:
        # - process items on our workq
        # - process netlink messages on our netlinkq, messages are placed there via our NetlinkListener
        try:
            while True:
                # Sleep until our alarm goes off...NetlinkListener will set the alarm once it
                # has placed a NetlinkPacket object on our netlinkq. If someone places an item on
                # our workq they should also set our alarm...if they don't it is not the end of
                # the world as we will wake up in 1s anyway to check to see if our shutdown_event
                # has been set.
                self.alarm.wait(0.1)
                # when ifupdown2 is not running we could change the timeout to 1 sec or more (daemon mode)
                # the daemon can also put a hook (pyinotify) on /etc/network/interfaces
                # if we detect changes to that file it probably means that ifupdown2 will be called very soon
                # then we can scale up our ops (or maybe unpause some of them)
                # lets study the use cases
                self.alarm.clear()
                if self.shutdown_event.is_set():
                    break

                while not self.workq.empty():
                    (event, options) = self.workq.get()

                    if event == self.WORKQ_SERVICE_NETLINK_QUEUE:
                        self.service_netlinkq(self.netlinkq_notify_event)
                    elif event == self.WORKQ_SERVICE_ERROR:
                        log.error('NetlinkListenerWithCache: WORKQ_SERVICE_ERROR')
                    else:
                        raise Exception("Unsupported workq event %s" % event)
        except:
            raise
        finally:
            # il faut surement mettre un try/except autour de la boucle au dessus
            # car s'il y a une exception on ne quitte pas le listener thread
            self.listener.shutdown_event.set()
            self.listener.join()

    def _addr_dump_entry(self, ifaces, addr_packet, addr_ifname, ifa_attr):

        def _addr_filter(addr_ifname, addr):
            return addr_ifname == 'lo' and addr in ['127.0.0.1/8', '::1/128', '0.0.0.0']

        attribute = addr_packet.attributes.get(ifa_attr)

        if attribute:
            address = attribute.get_pretty_value(str)

            if hasattr(addr_packet, 'prefixlen'):
                address = '%s' % address

            if _addr_filter(addr_ifname, address):
                return

            addr_family = nlpacket.NetlinkPacket.af_family_to_string.get(addr_packet.family)
            if not addr_family:
                return

            ifaces[addr_ifname]['addrs'][address] = {
                'type': addr_family,
                'scope': addr_packet.scope,
            }

    def __addr_dump_extract_ifname(self, addr_packet):
        addr_ifname_attr = addr_packet.attributes.get(nlpacket.Address.IFA_LABEL)

        if addr_ifname_attr:
            return addr_ifname_attr.get_pretty_value(str)
        else:
            try:
                return self.cache.get_ifname(addr_packet.ifindex)
            except NetlinkCacheIfindexNotFoundError:
                return None

    def rx_rtm_newaddr(self, rxed_addr_packet):
        super(NetlinkListenerWithCache, self).rx_rtm_newaddr(rxed_addr_packet)
        self.cache.add_address(rxed_addr_packet)

        #self.cache.DEBUG_IFNAME('bond0', with_addresses=True)

        # we are only caching the first dump in the old cache
        # after the first dump this check should always fail.
        #if not self.netlinkq_notify_event:
        #    return

        ifa_address_attributes = [
            nlpacket.Address.IFA_ADDRESS,
            nlpacket.Address.IFA_LOCAL,
            nlpacket.Address.IFA_BROADCAST,
            nlpacket.Address.IFA_ANYCAST,
            nlpacket.Address.IFA_MULTICAST
        ]
        ifaces = dict()
        try:
            for addr_packet in [rxed_addr_packet]:
                addr_ifname = self.__addr_dump_extract_ifname(addr_packet)

                if not addr_ifname:
                    continue

                if addr_packet.family not in [socket.AF_INET, socket.AF_INET6]:
                    continue

                if addr_ifname not in ifaces:
                    ifaces[addr_ifname] = {'addrs': OrderedDict({})}

                for ifa_attr in ifa_address_attributes:
                    self._addr_dump_entry(ifaces, addr_packet, addr_ifname, ifa_attr)

            for ifname, addrsattrs in ifaces.items():
                addrs_attrs = addrsattrs.get('addrs', {})

                with self.OLD_CACHE_LOCK:
                    if ifname not in linkCache.links:
                        linkCache.links[ifname] = {}
                    if 'addrs' not in linkCache.links[ifname]:
                        linkCache.links[ifname]['addrs'] = OrderedDict({})

                if not addrs_attrs:
                    continue

                for addr, attrs in addrs_attrs.items():
                    with self.OLD_CACHE_LOCK:
                        linkCache.links[ifname]['addrs'][addr] = attrs

        except Exception as e:
            import traceback
            traceback.print_exc()
            log.error('netlink: ip addr show: %s' % str(e))

    def rx_rtm_dellink(self, link):
        # cache only supports AF_UNSPEC for now
        if link.family != socket.AF_UNSPEC:
            return
        super(NetlinkListenerWithCache, self).rx_rtm_dellink(link)
        self.cache.remove_link(link)

    def rx_rtm_deladdr(self, addr):
        #exit(1)
        import os
        os.system('echo "a" >> /tmp/a') #log.error('b')
        super(NetlinkListenerWithCache, self).rx_rtm_deladdr(addr)
        self.cache.remove_address(addr)

    def rx_rtm_newlink(self, rxed_link_packet):
        # cache only supports AF_UNSPEC for now
        # we can modify the cache to support more family:
        # cache {
        #    intf_name: {
        #       AF_UNSPEC: NetlinkObj,
        #       AF_BRIDGE: NetlinkObj
        #    },
        # }
        if rxed_link_packet.family != socket.AF_UNSPEC:
            return

        super(NetlinkListenerWithCache, self).rx_rtm_newlink(rxed_link_packet)
        self.cache.add_link(rxed_link_packet)

        # old style caching:
        ifla_address = {'attr': nlpacket.Link.IFLA_ADDRESS, 'name': 'hwaddress', 'func': str}

        ifla_attributes = [
            {
                'attr': nlpacket.Link.IFLA_LINK,
                'name': 'link',
                'func': lambda x: self.cache.get_ifname(x) if x > 0 else None
            },
            {
                'attr': nlpacket.Link.IFLA_MASTER,
                'name': 'master',
                'func': lambda x: self.cache.get_ifname(x) if x > 0 else None
            },
            {
                'attr': nlpacket.Link.IFLA_IFNAME,
                'name': 'ifname',
                'func': str,
            },
            {
                'attr': nlpacket.Link.IFLA_MTU,
                'name': 'mtu',
                'func': str
            },
            {
                'attr': nlpacket.Link.IFLA_OPERSTATE,
                'name': 'state',
                'func': lambda x: '0%x' % int(x) if x > len(nlpacket.Link.oper_to_string) else nlpacket.Link.oper_to_string[x][8:]
            },
            {
                'attr': nlpacket.Link.IFLA_AF_SPEC,
                'name': 'af_spec',
                'func': dict
            }
        ]

        def _link_dump_attr(_link, _ifla_attributes, _dump):
            for obj in _ifla_attributes:
                attr = _link.attributes.get(obj['attr'])
                if attr:
                    _dump[obj['name']] = attr.get_pretty_value(obj=obj.get('func'))

        def _link_dump_linkinfo(link, dump):

            def _link_dump_linkdata_attr(linkdata, ifla_linkdata_attr, dump):
                for obj in ifla_linkdata_attr:
                    attr = obj['attr']
                    if attr in linkdata:
                        func = obj.get('func')
                        value = linkdata.get(attr)

                        if func:
                            value = func(value)

                        if value or obj['accept_none']:
                            dump[obj['name']] = value

            def _link_dump_info_data_vlan(ifname, linkdata):
                return {
                    'vlanid': str(linkdata.get(nlpacket.Link.IFLA_VLAN_ID, '')),
                    'vlan_protocol': linkdata.get(nlpacket.Link.IFLA_VLAN_PROTOCOL)
                }

            def _link_dump_info_data_vrf(ifname, linkdata):
                vrf_info = {'table': str(linkdata.get(nlpacket.Link.IFLA_VRF_TABLE, ''))}

                # to remove later when moved to a true netlink cache
                linkCache.vrfs[ifname] = vrf_info
                return vrf_info

            def IN_MULTICAST(a):
                """
                    /include/uapi/linux/in.h

                    #define IN_CLASSD(a)            ((((long int) (a)) & 0xf0000000) == 0xe0000000)
                    #define IN_MULTICAST(a)         IN_CLASSD(a)
                """
                return (int(a) & 0xf0000000) == 0xe0000000

            def _link_dump_info_data_vxlan(ifname, linkdata):
                ifla_vxlan_attributes = [
                    {
                        'attr': nlpacket.Link.IFLA_VXLAN_LOCAL,
                        'name': 'local',
                        'func': str,
                        'accept_none': True
                    },
                    {
                        'attr': nlpacket.Link.IFLA_VXLAN_LOCAL6,
                        'name': 'local',
                        'func': str,
                        'accept_none': True
                    },
                    {
                        'attr': nlpacket.Link.IFLA_VXLAN_GROUP,
                        'name': 'svcnode',
                        'func': lambda x: str(x) if not IN_MULTICAST(x) else None,
                        'accept_none': False
                    },
                    {
                        'attr': nlpacket.Link.IFLA_VXLAN_GROUP6,
                        'name': 'svcnode',
                        'func': lambda x: str(x) if not IN_MULTICAST(x) else None,
                        'accept_none': False
                    },
                    {
                        'attr': nlpacket.Link.IFLA_VXLAN_LEARNING,
                        'name': 'learning',
                        'func': lambda x: 'on' if x else 'off',
                        'accept_none': True
                    }
                ]

                for attr, value in (
                        ('learning', 'on'),
                        ('svcnode', None),
                        ('vxlanid', str(linkdata.get(nlpacket.Link.IFLA_VXLAN_ID, ''))),
                        ('ageing', str(linkdata.get(nlpacket.Link.IFLA_VXLAN_AGEING, ''))),
                        (nlpacket.Link.IFLA_VXLAN_PORT, linkdata.get(nlpacket.Link.IFLA_VXLAN_PORT))
                ):
                    linkdata[attr] = value
                _link_dump_linkdata_attr(linkdata, ifla_vxlan_attributes, linkdata)
                return linkdata

            def _link_dump_info_data_bond(ifname, linkdata):
                linkinfo = {}

                ifla_bond_attributes = (
                    nlpacket.Link.IFLA_BOND_MODE,
                    nlpacket.Link.IFLA_BOND_MIIMON,
                    nlpacket.Link.IFLA_BOND_USE_CARRIER,
                    nlpacket.Link.IFLA_BOND_AD_LACP_RATE,
                    nlpacket.Link.IFLA_BOND_XMIT_HASH_POLICY,
                    nlpacket.Link.IFLA_BOND_MIN_LINKS,
                    nlpacket.Link.IFLA_BOND_NUM_PEER_NOTIF,
                    nlpacket.Link.IFLA_BOND_AD_ACTOR_SYSTEM,
                    nlpacket.Link.IFLA_BOND_AD_ACTOR_SYS_PRIO,
                    nlpacket.Link.IFLA_BOND_AD_LACP_BYPASS,
                    nlpacket.Link.IFLA_BOND_UPDELAY,
                    nlpacket.Link.IFLA_BOND_DOWNDELAY,
                )

                for nl_attr in ifla_bond_attributes:
                    try:
                        linkinfo[nl_attr] = linkdata.get(nl_attr)
                    except Exception as e:
                        log.debug('%s: parsing bond IFLA_INFO_DATA (%s): %s'
                                          % (ifname, nl_attr, str(e)))
                return linkinfo

            def _link_dump_info_data_bridge(ifname, linkdata):
                linkinfo = {}

                # this dict contains the netlink attribute, cache key,
                # and a callable to translate the netlink value into
                # whatever value we need to store in the old cache to
                # make sure we don't break anything
                ifla_bridge_attributes = (
                    (nlpacket.Link.IFLA_BR_UNSPEC, nlpacket.Link.IFLA_BR_UNSPEC, None),
                    (nlpacket.Link.IFLA_BR_FORWARD_DELAY, "fd", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_HELLO_TIME, "hello", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_MAX_AGE, "maxage", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_AGEING_TIME, "ageing", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_STP_STATE, "stp", lambda x: 'yes' if x else 'no'),
                    (nlpacket.Link.IFLA_BR_PRIORITY, "bridgeprio", str),
                    (nlpacket.Link.IFLA_BR_VLAN_FILTERING, 'vlan_filtering', str),
                    (nlpacket.Link.IFLA_BR_VLAN_PROTOCOL, "vlan-protocol", str),
                    (nlpacket.Link.IFLA_BR_GROUP_FWD_MASK, nlpacket.Link.IFLA_BR_GROUP_FWD_MASK, None),
                    (nlpacket.Link.IFLA_BR_ROOT_ID, nlpacket.Link.IFLA_BR_ROOT_ID, None),
                    (nlpacket.Link.IFLA_BR_BRIDGE_ID, nlpacket.Link.IFLA_BR_BRIDGE_ID, None),
                    (nlpacket.Link.IFLA_BR_ROOT_PORT, nlpacket.Link.IFLA_BR_ROOT_PORT, None),
                    (nlpacket.Link.IFLA_BR_ROOT_PATH_COST, nlpacket.Link.IFLA_BR_ROOT_PATH_COST, None),
                    (nlpacket.Link.IFLA_BR_TOPOLOGY_CHANGE, nlpacket.Link.IFLA_BR_TOPOLOGY_CHANGE, None),
                    (nlpacket.Link.IFLA_BR_TOPOLOGY_CHANGE_DETECTED, nlpacket.Link.IFLA_BR_TOPOLOGY_CHANGE_DETECTED, None),
                    (nlpacket.Link.IFLA_BR_HELLO_TIMER, nlpacket.Link.IFLA_BR_HELLO_TIMER, None),
                    (nlpacket.Link.IFLA_BR_TCN_TIMER, nlpacket.Link.IFLA_BR_TCN_TIMER, None),
                    (nlpacket.Link.IFLA_BR_TOPOLOGY_CHANGE_TIMER, nlpacket.Link.IFLA_BR_TOPOLOGY_CHANGE_TIMER, None),
                    (nlpacket.Link.IFLA_BR_GC_TIMER, nlpacket.Link.IFLA_BR_GC_TIMER, None),
                    (nlpacket.Link.IFLA_BR_GROUP_ADDR, nlpacket.Link.IFLA_BR_GROUP_ADDR, None),
                    (nlpacket.Link.IFLA_BR_FDB_FLUSH, nlpacket.Link.IFLA_BR_FDB_FLUSH, None),
                    (nlpacket.Link.IFLA_BR_MCAST_ROUTER, "mcrouter", str),
                    (nlpacket.Link.IFLA_BR_MCAST_SNOOPING, "mcsnoop", str),
                    (nlpacket.Link.IFLA_BR_MCAST_QUERY_USE_IFADDR, "mcqifaddr", str),
                    (nlpacket.Link.IFLA_BR_MCAST_QUERIER, "mcquerier", str),
                    (nlpacket.Link.IFLA_BR_MCAST_HASH_ELASTICITY, "hashel", str),
                    (nlpacket.Link.IFLA_BR_MCAST_HASH_MAX, "hashmax", str),
                    (nlpacket.Link.IFLA_BR_MCAST_LAST_MEMBER_CNT, "mclmc", str),
                    (nlpacket.Link.IFLA_BR_MCAST_STARTUP_QUERY_CNT, "mcsqc", str),
                    (nlpacket.Link.IFLA_BR_MCAST_LAST_MEMBER_INTVL, "mclmi", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_MCAST_MEMBERSHIP_INTVL, "mcmi", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_MCAST_QUERIER_INTVL, "mcqpi", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_MCAST_QUERY_INTVL, "mcqi", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_MCAST_QUERY_RESPONSE_INTVL, "mcqri", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_MCAST_STARTUP_QUERY_INTVL, "mcsqi", lambda x: str(x / 100)),
                    (nlpacket.Link.IFLA_BR_NF_CALL_IPTABLES, nlpacket.Link.IFLA_BR_NF_CALL_IPTABLES, None),
                    (nlpacket.Link.IFLA_BR_NF_CALL_IP6TABLES, nlpacket.Link.IFLA_BR_NF_CALL_IP6TABLES, None),
                    (nlpacket.Link.IFLA_BR_NF_CALL_ARPTABLES, nlpacket.Link.IFLA_BR_NF_CALL_ARPTABLES, None),
                    (nlpacket.Link.IFLA_BR_VLAN_DEFAULT_PVID, nlpacket.Link.IFLA_BR_VLAN_DEFAULT_PVID, None),
                    (nlpacket.Link.IFLA_BR_PAD, nlpacket.Link.IFLA_BR_PAD, None),
                    (nlpacket.Link.IFLA_BR_VLAN_STATS_ENABLED, "vlan-stats", str),
                    (nlpacket.Link.IFLA_BR_MCAST_STATS_ENABLED, "mcstats", str),
                    (nlpacket.Link.IFLA_BR_MCAST_IGMP_VERSION, "igmp-version", str),
                    (nlpacket.Link.IFLA_BR_MCAST_MLD_VERSION, "mld-version", str)
                )

                for nl_attr, cache_key, func in ifla_bridge_attributes:
                    try:
                        if func:
                            linkinfo[cache_key] = func(linkdata.get(nl_attr))
                        else:
                            linkinfo[cache_key] = linkdata.get(nl_attr)

                        # we also store the value in pure netlink,
                        # to make the transition easier in the future
                        linkinfo[nl_attr] = linkdata.get(nl_attr)
                    except Exception as excption:
                        log.error('%s: parsing birdge IFLA_INFO_DATA %s: %s' % (ifname, nl_attr, str(excption)))
                return linkinfo

            link_kind_handlers = {
                'vlan': _link_dump_info_data_vlan,
                'vrf': _link_dump_info_data_vrf,
                'vxlan': _link_dump_info_data_vxlan,
                'bond': _link_dump_info_data_bond,
                'bridge': _link_dump_info_data_bridge
            }

            linkinfo = dict(link.attributes[nlpacket.Link.IFLA_LINKINFO].get_pretty_value(dict))

            if linkinfo:
                info_kind = linkinfo.get(nlpacket.Link.IFLA_INFO_KIND)
                info_data = dict(linkinfo.get(nlpacket.Link.IFLA_INFO_DATA, {}))

                info_slave_kind = linkinfo.get(nlpacket.Link.IFLA_INFO_SLAVE_KIND)
                info_slave_data = dict(linkinfo.get(nlpacket.Link.IFLA_INFO_SLAVE_DATA, {}))

                dump['kind'] = info_kind
                dump['slave_kind'] = info_slave_kind

                if info_data:
                    link_kind_handler = link_kind_handlers.get(info_kind)
                    if callable(link_kind_handler):
                        dump['linkinfo'] = link_kind_handler(dump['ifname'], info_data)

                if info_slave_data:
                    dump['info_slave_data'] = info_slave_data

        links = {}
        try:
            dump = dict()

            flags = []
            for flag, string in nlpacket.Link.flag_to_string.items():
                if rxed_link_packet.flags & flag:
                    flags.append(string[4:])

            dump['flags'] = flags
            dump['ifflag'] = 'UP' if 'UP' in flags else 'DOWN'
            dump['ifindex'] = str(rxed_link_packet.ifindex)

            if rxed_link_packet.device_type == nlpacket.Link.ARPHRD_ETHER:
                _link_dump_attr(rxed_link_packet, [ifla_address], dump)

            _link_dump_attr(rxed_link_packet, ifla_attributes, dump)

            if nlpacket.Link.IFLA_LINKINFO in rxed_link_packet.attributes:
                _link_dump_linkinfo(rxed_link_packet, dump)

            links[dump['ifname']] = dump
        except (NetlinkCacheIfindexNotFoundError, NetlinkCacheIfnameNotFoundError) as e:
            log.debug('netlink: ip link show: %s' % str(e))
        except Exception as e:
            log.error('netlink: ip link show: %s' % str(e))

        with self.OLD_CACHE_LOCK:
            try:
                for ifname, linkattrs in links.items():
                    addrs = linkCache.links.get(ifname, {}).get('addrs', OrderedDict({}))
                    linkCache.links[ifname] = linkattrs
                    linkCache.links[ifname]['addrs'] = addrs
            except:
                pass

    def tx_nlpacket_get_response_with_error(self, nl_packet):
        """
            After getting an ACK we need to check if this ACK was in fact an
            error (NLMSG_ERROR). This function go through the .errorq list to
            find the error packet associated with our request.
            If found, we process it and raise an exception with the appropriate
            information/message.

        :param nl_packet:
        :return:
        """
        self.tx_nlpacket_get_response(nl_packet)

        error_packet = None
        index = 0

        with self.errorq_lock:
            for error in self.errorq:
                if error.seq == nl_packet.seq and error.pid == nl_packet.pid:
                    error_packet = error
                    break
                index += 1

            if error_packet:
                del self.errorq[index]

        if not error_packet:
            return True

        error_code = abs(error_packet.negative_errno)

        if error_packet.msgtype == nlpacket.NLMSG_DONE or not error_code:
            # code NLE_SUCCESS...this is an ACK
            return True

        error_code_str = error_packet.error_to_string.get(error_code)

        if self.debug:
            error_packet.dump()

        if error_code_str:
            error_code_human_str = error_packet.error_to_human_readable_string.get(error_code)
            error_str = '%s (%s:%s)' % (error_code_human_str, error_code_str, error_code)
        else:
            try:
                # os.strerror might raise ValueError
                strerror = os.strerror(error_code)

                if strerror:
                    error_str = 'operation failed with \'%s\' (%s)' % (strerror, error_code)
                else:
                    error_str = 'operation failed with code %s' % error_code

            except ValueError:
                error_str = 'operation failed with code %s' % error_code

        raise Exception(error_str)

    def tx_nlpacket_get_response_with_error_and_wait_for_cache(self, ifname, nl_packet):
        """
        The netlink request are asynchronus, but sometimes the main thread/user
         would like to wait until the result of the request is cached. To do so
         a cache event for ifname and nl_packet.msgtype is registered. Then the
         netlink packet is TXed, errors are checked then we sleep until the
         cache event is set (or we reach the timeout). This allows us to reliably
         make sure is up to date with newly created/removed devices or addresses.
        :param nl_packet:
        :return:
        """
        wait_event_registered = self.cache.register_wait_event(ifname, nl_packet.msgtype)

        try:
            result = self.tx_nlpacket_get_response_with_error(nl_packet)
        except:
            # an error was caught, we need to unregister the event and raise again
            self.cache.unregister_wait_event()
            raise

        if wait_event_registered:
            self.cache.wait_event()

        return result

    def get_all_links_wait_netlinkq(self):
        # create netlinkq notify event so we can wait until the links are cached
        self.netlinkq_notify_event = threading.Event()
        self.get_all_links()
        # block until the netlinkq was serviced and cached
        self.service_netlinkq(self.netlinkq_notify_event)
        self.netlinkq_notify_event.wait()
        self.netlinkq_notify_event.clear()

    def get_all_addresses_wait_netlinkq(self):
        self.netlinkq_notify_event = threading.Event()
        self.get_all_addresses()
        # block until the netlinkq was serviced and cached
        self.service_netlinkq(self.netlinkq_notify_event)
        self.netlinkq_notify_event.wait()
        self.netlinkq_notify_event.clear()
        self.netlinkq_notify_event = False

    def _link_add(self, ifindex, ifname, kind, ifla_info_data):
        """
        Build and TX a RTM_NEWLINK message to add the desired interface
        """
        debug = nlpacket.RTM_NEWLINK in self.debug

        link = nlpacket.Link(nlpacket.RTM_NEWLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_CREATE | nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('Bxxxiii', socket.AF_UNSPEC, 0, 0, 0)
        link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)

        if ifindex:
            link.add_attribute(nlpacket.Link.IFLA_LINK, ifindex)

        link.add_attribute(nlpacket.Link.IFLA_LINKINFO, {
            nlpacket.Link.IFLA_INFO_KIND: kind,
            nlpacket.Link.IFLA_INFO_DATA: ifla_info_data
        })
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response_with_error_and_wait_for_cache(ifname, link)

    def _link_add_set(self, kind,
                     ifname=None,
                     ifindex=0,
                     slave_kind=None,
                     ifla={},
                     ifla_info_data={},
                     ifla_info_slave_data={}):
        """
        Build and TX a RTM_NEWLINK message to add the desired interface
        """
        debug = nlpacket.RTM_NEWLINK in self.debug

        link = nlpacket.Link(nlpacket.RTM_NEWLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_CREATE | nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('Bxxxiii', socket.AF_UNSPEC, ifindex, 0, 0)

        for nl_attr, value in ifla.items():
            link.add_attribute(nl_attr, value)

        if ifname:
            link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)

        linkinfo = dict()
        if kind:
            linkinfo[nlpacket.Link.IFLA_INFO_KIND] = kind
            linkinfo[nlpacket.Link.IFLA_INFO_DATA] = ifla_info_data
        if slave_kind:
            linkinfo[nlpacket.Link.IFLA_INFO_SLAVE_KIND] = slave_kind
            linkinfo[nlpacket.Link.IFLA_INFO_SLAVE_DATA] = ifla_info_slave_data
        link.add_attribute(nlpacket.Link.IFLA_LINKINFO, linkinfo)

        link.build_message(self.sequence.next(), self.pid)

        return self.tx_nlpacket_get_response_with_error(link)

    def _link_del(self, ifindex=None, ifname=None):
        """
            this call can raise NetlinkCacheIfnameNotFoundError
        """
        if not ifindex and not ifname:
            raise ValueError('invalid ifindex and/or ifname')

        if not ifindex:
            ifindex = self.cache.get_ifindex(ifname)

        debug = nlpacket.RTM_DELLINK in self.debug

        link = nlpacket.Link(nlpacket.RTM_DELLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('Bxxxiii', socket.AF_UNSPEC, ifindex, 0, 0)
        link.build_message(self.sequence.next(), self.pid)

        try:
            # We need to register this ifname so the cache can ignore and discard
            # any further RTM_NEWLINK packet until we receive the associated
            # RTM_DELLINK notification
            self.cache.append_to_ignore_rtm_newlinkq(ifname)

            result = self.tx_nlpacket_get_response_with_error(link)

            # Manually purge the cache entry for ifname to make sure we don't have
            # any stale value in our cache
            self.cache.force_remove_link(ifname)
            return result
        except:
            # Something went wrong while sending the RTM_DELLINK request
            # we need to clear ifname from the ignore_rtm_newlinkq list
            self.cache.remove_from_ignore_rtm_newlinkq(ifname)
            raise

    def _link_set_master(self, ifname, master_ifindex=0, state=None):
        """
            ip link set %ifname master %master_ifindex %state
            use master_ifindex=0 for nomaster
        """
        if state == 'up':
            if_change = nlpacket.Link.IFF_UP
            if_flags = nlpacket.Link.IFF_UP
        elif state == 'down':
            if_change = nlpacket.Link.IFF_UP
            if_flags = 0
        else:
            if_change = 0
            if_flags = 0

        debug = nlpacket.RTM_NEWLINK in self.debug

        link = nlpacket.Link(nlpacket.RTM_NEWLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('=BxxxiLL', socket.AF_UNSPEC, 0, if_flags, if_change)
        link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)
        link.add_attribute(nlpacket.Link.IFLA_MASTER, master_ifindex)
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response_with_error(link)

    def _link_add_vlan(self, ifindex, ifname, vlanid, vlan_protocol=None):
        """
        ifindex is the index of the parent interface that this sub-interface
        is being added to
        """

        '''
        If you name an interface swp2.17 but assign it to vlan 12, the kernel
        will return a very misleading NLE_MSG_OVERFLOW error.  It only does
        this check if the ifname uses dot notation.

        Do this check here so we can provide a more intuitive error
        '''
        if '.' in ifname:
            ifname_vlanid = int(ifname.split('.')[-1])

            if ifname_vlanid != vlanid:
                raise Exception("Interface %s must belong to VLAN %d (VLAN %d was requested)" % (ifname, ifname_vlanid, vlanid))

        ifla_info_data = {nlpacket.Link.IFLA_VLAN_ID: vlanid}

        if vlan_protocol:
            ifla_info_data[nlpacket.Link.IFLA_VLAN_PROTOCOL] = vlan_protocol

        return self._link_add(ifindex, ifname, 'vlan', ifla_info_data)

    def _link_add_macvlan(self, ifindex, ifname):
        """
        ifindex is the index of the parent interface that this sub-interface
        is being added to
        """
        return self._link_add(ifindex, ifname, 'macvlan', {nlpacket.Link.IFLA_MACVLAN_MODE: nlpacket.Link.MACVLAN_MODE_PRIVATE})

    def _link_set_updown(self, ifname, state):
        """
        Either bring ifname up or take it down
        """

        if state == 'up':
            if_flags = nlpacket.Link.IFF_UP
        elif state == 'down':
            if_flags = 0
        else:
            raise Exception('Unsupported state %s, valid options are "up" and "down"' % state)

        debug = nlpacket.RTM_NEWLINK in self.debug
        if_change = nlpacket.Link.IFF_UP

        link = nlpacket.Link(nlpacket.RTM_NEWLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('=BxxxiLL', socket.AF_UNSPEC, 0, if_flags, if_change)
        link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response_with_error(link)

    def _link_set_protodown(self, ifname, state):
        """
        Either bring ifname up or take it down by setting IFLA_PROTO_DOWN on or off
        """
        protodown = 1 if state == "on" else 0

        debug = nlpacket.RTM_NEWLINK in self.debug

        link = nlpacket.Link(nlpacket.RTM_NEWLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('=BxxxiLL', socket.AF_UNSPEC, 0, 0, 0)
        link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)
        link.add_attribute(nlpacket.Link.IFLA_PROTO_DOWN, protodown)
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response_with_error(link)

    def _link_add_bridge(self, ifname, ifla_info_data={}):
        return self._link_add(ifindex=None, ifname=ifname, kind='bridge', ifla_info_data=ifla_info_data)

    def _link_add_bridge_vlan(self, ifindex, vlanid_start, vlanid_end=None, pvid=False, untagged=False, master=False):
        """
        Add VLAN(s) to a bridge interface
        """
        bridge_self = False if master else True
        self.vlan_modify(nlpacket.RTM_SETLINK, ifindex, vlanid_start, vlanid_end, bridge_self, master, pvid, untagged)

    def vlan_modify(self, msgtype, ifindex, vlanid_start, vlanid_end=None, bridge_self=False, bridge_master=False, pvid=False, untagged=False):
        """
        iproute2 bridge/vlan.c vlan_modify()
        """
        assert msgtype in (nlpacket.RTM_SETLINK, nlpacket.RTM_DELLINK), "Invalid msgtype %s, must be RTM_SETLINK or RTM_DELLINK" % msgtype
        assert vlanid_start >= 1 and vlanid_start <= 4096, "Invalid VLAN start %s" % vlanid_start

        if vlanid_end is None:
            vlanid_end = vlanid_start

        assert vlanid_end >= 1 and vlanid_end <= 4096, "Invalid VLAN end %s" % vlanid_end
        assert vlanid_start <= vlanid_end, "Invalid VLAN range %s-%s, start must be <= end" % (vlanid_start, vlanid_end)

        debug = msgtype in self.debug
        bridge_flags = 0
        vlan_info_flags = 0

        link = nlpacket.Link(msgtype, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('Bxxxiii', socket.AF_BRIDGE, ifindex, 0, 0)

        if bridge_self:
            bridge_flags |= nlpacket.Link.BRIDGE_FLAGS_SELF

        if bridge_master:
            bridge_flags |= nlpacket.Link.BRIDGE_FLAGS_MASTER

        if pvid:
            vlan_info_flags |= nlpacket.Link.BRIDGE_VLAN_INFO_PVID

        if untagged:
            vlan_info_flags |= nlpacket.Link.BRIDGE_VLAN_INFO_UNTAGGED

        ifla_af_spec = OrderedDict()

        if bridge_flags:
            ifla_af_spec[nlpacket.Link.IFLA_BRIDGE_FLAGS] = bridge_flags

        # just one VLAN
        if vlanid_start == vlanid_end:
            ifla_af_spec[nlpacket.Link.IFLA_BRIDGE_VLAN_INFO] = [(vlan_info_flags, vlanid_start), ]

        # a range of VLANs
        else:
            ifla_af_spec[nlpacket.Link.IFLA_BRIDGE_VLAN_INFO] = [
                (vlan_info_flags | nlpacket.Link.BRIDGE_VLAN_INFO_RANGE_BEGIN, vlanid_start),
                (vlan_info_flags | nlpacket.Link.BRIDGE_VLAN_INFO_RANGE_END, vlanid_end)
            ]

        link.add_attribute(nlpacket.Link.IFLA_AF_SPEC, ifla_af_spec)
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response_with_error(link)

    def _link_del_bridge_vlan(self, ifindex, vlanid_start, vlanid_end=None, pvid=False, untagged=False, master=False):
        """
        Delete VLAN(s) from a bridge interface
        """
        bridge_self = False if master else True
        self.vlan_modify(nlpacket.RTM_DELLINK, ifindex, vlanid_start, vlanid_end, bridge_self, master, pvid, untagged)

    def _link_add_vxlan(self, ifname, vxlanid, dstport=None, local=None, group=None, learning=True, ageing=None, physdev=None):
        debug = nlpacket.RTM_NEWLINK in self.debug
        info_data = {nlpacket.Link.IFLA_VXLAN_ID: int(vxlanid)}

        if dstport:
            info_data[nlpacket.Link.IFLA_VXLAN_PORT] = int(dstport)
        if local:
            info_data[nlpacket.Link.IFLA_VXLAN_LOCAL] = local
        if group:
            info_data[nlpacket.Link.IFLA_VXLAN_GROUP] = group

        info_data[nlpacket.Link.IFLA_VXLAN_LEARNING] = int(learning)

        if ageing:
            info_data[nlpacket.Link.IFLA_VXLAN_AGEING] = int(ageing)

        if physdev:
            info_data[nlpacket.Link.IFLA_VXLAN_LINK] = int(physdev)

        link = nlpacket.Link(nlpacket.RTM_NEWLINK, debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_CREATE | nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack('Bxxxiii', socket.AF_UNSPEC, 0, 0, 0)
        link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)
        link.add_attribute(nlpacket.Link.IFLA_LINKINFO, {
            nlpacket.Link.IFLA_INFO_KIND: "vxlan",
            nlpacket.Link.IFLA_INFO_DATA: info_data
        })

        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response_with_error_and_wait_for_cache(ifname, link)

    def link_up_dry_run(self, ifname):
        log.info("%s: dryrun: netlink: ip link set dev %s up" % (ifname, ifname))

    def link_up(self, ifname):
        """
        Bring interface 'ifname' up (raises on error)
        :param ifname:
        :return:
        """
        log.info("%s: netlink: ip link set dev %s up" % (ifname, ifname))
        link = nlpacket.Link(nlpacket.RTM_NEWLINK, nlpacket.RTM_NEWLINK in self.debug, use_color=self.use_color)
        link.flags = nlpacket.NLM_F_REQUEST | nlpacket.NLM_F_ACK
        link.body = struct.pack("=BxxxiLL", socket.AF_UNSPEC, 0, nlpacket.Link.IFF_UP, nlpacket.Link.IFF_UP)
        link.add_attribute(nlpacket.Link.IFLA_IFNAME, ifname)
        link.build_message(self.sequence.next(), self.pid)
        try:
            result = self.tx_nlpacket_get_response_with_error(link)
            # if we reach this code it means the operation went through
            # without exception we can update the cache value this is
            # needed for the following case (and probably others):
            #
            # ifdown bond0 ; ip link set dev bond_slave down
            # ifup bond0
            #       at the beginning the slaves are admin down
            #       ifupdownmain:run_up link up the slave
            #       the bond addon check if the slave is up or down
            #           and admin down the slave before enslavement
            #           but the cache didn't process the UP notification yet
            #           so the cache has a stale value and we try to enslave
            #           a port, that is admin up, to a bond resulting
            #           in an unexpected failure
            self.cache.force_admin_status_update(ifname, nlpacket.Link.IFF_UP)
            return result
        except Exception as e:
            raise NetlinkError(e, "ip link set dev %s up" % ifname, ifname=ifname)