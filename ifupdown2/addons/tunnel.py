#!/usr/bin/python
#
# Maximilian Wilhelm <max@rfc2324.org>
#  --  Mon 10 Oct 2016 10:53:13 PM CEST
#
try:
    from ifupdown2.lib.addon import Addon
    from ifupdown2.nlmanager.nlmanager import Link

    from ifupdown2.ifupdown.iface import *

    from ifupdown2.ifupdownaddons.modulebase import moduleBase

    import ifupdown2.ifupdown.ifupdownflags as ifupdownflags
except ImportError:
    from lib.addon import Addon
    from nlmanager.nlmanager import Link

    from ifupdown.iface import *

    from ifupdownaddons.modulebase import moduleBase

    import ifupdown.ifupdownflags as ifupdownflags


#
# TODO: Add checks for ipip tunnels.
#
class tunnel(Addon, moduleBase):
    """
    ifupdown2 addon module to configure tunnels
    """
    _modinfo = {
        'mhelp': 'create/configure GRE/IPIP/SIT and GRETAP tunnel interfaces',
        'attrs': {
            'tunnel-mode': {
                'help': 'type of tunnel as in \'ip link\' command.',
                'validvals': ['gre', 'gretap', 'ipip', 'sit', 'vti', 'ip6gre', 'ipip6', 'ip6ip6', 'vti6'],
                'required': True,
                'example': ['mode gre']
            },
            'tunnel-local': {
                'help': 'IP of local tunnel endpoint',
                'validvals': ['<ipv4>', '<ipv6>'],
                'required': True,
                'example': ['local 192.2.0.42']
            },
            'tunnel-endpoint': {
                'help': 'IP of remote tunnel endpoint',
                'validvals': ['<ipv4>', '<ipv6>'],
                'required': True,
                'example': ['endpoint 192.2.0.23']
            },
            'tunnel-ttl': {
                'help': 'TTL for tunnel packets',
                'validvals': ['<number>'],
                'required': False,
                'example': ['ttl 64']
            },
            'tunnel-dev': {
                'help': 'Physical underlay device to use for tunnel packets',
                'validvals': ['<interface>'],
                'required': False,
                'example': ['tunnel-physdev eth1']
            },
        }
    }

    def __init__(self, *args, **kargs):
        moduleBase.__init__(self, *args, **kargs)
        Addon.__init__(self)

    @staticmethod
    def _is_my_interface(ifaceobj):
        return ifaceobj.get_attr_value_first("tunnel-mode")

    @staticmethod
    def _has_config_changed(attrs_present, attrs_configured):
        for key, value in attrs_configured.iteritems():
            if attrs_present.get(key) != value:
                return True
        return False

    def __get_info_data_gre_tunnel(self, info_data):
        tunnel_link_ifindex = info_data.get(Link.IFLA_GRE_LINK)

        return {
            "tunnel-endpoint": str(info_data.get(Link.IFLA_GRE_REMOTE)),
            "tunnel-local": str(info_data.get(Link.IFLA_GRE_LOCAL)),
            "tunnel-ttl": str(info_data.get(Link.IFLA_GRE_TTL)),
            "tunnel-dev": self.cache.get_ifname(tunnel_link_ifindex) if tunnel_link_ifindex else ""
        }

    def __get_info_data_iptun_tunnel(self, info_data):
        tunnel_link_ifindex = info_data.get(Link.IFLA_IPTUN_LINK)

        return {
            "tunnel-endpoint": str(info_data.get(Link.IFLA_IPTUN_REMOTE)),
            "tunnel-local": str(info_data.get(Link.IFLA_IPTUN_LOCAL)),
            "tunnel-ttl": str(info_data.get(Link.IFLA_IPTUN_TTL)),
            "tunnel-dev": self.cache.get_ifname(tunnel_link_ifindex) if tunnel_link_ifindex else ""
        }

    def __get_info_data_vti_tunnel(self, info_data):
        tunnel_link_ifindex = info_data.get(Link.IFLA_VTI_LINK)

        return {
            "tunnel-endpoint": str(info_data.get(Link.IFLA_VTI_REMOTE)),
            "tunnel-local": str(info_data.get(Link.IFLA_VTI_LOCAL)),
            "tunnel-dev": self.cache.get_ifname(tunnel_link_ifindex) if tunnel_link_ifindex else ""
        }

    def get_linkinfo_attrs(self, ifname, link_kind):
        return {
            "gre": self.__get_info_data_gre_tunnel,
            "gretap": self.__get_info_data_gre_tunnel,
            "ip6gre": self.__get_info_data_gre_tunnel,
            "ip6gretap": self.__get_info_data_gre_tunnel,
            "ip6erspan": self.__get_info_data_gre_tunnel,
            "ipip": self.__get_info_data_iptun_tunnel,
            "sit": self.__get_info_data_iptun_tunnel,
            "ip6tnl": self.__get_info_data_iptun_tunnel,
            "vti": self.__get_info_data_vti_tunnel,
            "vti6": self.__get_info_data_vti_tunnel,
        }.get(link_kind, lambda x: {})(self.cache.get_link_info_data(ifname))

    def _up(self, ifaceobj):
        attr_map = {
            # attr_name -> ip route param name
            'tunnel-local': 'local',
            'tunnel-endpoint': 'remote',
            'tunnel-ttl': 'ttl',
            'tunnel-dev': 'dev',
        }

        mode = ifaceobj.get_attr_value_first('tunnel-mode')
        attrs = {}
        attrs_mapped = {}

        # Only include attributes which have been set and map ifupdown2 names
        # to attribute names expected by iproute
        for attr, iproute_attr in attr_map.items():
            attr_val = ifaceobj.get_attr_value_first(attr)
            if attr_val is not None:
                attrs_mapped[iproute_attr] = attr_val
                attrs[attr] = attr_val

        # Create the tunnel if it doesn't exist yet...
        if not self.cache.link_exists(ifaceobj.name):
            self.iproute2.tunnel_create(ifaceobj.name, mode, attrs_mapped)
            return

        # If it's present, check if there were changes
        current_mode = self.cache.get_link_kind(ifaceobj.name)
        current_attrs = self.get_linkinfo_attrs(ifaceobj.name, current_mode)

        try:
            if current_attrs and current_mode != mode or self._has_config_changed(current_attrs, attrs):
                # Mode and some other changes are not possible without recreating the interface,
                # so just recreate it IFF there have been changes.
                self.netlink.link_del(ifaceobj.name)
                self.iproute2.tunnel_create(ifaceobj.name, mode, attrs_mapped)
        except Exception, e:
            self.log_warn(str(e))

    def _down(self, ifaceobj):
        if not ifupdownflags.flags.PERFMODE and not self.cache.link_exists(ifaceobj.name):
            return
        try:
            self.netlink.link_del(ifaceobj.name)
        except Exception, e:
            self.log_warn(str(e))

    def get_dependent_ifacenames(self, ifaceobj, ifacenames_all=None):
        if not self._is_my_interface(ifaceobj):
            return None

        device = ifaceobj.get_attr_value_first('tunnel-dev')
        if device:
            return [device]

        return None

    @staticmethod
    def _query_check_n_update(ifaceobj, ifaceobjcurr, attrname, attrval, running_attrval):
        if not ifaceobj.get_attr_value_first(attrname):
            return
        if running_attrval and attrval == running_attrval:
            ifaceobjcurr.update_config_with_status(attrname, attrval, 0)
        else:
            ifaceobjcurr.update_config_with_status(attrname, running_attrval, 1)

    def _query_check(self, ifaceobj, ifaceobjcurr):
        ifname = ifaceobj.name

        if not self.cache.link_exists(ifname):
            return

        link_kind = self.cache.get_link_kind(ifname)
        tunattrs = self.get_linkinfo_attrs(ifaceobj.name, link_kind)

        if not tunattrs:
            ifaceobjcurr.check_n_update_config_with_status_many(ifaceobj, self.get_mod_attrs(), -1)
            return

        tunattrs["tunnel-mode"] = link_kind

        user_config_mode = ifaceobj.get_attr_value_first("tunnel-mode")
        if user_config_mode in ('ipip6', 'ip6ip6'):
            ifaceobj.replace_config("tunnel-mode", "ip6tnl")

        for attr in self.get_mod_attrs():
            if not ifaceobj.get_attr_value_first(attr):
                continue

            # Validate all interface attributes set in the config.
            # Remote any leading 'tunnel-' prefix in front of the attr name
            # when accessing tunattrs parsed from 'ip -d link'.
            self._query_check_n_update(ifaceobj, ifaceobjcurr, attr,
                                       ifaceobj.get_attr_value_first(attr),
                                       tunattrs.get(attr))

    # Operations supported by this addon (yet).
    _run_ops = {
        'pre-up': _up,
        'post-down': _down,
        'query-checkcurr': _query_check
    }

    def get_ops(self):
        return self._run_ops.keys()

    def run(self, ifaceobj, operation, query_ifaceobj=None, **extra_args):
        op_handler = self._run_ops.get(operation)
        if not op_handler:
            return

        if operation != 'query-running' and not self._is_my_interface(ifaceobj):
            return

        if operation == 'query-checkcurr':
            op_handler(self, ifaceobj, query_ifaceobj)
        else:
            op_handler(self, ifaceobj)
