# -*- coding: utf-8 -*-
#
# Copyright (c) 2015 Metaswitch Networks
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

# Etcd-based transport for the Calico/OpenStack Plugin.

# Standard Python library imports.
import etcd
import eventlet
import eventlet.event
import json
import re
import weakref

# OpenStack imports.
from oslo.config import cfg
from neutron.openstack.common import log

# Calico imports.
from calico.datamodel_v1 import (READY_KEY, CONFIG_DIR, TAGS_KEY_RE, HOST_DIR,
                                 key_for_endpoint, PROFILE_DIR,
                                 key_for_profile, key_for_profile_rules,
                                 key_for_profile_tags, key_for_config)

# Register Calico-specific options.
calico_opts = [
    cfg.StrOpt('etcd_host', default='localhost',
               help="The hostname or IP of the etcd node/proxy"),
    cfg.IntOpt('etcd_port', default=4001,
               help="The port to use for the etcd node/proxy"),
]
cfg.CONF.register_opts(calico_opts, 'calico')

OPENSTACK_ENDPOINT_RE = re.compile(
    r'^' + HOST_DIR +
    r'/(?P<hostname>[^/]+)/.*openstack.*/endpoint/(?P<endpoint_id>[^/]+)')

json_decoder = json.JSONDecoder()

PERIODIC_RESYNC_INTERVAL_SECS = 30

LOG = log.getLogger(__name__)


class CalicoTransportEtcd(object):
    """Calico transport implementation based on etcd."""

    def __init__(self, driver):
        # Explicitly store the driver as a weakreference. This prevents
        # the reference loop between transport and driver keeping the objects
        # alive.
        self.driver = weakref.proxy(driver)

        # Prepare client for accessing etcd data.
        self.client = etcd.Client(host=cfg.CONF.calico.etcd_host,
                                  port=cfg.CONF.calico.etcd_port)

        # Spawn a green thread for periodically resynchronizing etcd against
        # the OpenStack database.
        eventlet.spawn(self.periodic_resync_thread)

    def periodic_resync_thread(self):
        while True:
            LOG.info("Calico plugin doing periodic resync.")
            try:
                # Write non-default config that Felices need.
                self.provide_felix_config()

                # Resynchronize endpoint data.
                self.resync_endpoints()

                # Resynchronize security group data.
                self.resync_security_groups()

            except:
                LOG.exception("Exception in periodic resync thread")

            # Sleep until time for next resync.
            LOG.info("Calico plugin finished periodic resync.  "
                     "Next resync in %s seconds.",
                     PERIODIC_RESYNC_INTERVAL_SECS)
            eventlet.sleep(PERIODIC_RESYNC_INTERVAL_SECS)

    def resync_endpoints(self):
        pass

    def resync_security_groups(self):
        pass

    def port_etcd_key(self, port):
        return key_for_endpoint(port['binding:host_id'],
                                "openstack",
                                port['device_id'],
                                port['id'])

    def port_etcd_data(self, port):
        # Construct the simpler port data.
        data = {'state': 'active' if port['admin_state_up'] else 'inactive',
                'name': port['interface_name'],
                'mac': port['mac_address'],
                'profile_id': self.port_profile_id(port)}

        # Collect IPv6 and IPv6 addresses.  On the way, also set the
        # corresponding gateway fields.  If there is more than one IPv4 or IPv6
        # gateway, the last one (in port['fixed_ips']) wins.
        ipv4_nets = []
        ipv6_nets = []
        for ip in port['fixed_ips']:
            if ':' in ip['ip_address']:
                ipv6_nets.append(ip['ip_address'] + '/128')
                if ip['gateway'] is not None:
                    data['ipv6_gateway'] = ip['gateway']
            else:
                ipv4_nets.append(ip['ip_address'] + '/32')
                if ip['gateway'] is not None:
                    data['ipv4_gateway'] = ip['gateway']
        data['ipv4_nets'] = ipv4_nets
        data['ipv6_nets'] = ipv6_nets

        # Return that data.
        return data

    def port_profile_id(self, port):
        return '_'.join(port['security_groups'])

    def write_profile_to_etcd(self, profile_id):
        self.client.write(key_for_profile_rules(profile_id),
                          json.dumps(self.profile_rules(profile_id)))
        self.client.write(key_for_profile_tags(profile_id),
                          json.dumps(self.profile_tags(profile_id)))

    def profile_rules(self, profile_id):
        inbound = []
        outbound = []
        for sgid in self.profile_tags(profile_id):
            # Be tolerant of a security group not being here. Allow up to 20
            # attempts to get it, waiting a few hundred ms in between: we might
            # just be racing slightly ahead of a security group update.
            rules = None
            retries = 20
            while rules is None:
                try:
                    with self._sgs_semaphore:
                        rules = self.sgs[sgid]['security_group_rules']
                except KeyError:
                    LOG.warning("Missing info for SG %s: waiting.", sgid)
                    retries -= 1

                    if not retries:
                        LOG.error("Gave up waiting for SG %s", sgid)
                        raise

                    # Wait for 200ms
                    eventlet.sleep(0.2)


            for rule in rules:
                LOG.info("Neutron rule  %s : %s", profile_id, rule)
                etcd_rule = _neutron_rule_to_etcd_rule(rule)
                if rule['direction'] == 'ingress':
                    inbound.append(etcd_rule)
                else:
                    outbound.append(etcd_rule)

        return {'inbound_rules': inbound, 'outbound_rules': outbound}

    def profile_tags(self, profile_id):
        return profile_id.split('_')

    def endpoint_created(self, port, profile):
        """
        Write appropriate data to etcd for an endpoint creation event.
        """
        # First, write etcd data for the new endpoint.
        # TODO: Write this function.
        self.write_port_to_etcd(port)

        # Next, write the security profile.
        # TODO: Fix this function to do the right thing.
        self.write_profile_to_etcd(profile)

    def endpoint_updated(self, port, profile):
        """
        Write data to etcd for an endpoint updated event.
        """
        # Do the same as for endpoint_created.
        self.endpoint_created(port, profile)

    def endpoint_deleted(self, port):
        """
        Delete data from etcd for an endpoint deleted event.
        """
        # TODO: What do we do about profiles here?
        # Delete the etcd key for this endpoint.
        key = self.port_etcd_key(port)
        try:
            self.client.delete(key)
        except etcd.EtcdKeyNotFound:
            # Already gone, treat as success.
            LOG.debug("Key %s, which we were deleting, disappeared", key)

    def security_group_updated(self, sg):
        # Update the data that we're keeping for this security group.
        with self._sgs_semaphore:
            self.sgs[sg['id']] = sg

        # Identify all the needed profiles that incorporate this security
        # group, and rewrite their data.
        # Take the profile lock so that no-one can modify needed_profiles
        # while we iterate over it.  (This is probably unneeded since there
        # aren't any yield points in the loop but better safe than sorry.)
        profiles_to_rewrite = set()
        with self.profile_semaphore:
            for profile_id in self.needed_profiles:
                if sg['id'] in self.profile_tags(profile_id):
                    # Write etcd data for this profile.
                    profiles_to_rewrite.add(profile_id)
        for profile_id in profiles_to_rewrite:
            self.write_profile_to_etcd(profile_id)

    def provide_felix_config(self):
        """Specify the prefix of the TAP interfaces that Felix should
        look for and work with.  This config setting does not have a
        default value, because different cloud systems will do
        different things.  Here we provide the prefix that Neutron
        uses.
        """
        # First read the config values, so as to avoid unnecessary
        # writes.
        prefix = None
        ready = None
        iface_pfx_key = key_for_config('InterfacePrefix')
        try:
            prefix = self.client.read(iface_pfx_key).value
            ready = self.client.read(READY_KEY).value
        except etcd.EtcdKeyNotFound:
            LOG.info('%s values are missing', CONFIG_DIR)

        # Now write the values that need writing.
        if prefix != 'tap':
            LOG.info('%s -> tap', iface_pfx_key)
            self.client.write(iface_pfx_key, 'tap')
        if ready != 'true':
            # TODO Set this flag only once we're really ready!
            LOG.info('%s -> true', READY_KEY)
            self.client.write(READY_KEY, 'true')

def _neutron_rule_to_etcd_rule(rule):
    """
    Translate a single Neutron rule dict to a single dict in our
    etcd format.
    """
    ethertype = rule['ethertype']
    etcd_rule = {}
    # Map the ethertype field from Neutron to etcd format.
    etcd_rule['ip_version'] = {'IPv4': 4,
                               'IPv6': 6}[ethertype]
    # Map the protocol field from Neutron to etcd format.
    if rule['protocol'] is None or rule['protocol'] == -1:
        pass
    elif rule['protocol'] == 'icmp':
        etcd_rule['protocol'] = {'IPv4': 'icmp',
                                 'IPv6': 'icmpv6'}[ethertype]
    else:
        etcd_rule['protocol'] = rule['protocol']

    # OpenStack (sometimes) represents 'any IP address' by setting
    # both 'remote_group_id' and 'remote_ip_prefix' to None.  We
    # translate that to an explicit 0.0.0.0/0 (for IPv4) or ::/0
    # (for IPv6).
    net = rule['remote_ip_prefix']
    if not (net or rule['remote_group_id']):
        net = {'IPv4': '0.0.0.0/0',
               'IPv6': '::/0'}[ethertype]
    port_spec = None
    if rule['protocol'] == 'icmp':
        # OpenStack stashes the ICMP match criteria in
        # port_range_min/max.
        icmp_type = rule['port_range_min']
        if icmp_type is not None and icmp_type != -1:
            etcd_rule['icmp_type'] = icmp_type
        icmp_code = rule['port_range_max']
        if icmp_code is not None and icmp_code != -1:
            etcd_rule['icmp_code'] = icmp_code
    else:
        # src/dst_ports is a list in which each entry can be a
        # single number, or a string describing a port range.
        if rule['port_range_min'] == -1:
            port_spec = ['1:65535']
        elif rule['port_range_min'] == rule['port_range_max']:
            if rule['port_range_min'] is not None:
                port_spec = [rule['port_range_min']]
        else:
            port_spec = ['%s:%s' % (rule['port_range_min'],
                                    rule['port_range_max'])]

    # Put it all together and add to either the inbound or the
    # outbound list.
    if rule['direction'] == 'ingress':
        if rule['remote_group_id'] is not None:
            etcd_rule['src_tag'] = rule['remote_group_id']
        if net is not None:
            etcd_rule['src_net'] = net
        if port_spec is not None:
            etcd_rule['dst_ports'] = port_spec
        LOG.info("=> Inbound Calico rule %s" % etcd_rule)
    else:
        if rule['remote_group_id'] is not None:
            etcd_rule['dst_tag'] = rule['remote_group_id']
        if net is not None:
            etcd_rule['dst_net'] = net
        if port_spec is not None:
            etcd_rule['dst_ports'] = port_spec
        LOG.info("=> Outbound Calico rule %s" % etcd_rule)

    return etcd_rule
