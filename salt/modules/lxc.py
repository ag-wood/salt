# -*- coding: utf-8 -*-
'''
Control Linux Containers via Salt

:depends: lxc package for distribution

lxc >= 1.0 (even beta alpha) is required

'''

# Import python libs
from __future__ import absolute_import, print_function
import datetime
import copy
import difflib
import logging
import tempfile
import os
import time
import shutil
import re
import random

# Import salt libs
import salt
import salt.utils.odict
import salt.utils
import salt.utils.dictupdate
import salt.utils.network
from salt.exceptions import CommandExecutionError, SaltInvocationError
import salt.utils.cloud
import salt.config

# Import 3rd-party libs
import salt.ext.six as six
# pylint: disable=import-error,no-name-in-module
from salt.ext.six.moves import range  # pylint: disable=redefined-builtin
from salt.ext.six.moves.urllib.parse import urlparse as _urlparse
# pylint: enable=import-error,no-name-in-module

# Set up logging
log = logging.getLogger(__name__)

# Don't shadow built-in's.
__func_alias__ = {
    'list_': 'list',
    'ls_': 'ls'
}

__virtualname__ = 'lxc'
DEFAULT_NIC = 'eth0'
SEED_MARKER = '/lxc.initial_seed'
EXEC_DRIVER = 'lxc-attach'
_marker = object()


def __virtual__():
    if salt.utils.which('lxc-start'):
        return __virtualname__
    # To speed up the whole thing, we decided to not use the
    # subshell way and assume things are in place for lxc
    # Discussion made by @kiorky and @thatch45

    # lxc-version presence is not sufficient, in lxc1.0 alpha
    # (precise backports), we have it and it is sufficient
    # for the module to execute.
    # elif salt.utils.which('lxc-version'):
    #     passed = False
    #     try:
    #         passed = subprocess.check_output(
    #             'lxc-version').split(':')[1].strip() >= '1.0'
    #     except Exception:
    #         pass
    #     if not passed:
    #         log.warning('Support for lxc < 1.0 may be incomplete.')
    #     return 'lxc'
    # return False
    #
    return False


def _clear_context():
    '''
    Clear any lxc variables set in __context__
    '''
    for var in [x for x in __context__ if x.startswith('lxc.')]:
        log.trace('Clearing __context__[\'{0}\']'.format(var))
        __context__.pop(var, None)


def _ip_sort(ip):
    '''Ip sorting'''
    idx = '001'
    if ip == '127.0.0.1':
        idx = '200'
    if ip == '::1':
        idx = '201'
    elif '::' in ip:
        idx = '100'
    return '{0}___{1}'.format(idx, ip)


def cloud_init_interface(name, vm_=None, **kwargs):
    '''
    Interface between salt.cloud.lxc driver and lxc.init
    ``vm_`` is a mapping of vm opts in the salt.cloud format
    as documented for the lxc driver.

    This can be used either:

    - from the salt cloud driver
    - because you find the argument to give easier here
      than using directly lxc.init

    .. warning::
        BE REALLY CAREFUL CHANGING DEFAULTS !!!
        IT'S A RETRO COMPATIBLE INTERFACE WITH
        THE SALT CLOUD DRIVER (ask kiorky).

    name
        name of the lxc container to create
    from_container
        which container we use as a template
        when running lxc.clone
    image
        which template do we use when we
        are using lxc.create. This is the default
        mode unless you specify something in from_container
    backing
        which backing store to use.
        Values can be: overlayfs, dir(default), lvm, zfs, brtfs
    fstype
        When using a blockdevice level backing store,
        which filesystem to use on
    size
        When using a blockdevice level backing store,
        which size for the filesystem to use on
    snapshot
        Use snapshot when cloning the container source
    vgname
        if using LVM: vgname
    lgname
        if using LVM: lvname
    pub_key
        public key to preseed the minion with.
        Can be the keycontent or a filepath
    priv_key
        private key to preseed the minion with.
        Can be the keycontent or a filepath
    ip
        ip for the primary nic
    mac
        mac address for the primary nic
    netmask
        netmask for the primary nic (24)
        = ``vm_.get('netmask', '24')``
    bridge
        bridge for the primary nic (lxcbr0)
    gateway
        network gateway for the container
    additional_ips
        additional ips which will be wired on the main bridge (br0)
        which is connected to internet.
        Be aware that you may use manual virtual mac addresses
        providen by you provider (online, ovh, etc).
        This is a list of mappings {ip: '', mac: '', netmask:''}
        Set gateway to None and an interface with a gateway
        to escape from another interface that eth0.
        eg::

              - {'mac': '00:16:3e:01:29:40',
                 'gateway': None, (default)
                 'link': 'br0', (default)
                 'netmask': '', (default)
                 'ip': '22.1.4.25'}
    unconditional_install
        given to lxc.bootstrap (see relative doc)
    force_install
        given to lxc.bootstrap (see relative doc)
    config
        any extra argument for the salt minion config
    dnsservers
        dns servers to set inside the container
    autostart
        autostart the container at boot time
    password
        administrative password for the container
    users
        administrative users for the container
        default: [root] and [root, ubuntu] on ubuntu

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.cloud_init_interface foo

    '''
    if vm_ is None:
        vm_ = {}
    vm_ = copy.deepcopy(vm_)
    vm_ = salt.utils.dictupdate.update(vm_, kwargs)
    if 'profile' in vm_:
        profile_data = copy.deepcopy(vm_['profile'])
        if isinstance(profile_data, dict):
            profile_name = profile_data.pop('name', name)
        else:
            # assuming profile is a string (profile name)
            profile_name = profile_data
            profile_data = {}
        profile = get_container_profile(profile_name, **profile_data)
    else:
        profile = {}
    if name is None:
        name = vm_['name']
    # if we are on ubuntu, default to ubuntu
    default_template = ''
    if __grains__.get('os', '') in ['Ubuntu']:
        default_template = 'ubuntu'
    image = vm_.get('image', profile.get('template',
                                         default_template))
    vgname = vm_.get('vgname', None)
    backing = vm_.get('backing', 'dir')
    snapshot = vm_.get('snapshot', False)
    autostart = bool(vm_.get('autostart', True))
    dnsservers = vm_.get('dnsservers', [])
    if not dnsservers:
        dnsservers = ['8.8.8.8', '4.4.4.4']
    password = vm_.get('password', 's3cr3t')
    password_encrypted = vm_.get('password_encrypted', False)
    fstype = vm_.get('fstype', None)
    lvname = vm_.get('lvname', None)
    pub_key = vm_.get('pub_key', None)
    priv_key = vm_.get('priv_key', None)
    size = vm_.get('size', '20G')
    script = vm_.get('script', None)
    script_args = vm_.get('script_args', None)
    if image:
        profile['template'] = image
    if vgname:
        profile['vgname'] = vgname
    if backing:
        profile['backing'] = backing
    users = vm_.get('users', None)
    if users is None:
        users = []
    ssh_username = vm_.get('ssh_username', None)
    if ssh_username and (ssh_username not in users):
        users.append(ssh_username)
    ip = vm_.get('ip', None)
    mac = vm_.get('mac', None)
    netmask = vm_.get('netmask', '24')
    bridge = vm_.get('bridge', 'lxcbr0')
    gateway = vm_.get('gateway', 'auto')
    unconditional_install = vm_.get('unconditional_install', False)
    force_install = vm_.get('force_install', True)
    config = vm_.get('config', {})
    if not config:
        config = vm_.get('minion', {})
    if not config:
        config = {}
    config.setdefault('master',
                      vm_.get('master',
                              __opts__.get('master',
                                           __opts__['id'])))
    config.setdefault(
        'master_port',
        vm_.get('master_port',
                __opts__.get('master_port',
                             __opts__.get('ret_port',
                                          __opts__.get('4506')))))
    if not config['master']:
        config = {}
    eth0 = {}
    nic_opts = {'eth0': eth0}
    bridge = vm_.get('bridge', 'lxcbr0')
    if ip is None:
        nic_opts = None
    else:
        fullip = ip
        if netmask:
            fullip += '/{0}'.format(netmask)
        eth0['ipv4'] = fullip
        if mac is not None:
            eth0['mac'] = mac
        if bridge:
            eth0['link'] = bridge
    for ix, iopts in enumerate(vm_.get("additional_ips", [])):
        ifh = "eth{0}".format(ix+1)
        ethx = nic_opts.setdefault(ifh, {})
        gw = iopts.get('gateway', None)
        if gw:
            ethx['gateway'] = gw
            vm_['gateway'] = gateway = None
        ethx['link'] = iopts.get('link', 'br0')
        ethx['ipv4'] = iopts['ip']
        nm = iopts.get('netmask', '')
        if nm:
            ethx['ipv4'] += '/{0}'.format(nm)
        for i in ('mac', 'hwaddr'):
            if i in iopts:
                ethx['mac'] = iopts[i]
                break
        if 'mac' not in ethx:
            raise ValueError('No mac for {0}'.format(ifh))
    gateway = vm_.get('gateway', 'auto')
    #
    lxc_init_interface = {}
    lxc_init_interface['name'] = name
    lxc_init_interface['config'] = config
    lxc_init_interface['memory'] = vm_.get('memory', 0)  # nolimit
    lxc_init_interface['pub_key'] = pub_key
    lxc_init_interface['priv_key'] = priv_key
    lxc_init_interface['bridge'] = bridge
    lxc_init_interface['gateway'] = gateway
    lxc_init_interface['nic_opts'] = nic_opts
    for clone_from in ['clone_from', 'clone', 'from_container']:
        # clone_from should default to None if not available
        lxc_init_interface['clone_from'] = vm_.get(clone_from, None)
        if lxc_init_interface['clone_from'] is not None:
            break
    lxc_init_interface['profile'] = profile
    lxc_init_interface['snapshot'] = snapshot
    lxc_init_interface['dnsservers'] = dnsservers
    lxc_init_interface['fstype'] = fstype
    lxc_init_interface['vgname'] = vgname
    lxc_init_interface['size'] = size
    lxc_init_interface['lvname'] = lvname
    lxc_init_interface['force_install'] = force_install
    lxc_init_interface['unconditional_install'] = (
        unconditional_install
    )
    lxc_init_interface['bootstrap_url'] = script
    lxc_init_interface['bootstrap_args'] = script_args
    lxc_init_interface['bootstrap_shell'] = vm_.get('bootstrap_shell', 'sh')
    lxc_init_interface['autostart'] = autostart
    lxc_init_interface['users'] = users
    lxc_init_interface['password'] = password
    lxc_init_interface['password_encrypted'] = password_encrypted
    lxc_init_interface['network_profile'] = DEFAULT_NIC
    for i in ['cpu', 'cpuset', 'cpushare']:
        if vm_.get(i, None):
            lxc_init_interface[i] = vm_[i]
    return lxc_init_interface


def get_container_profile(name=None, **kwargs):
    '''
    .. versionadded:: 2015.2.0

    Gather a pre-configured set of container configuration parameters. If no
    arguments are passed, an empty profile is returned.

    Profiles can be defined in the minion or master config files, or in pillar
    or grains, and are loaded using :mod:`config.get
    <salt.modules.config.get>`. The key under which LXC profiles must be
    configured is ``lxc.container_profile.profile_name``. An example container
    profile would be as follows:

    .. code-block:: yaml

        lxc.container_profile:
          ubuntu:
            template: ubuntu
            backing: lvm
            vgname: lxc
            size: 1G

    Parameters set in a profile can be overridden by passing additional
    container creation arguments (such as the ones passed to :mod:`lxc.create
    <salt.modules.lxc.create>`) to this function.

    A profile can be defined either as the name of the profile, or a dictionary
    of variable names and values. See the :ref:`LXC Tutorial
    <tutorial-lxc-profiles>` for more information on how to use LXC profiles.

    CLI Example::

    .. code-block:: bash

        salt-call lxc.get_container_profile centos
        salt-call lxc.get_container_profile ubuntu template=ubuntu backing=overlayfs
    '''
    if isinstance(name, dict):
        profilename = name.pop('name', None)
        return get_container_profile(profilename, **name)

    if name is None:
        profile_match = {}
    else:
        profile_match = \
            __salt__['config.get'](
                'lxc.container_profile:{0}'.format(name),
                default=None,
                merge='recurse'
            )
        if profile_match is None:
            # Try legacy profile location
            profile_match = \
                __salt__['config.get']('lxc.profile:{0}'.format(name), None)
            if profile_match is not None:
                salt.utils.warn_until(
                    'Boron',
                    'lxc.profile has been deprecated, please configure LXC '
                    'container profiles under lxc.container_profile instead'
                )
            else:
                # No matching profile, make the profile an empty dict so that
                # overrides can be applied below.
                profile_match = {}

    if not isinstance(profile_match, dict):
        raise CommandExecutionError('Container profile must be a dictionary')

    # Overlay the kwargs to override matched profile data
    overrides = copy.deepcopy(kwargs)
    for key in overrides.keys():
        if key.startswith('__'):
            # Remove pub data from kwargs
            overrides.pop(key)
    profile_match = salt.utils.dictupdate.update(
        copy.deepcopy(profile_match),
        overrides
    )
    return profile_match


def get_network_profile(name=None):
    '''
    .. versionadded:: 2015.2.0

    Gather a pre-configured set of network configuration parameters. If no
    arguments are passed, the following default profile is returned:

    .. code-block:: python

        {'eth0': {'link': 'br0', 'type': 'veth', 'flags': 'up'}}

    Profiles can be defined in the minion or master config files, or in pillar
    or grains, and are loaded using :mod:`config.get
    <salt.modules.config.get>`. The key under which LXC profiles must be
    configured is ``lxc.network_profile``. An example network profile would be
    as follows:

    .. code-block:: yaml

        lxc.network_profile.centos:
          eth0:
            link: br0
            type: veth
            flags: up

    Parameters set in a profile can be overridden by passing additional
    arguments to this function.

    A profile can be passed either as the name of the profile, or a
    dictionary of variable names and values. See the :ref:`LXC Tutorial
    <tutorial-lxc-profiles>` for more information on how to use network
    profiles.

    .. warning::

        The ``ipv4``, ``ipv6``, ``gateway``, and ``link`` (bridge) settings in
        network profiles will only work if the container doesnt redefine the
        network configuration (for example in
        ``/etc/sysconfig/network-scripts/ifcfg-<interface_name>`` on
        RHEL/CentOS, or ``/etc/network/interfaces`` on Debian/Ubuntu/etc.)

    CLI Example::

    .. code-block:: bash

        salt-call lxc.get_network_profile default
    '''
    if name is None:
        net_profile = None
    else:
        net_profile = \
            __salt__['config.get'](
                'lxc.network_profile:{0}'.format(name),
                default=None,
                merge='recurse'
            )
        if net_profile is None:
            # Try legacy profile location
            net_profile = \
                __salt__['config.get']('lxc.nic:{0}'.format(name), None)
            if net_profile is not None:
                salt.utils.warn_until(
                    'Boron',
                    'lxc.nic has been deprecated, please configure LXC '
                    'network profiles under lxc.network_profile instead'
                )
    return net_profile if net_profile is not None else {}


def _rand_cpu_str(cpu):
    '''
    Return a random subset of cpus for the cpuset config
    '''
    cpu = int(cpu)
    avail = __salt__['status.nproc']()
    if cpu < avail:
        return '0-{0}'.format(avail)
    to_set = set()
    while len(to_set) < cpu:
        choice = random.randint(0, avail - 1)
        if choice not in to_set:
            to_set.add(str(choice))
    return ','.join(sorted(to_set))


def _network_conf(conf_tuples=None, **kwargs):
    '''
    Network configuration defaults

        network_profile
            as for containers, we can either call this function
            either with a network_profile dict or network profile name
            in the kwargs
        nic_opts
            overrides or extra nics in the form {nic_name: {set: tings}

    '''
    nic = kwargs.pop('network_profile', None)
    ret = []
    nic_opts = kwargs.pop('nic_opts', {})
    if nic_opts is None:
        # coming from elsewhere
        nic_opts = {}
    if not conf_tuples:
        conf_tuples = []
    old = _get_veths(conf_tuples)
    if not nic and not nic_opts and not old:
        return ret
    kwargs = copy.deepcopy(kwargs)
    gateway = kwargs.pop('gateway', None)
    bridge = kwargs.get('bridge', None)

    # if we have a profile name, get the profile and load the network settings
    # this will obviously by default  look for a profile called "eth0"
    # or by what is defined in nic_opts
    # and complete each nic settings by sane defaults
    if isinstance(nic, six.string_types):
        nicp = get_network_profile(nic)
        if nic_opts:
            for dev, args in six.iteritems(nic_opts):
                ethx = nicp.setdefault(dev, {})
                try:
                    ethx = salt.utils.dictupdate.update(ethx, args)
                except AttributeError:
                    raise SaltInvocationError('Invalid nic_opts configuration')
        ifs = [a for a in nicp]
        ifs += [a for a in old if a not in nicp]
        ifs.sort()
        gateway_set = False
        for dev in ifs:
            args = nicp.get(dev, {})
            opts = nic_opts.get(dev, {}) if nic_opts else {}
            old_if = old.get(dev, {})
            flags = opts.get('flags', '')
            mac = opts.get('mac', '')
            type_ = opts.get('type', args.get('type', ''))
            link = opts.get('link', args.get('link', ''))
            ipv4 = opts.get('ipv4')
            ipv6 = opts.get('ipv6')
            infos = salt.utils.odict.OrderedDict([
                ('lxc.network.type', {
                    'test': not type_,
                    'value': type_,
                    'old': old_if.get('lxc.network.type'),
                    'default': 'veth'}),
                ('lxc.network.name', {
                    'test': False,
                    'value': dev,
                    'old': dev,
                    'default': dev}),
                ('lxc.network.flags', {
                    'test': not flags,
                    'value': flags,
                    'old': old_if.get('lxc.network.flags'),
                    'default': 'up'}),
                ('lxc.network.link', {
                    'test': not link,
                    'value': link,
                    'old': old_if.get('lxc.network.link'),
                    'default': 'br0'}),
                ('lxc.network.hwaddr', {
                    'test': not mac,
                    'value': mac,
                    'old': old_if.get('lxc.network.hwaddr'),
                    'default': salt.utils.gen_mac()}),
                ('lxc.network.ipv4', {
                    'test': not ipv4,
                    'value': ipv4,
                    'old': old_if.get('lxc.network.ipv4', ''),
                    'default': None}),
                ('lxc.network.ipv6', {
                    'test': not ipv6,
                    'value': ipv6,
                    'old': old_if.get('lxc.network.ipv6', ''),
                    'default': None})])
            # for each parameter, if not explicitly set, the
            # config value present in the LXC configuration should
            # take precedence over the profile configuration
            for info in list(infos.keys()):
                bundle = infos[info]
                if bundle['test']:
                    if bundle['old']:
                        bundle['value'] = bundle['old']
                    elif bundle['default']:
                        bundle['value'] = bundle['default']
            for info, data in infos.items():
                if data['value']:
                    ret.append({info: data['value']})
            for key, val in six.iteritems(args):
                if key == 'link' and bridge:
                    val = bridge
                val = opts.get(key, val)
                if key in [
                    'type', 'flags', 'name',
                    'gateway', 'mac', 'link', 'ipv4', 'ipv6'
                ]:
                    continue
                ret.append({'lxc.network.{0}'.format(key): val})
            # gateway (in automode) must be appended following network conf !
            if not gateway:
                gateway = args.get('gateway', None)
            if gateway is not None and not gateway_set:
                ret.append({'lxc.network.ipv4.gateway': gateway})
                # only one network gateway ;)
                gateway_set = True
        # normally, this wont happen
        # set the gateway if specified even if we did
        # not managed the network underlying
        if gateway is not None and not gateway_set:
            ret.append({'lxc.network.ipv4.gateway': gateway})
            # only one network gateway ;)
            gateway_set = True

    new = _get_veths(ret)
    # verify that we did not loose the mac settings
    for iface in [a for a in new]:
        ndata = new[iface]
        nmac = ndata.get('lxc.network.hwaddr', '')
        ntype = ndata.get('lxc.network.type', '')
        omac, otype = '', ''
        if iface in old:
            odata = old[iface]
            omac = odata.get('lxc.network.hwaddr', '')
            otype = odata.get('lxc.network.type', '')
        # default for network type is setted here
        # attention not to change the network type
        # without a good and explicit reason to.
        if otype and not ntype:
            ntype = otype
        if not ntype:
            ntype = 'veth'
        new[iface]['lxc.network.type'] = ntype
        if omac and not nmac:
            new[iface]['lxc.network.hwaddr'] = omac

    ret = []
    for val in six.itervalues(new):
        for row in val:
            ret.append(salt.utils.odict.OrderedDict([(row, val[row])]))
    return ret


def _get_lxc_default_data(**kwargs):
    kwargs = copy.deepcopy(kwargs)
    ret = {}
    autostart = kwargs.get('autostart')
    # autostart can have made in kwargs, but with the None
    # value which is invalid, we need an explicit boolean
    # autostart = on is the default.
    if autostart is None:
        autostart = True
    # we will set the regular lxc marker to restart container at
    # machine (re)boot only if we did not explicitly ask
    # not to touch to the autostart settings via
    # autostart == 'keep'
    if autostart != 'keep':
        if autostart:
            ret['lxc.start.auto'] = '1'
        else:
            ret['lxc.start.auto'] = '0'
    memory = kwargs.get('memory')
    if memory is not None:
        ret['lxc.cgroup.memory.limit_in_bytes'] = memory * 1024
    cpuset = kwargs.get('cpuset')
    if cpuset:
        ret['lxc.cgroup.cpuset.cpus'] = cpuset
    cpushare = kwargs.get('cpushare')
    cpu = kwargs.get('cpu')
    if cpushare:
        ret['lxc.cgroup.cpu.shares'] = cpushare
    if cpu and not cpuset:
        ret['lxc.cgroup.cpuset.cpus'] = _rand_cpu_str(cpu)
    return ret


def _config_list(conf_tuples=None, only_net=False, **kwargs):
    '''
    Return a list of dicts from the salt level configurations

    conf_tuples
        _LXCConfig compatible list of entries which can contain

            - string line
            - tuple (lxc config param,value)
            - dict of one entry: {lxc config param: value)

    only_net
        by default we add to the tuples a reflection of both
        the real config if avalaible and a certain amount of
        default values like the cpu parameters, the memory
        and etc.
        On the other hand, we also no matter the case reflect
        the network configuration computed from the actual config if
        available and given values.
        if no_default_loads is set, we will only
        reflect the network configuration back to the conf tuples
        list

    '''
    # explicit cast
    only_net = bool(only_net)
    if not conf_tuples:
        conf_tuples = []
    kwargs = copy.deepcopy(kwargs)
    ret = []
    if not only_net:
        default_data = _get_lxc_default_data(**kwargs)
        for k, val in six.iteritems(default_data):
            ret.append({k: val})
    net_datas = _network_conf(conf_tuples=conf_tuples, **kwargs)
    ret.extend(net_datas)
    return ret


def _get_veths(net_data):
    '''
    Parse the nic setup inside lxc conf tuples back to a dictionary indexed by
    network interface
    '''
    if isinstance(net_data, dict):
        net_data = list(net_data.items())
    nics = salt.utils.odict.OrderedDict()
    current_nic = salt.utils.odict.OrderedDict()
    for item in net_data:
        if item and isinstance(item, dict):
            item = list(item.items())[0]
        # skip LXC configuration comment lines, and play only with tuples conf
        elif isinstance(item, six.string_types):
            # deal with reflection of commented lxc configs
            sitem = item.strip()
            if sitem.startswith('#') or not sitem:
                continue
            elif '=' in item:
                item = tuple([a.strip() for a in item.split('=', 1)])
        if item[0] == 'lxc.network.type':
            current_nic = salt.utils.odict.OrderedDict()
        if item[0] == 'lxc.network.name':
            nics[item[1].strip()] = current_nic
        current_nic[item[0].strip()] = item[1].strip()
    return nics


class _LXCConfig(object):
    '''
    LXC configuration data
    '''
    pattern = re.compile(r'^(\S+)(\s*)(=)(\s*)(.*)')
    non_interpretable_pattern = re.compile(r'^((#.*)|(\s*))$')

    def __init__(self, **kwargs):
        kwargs = copy.deepcopy(kwargs)
        self.name = kwargs.pop('name', None)
        self.data = []
        if self.name:
            self.path = '/var/lib/lxc/{0}/config'.format(self.name)
            if os.path.isfile(self.path):
                with salt.utils.fopen(self.path) as fhr:
                    for line in fhr.readlines():
                        match = self.pattern.findall((line.strip()))
                        if match:
                            self.data.append((match[0][0], match[0][-1]))
                        match = self.non_interpretable_pattern.findall(
                            (line.strip()))
                        if match:
                            self.data.append(('', match[0][0]))
        else:
            self.path = None

        def _replace(key, val):
            if val:
                self._filter_data(key)
                self.data.append((key, val))

        default_data = _get_lxc_default_data(**kwargs)
        for key, val in six.iteritems(default_data):
            _replace(key, val)
        old_net = self._filter_data('lxc.network')
        net_datas = _network_conf(conf_tuples=old_net, **kwargs)
        if net_datas:
            for row in net_datas:
                self.data.extend(list(row.items()))

        # be sure to reset harmful settings
        for idx in ['lxc.cgroup.memory.limit_in_bytes']:
            if not default_data.get(idx):
                self._filter_data(idx)

    def as_string(self):
        chunks = ('{0[0]}{1}{0[1]}'.format(item, (' = ' if item[0] else '')) for item in self.data)
        return '\n'.join(chunks) + '\n'

    def write(self):
        if self.path:
            content = self.as_string()
            # 2 step rendering to be sure not to open/wipe the config
            # before as_string succeeds.
            with salt.utils.fopen(self.path, 'w') as fic:
                fic.write(content)
                fic.flush()

    def tempfile(self):
        # this might look like the function name is shadowing the
        # module, but it's not since the method belongs to the class
        ntf = tempfile.NamedTemporaryFile()
        ntf.write(self.as_string())
        ntf.flush()
        return ntf

    def _filter_data(self, pattern):
        '''
        Removes parameters which match the pattern from the config data
        '''
        removed = []
        filtered = []
        for param in self.data:
            if not param[0].startswith(pattern):
                filtered.append(param)
            else:
                removed.append(param)
        self.data = filtered
        return removed


def _get_base(**kwargs):
    '''
    If the needed base does not exist, then create it, if it does exist
    create nothing and return the name of the base lxc container so
    it can be cloned.
    '''
    profile = get_container_profile(copy.deepcopy(kwargs.get('profile')))
    kw_overrides = copy.deepcopy(kwargs)

    def select(key, default=None):
        kw_overrides_match = kw_overrides.pop(key, _marker)
        profile_match = profile.pop(key, default)
        # let kwarg overrides be the preferred choice
        if kw_overrides_match is _marker:
            return profile_match
        return kw_overrides_match

    template = select('template')
    image = select('image')
    vgname = select('vgname')
    # remove the above three variables from kwargs, if they exist, to avoid
    # duplicates if create() is invoked below.
    for param in ('image', 'vgname', 'template'):
        kwargs.pop(param, None)

    if image:
        proto = _urlparse(image).scheme
        img_tar = __salt__['cp.cache_file'](image)
        img_name = os.path.basename(img_tar)
        hash_ = salt.utils.get_hash(
                img_tar,
                __salt__['config.get']('hash_type'))
        name = '__base_{0}_{1}_{2}'.format(proto, img_name, hash_)
        if not exists(name):
            create(name, template=template, image=image,
                   vgname=vgname, **kwargs)
            if vgname:
                rootfs = os.path.join('/dev', vgname, name)
                edit_conf(info(name)['config'], out_format='commented', **{'lxc.rootfs': rootfs})
        return name
    elif template:
        name = '__base_{0}'.format(template)
        if not exists(name):
            create(name, template=template, image=image,
                   vgname=vgname, **kwargs)
            if vgname:
                rootfs = os.path.join('/dev', vgname, name)
                edit_conf(info(name)['config'], out_format='commented', **{'lxc.rootfs': rootfs})
        return name
    return ''


def init(name,
         config=None,
         cpuset=None,
         cpushare=None,
         memory=None,
         profile=None,
         network_profile=_marker,
         nic=_marker,
         nic_opts=None,
         cpu=None,
         autostart=True,
         password=None,
         password_encrypted=None,
         users=None,
         dnsservers=None,
         searchdomains=None,
         bridge=None,
         gateway=None,
         pub_key=None,
         priv_key=None,
         force_install=False,
         unconditional_install=False,
         bootstrap_delay=None,
         bootstrap_args=None,
         bootstrap_shell=None,
         bootstrap_url=None,
         **kwargs):
    '''
    Initialize a new container.

    This is a partial idempotent function as if it is already provisioned, we
    will reset a bit the lxc configuration file but much of the hard work will
    be escaped as markers will prevent re-execution of harmful tasks.

    name
        Name of the container

    image
        A tar archive to use as the rootfs for the container. Conflicts with
        the ``template`` argument.

    cpus
        Select a random number of cpu cores and assign it to the cpuset, if the
        cpuset option is set then this option will be ignored

    cpuset
        Explicitly define the cpus this container will be bound to

    cpushare
        cgroups cpu shares

    autostart
        autostart container on reboot

    memory
        cgroups memory limit, in MB

        .. versionchanged:: 2015.2.0
            If no value is passed, no limit is set. In earlier Salt versions,
            not passing this value causes a 1024MB memory limit to be set, and
            it was necessary to pass ``memory=0`` to set no limit.

    gateway
        the ipv4 gateway to use
        the default does nothing more than lxcutils does

    bridge
        the bridge to use
        the default does nothing more than lxcutils does

    network_profile
        Network profile to use for the container

        .. versionadded:: 2015.2.0

    nic
        .. deprecated:: 2015.2.0
            Use ``network_profile`` instead

    nic_opts
        Extra options for network interfaces, will override

        ``{"eth0": {"hwaddr": "aa:bb:cc:dd:ee:ff", "ipv4": "10.1.1.1", "ipv6": "2001:db8::ff00:42:8329"}}``

        or

        ``{"eth0": {"hwaddr": "aa:bb:cc:dd:ee:ff", "ipv4": "10.1.1.1/24", "ipv6": "2001:db8::ff00:42:8329"}}``

    users
        Users for which the password defined in the ``password`` param should
        be set. Can be passed as a comma separated list or a python list.
        Defaults to just the ``root`` user.

    password
        Set the initial password for the users defined in the ``users``
        parameter

    password_encrypted : False
        Set to ``True`` to denote a password hash instead of a plaintext
        password

        .. versionadded:: 2015.2.0

    profile
        A LXC profile (defined in config or pillar).
        This can be either a real profile mapping or a string
        to retrieve it in configuration

    start
        Start the newly-created container

    dnsservers
        list of dns servers to set in the container, default [] (no setting)

    seed
        Seed the container with the minion config. Default: ``True``

    install
        If salt-minion is not already installed, install it. Default: ``True``

    config
        Optional config parameters. By default, the id is set to
        the name of the container.

    pub_key
        Explicit public key to preseed the minion with (optional).
        This can be either a filepath or a string representing the key

    priv_key
        Explicit private key to preseed the minion with (optional).
        This can be either a filepath or a string representing the key

    approve_key
        If explicit preseeding is not used;
        Attempt to request key approval from the master. Default: ``True``

    clone
        .. deprecated:: 2015.2.0
            Use ``clone_from`` instead

    clone_from
        Original from which to use a clone operation to create the container.
        Default: ``None``

    bootstrap_delay
        Delay in seconds between end of container creation and bootstrapping.
        Useful when waiting for container to obtain a DHCP lease.

        .. versionadded:: 2015.2.0

    bootstrap_url
        See lxc.bootstrap

    bootstrap_shell
        See lxc.bootstrap

    bootstrap_args
        See lxc.bootstrap

    force_install
        Force installation even if salt-minion is detected,
        this is the way to run vendor bootstrap scripts even
        if a salt minion is already present in the container

    unconditional_install
        Run the script even if the container seems seeded

    CLI Example:

    .. code-block:: bash

        salt 'minion' lxc.init name [cpuset=cgroups_cpuset] \\
                [cpushare=cgroups_cpushare] [memory=cgroups_memory] \\
                [nic=nic_profile] [profile=lxc_profile] \\
                [nic_opts=nic_opts] [start=(True|False)] \\
                [seed=(True|False)] [install=(True|False)] \\
                [config=minion_config] [approve_key=(True|False) \\
                [clone_from=original] [autostart=True] \\
                [priv_key=/path_or_content] [pub_key=/path_or_content] \\
                [bridge=lxcbr0] [gateway=10.0.3.1] \\
                [dnsservers[dns1,dns2]] \\
                [users=[foo]] [password='secret'] \\
                [password_encrypted=(True|False)]

    '''
    ret = {'name': name,
           'changes': {}}

    if bool(nic) and nic is not _marker:
        salt.utils.warn_until(
            'Boron',
            'The \'nic\' argument to \'lxc.init\' has been deprecated, '
            'please use \'network_profile\' instead.'
        )
        network_profile = nic
    if network_profile is _marker:
        network_profile = DEFAULT_NIC

    try:
        kwargs['clone_from'] = kwargs.pop('clone')
    except KeyError:
        pass
    else:
        salt.utils.warn_until(
            'Boron',
            'The \'clone\' argument to \'lxc.init\' has been deprecated, '
            'please use \'clone_from\' instead.'
        )

    # Changes is a pointer to changes_dict['init']. This method is used so that
    # we can have a list of changes as they are made, providing an ordered list
    # of things that were changed.
    changes_dict = {'init': []}
    changes = changes_dict.get('init')

    state_pre = state(name)

    if users is None:
        users = []
    dusers = ['root']
    for user in dusers:
        if user not in users:
            users.append(user)

    profile = get_container_profile(copy.deepcopy(profile))
    kw_overrides = copy.deepcopy(kwargs)

    def select(key, default=None):
        kw_overrides_match = kw_overrides.pop(key, _marker)
        profile_match = profile.pop(key, default)
        # let kwarg overrides be the preferred choice
        if kw_overrides_match is _marker:
            return profile_match
        return kw_overrides_match

    tvg = select('vgname')
    vgname = tvg if tvg else __salt__['config.get']('lxc.vgname')
    start_ = select('start', True)
    autostart = select('autostart', autostart)
    seed = select('seed', True)
    install = select('install', True)
    seed_cmd = select('seed_cmd')
    salt_config = select('config')
    approve_key = select('approve_key', True)
    clone_from = select('clone_from')
    if password and password_encrypted is None:
        salt.utils.warn_until(
            'Boron',
            'Starting with the Boron release, passwords passed to the '
            '\'lxc.init\' function will be assumed to be hashed, unless '
            'password_encrypted=False. Please keep this in mind.'
        )
        password_encrypted = False

    # If using a volume group then set up to make snapshot cow clones
    if vgname and not clone_from:
        try:
            clone_from = _get_base(vgname=vgname, profile=profile, **kwargs)
        except (SaltInvocationError, CommandExecutionError) as exc:
            ret['comment'] = exc.message
            if changes:
                ret['changes'] = changes_dict
            return ret
        if not kwargs.get('snapshot') is False:
            kwargs['snapshot'] = True
    does_exist = exists(name)
    to_reboot = False
    remove_seed_marker = False
    if does_exist:
        pass
    elif clone_from:
        remove_seed_marker = True
        try:
            clone(name, clone_from, profile=profile, **kwargs)
            changes.append({'create': 'Container cloned'})
        except (SaltInvocationError, CommandExecutionError) as exc:
            if 'already exists' in exc.strerror:
                changes.append({'create': 'Container already exists'})
            else:
                ret['result'] = False
                ret['comment'] = exc.strerror
                if changes:
                    ret['changes'] = changes_dict
                return ret
        cfg = _LXCConfig(name=name, network_profile=network_profile,
                         nic_opts=nic_opts, bridge=bridge,
                         gateway=gateway, autostart=autostart,
                         cpuset=cpuset, cpushare=cpushare, memory=memory)
        old_chunks = read_conf(cfg.path, out_format='commented')
        cfg.write()
        chunks = read_conf(cfg.path, out_format='commented')
        if old_chunks != chunks:
            to_reboot = True
    else:
        remove_seed_marker = True
        cfg = _LXCConfig(network_profile=network_profile,
                         nic_opts=nic_opts, cpuset=cpuset,
                         bridge=bridge, gateway=gateway,
                         autostart=autostart,
                         cpushare=cpushare, memory=memory)
        with cfg.tempfile() as cfile:
            try:
                create(name, config=cfile.name, profile=profile, **kwargs)
                changes.append({'create': 'Container created'})
            except (SaltInvocationError, CommandExecutionError) as exc:
                if 'already exists' in exc.strerror:
                    changes.append({'create': 'Container already exists'})
                else:
                    ret['comment'] = exc.strerror
                    if changes:
                        ret['changes'] = changes_dict
                    return ret
        path = '/var/lib/lxc/{0}/config'.format(name)
        old_chunks = []
        if os.path.exists(path):
            old_chunks = read_conf(path, out_format='commented')
        new_cfg = _config_list(conf_tuples=old_chunks,
                                 cpu=cpu,
                                 network_profile=network_profile,
                                 nic_opts=nic_opts, bridge=bridge,
                                 cpuset=cpuset, cpushare=cpushare,
                                 memory=memory)
        if new_cfg:
            edit_conf(path, out_format='commented', lxc_config=new_cfg)
        chunks = read_conf(path, out_format='commented')
        if old_chunks != chunks:
            to_reboot = True
    if remove_seed_marker:
        run(name,
            'rm -f \'{0}\''.format(SEED_MARKER),
            python_shell=False)

    # last time to be sure any of our property is correctly applied
    cfg = _LXCConfig(name=name, network_profile=network_profile,
                     nic_opts=nic_opts, bridge=bridge,
                     gateway=gateway, autostart=autostart,
                     cpuset=cpuset, cpushare=cpushare, memory=memory)
    old_chunks = []
    if os.path.exists(cfg.path):
        old_chunks = read_conf(cfg.path, out_format='commented')
    cfg.write()
    chunks = read_conf(cfg.path, out_format='commented')
    if old_chunks != chunks:
        changes.append({'config': 'Container configuration updated'})
        to_reboot = True

    if to_reboot:
        try:
            stop(name)
        except (SaltInvocationError, CommandExecutionError) as exc:
            ret['comment'] = 'Unable to stop container: {0}'.format(exc)
            if changes:
                ret['changes'] = changes_dict
            return ret
    if not does_exist or (does_exist and state(name) != 'running'):
        try:
            start(name)
        except (SaltInvocationError, CommandExecutionError) as exc:
            ret['comment'] = 'Unable to stop container: {0}'.format(exc)
            if changes:
                ret['changes'] = changes_dict
            return ret

    # set the default user/password, only the first time
    if ret.get('result', True) and password:
        gid = '/.lxc.initial_pass'
        gids = [gid,
                '/lxc.initial_pass',
                '/.lxc.{0}.initial_pass'.format(name)]
        if not any(retcode(name,
                           'test -e "{0}"'.format(x),
                           ignore_retcode=True) == 0
                   for x in gids):
            # think to touch the default user generated by default templates
            # which has a really unsecure passwords...
            # root is defined as a member earlier in the code
            for default_user in ['ubuntu']:
                if (
                    default_user not in users
                    and retcode(name,
                                'id {0}'.format(default_user),
                                python_shell=False,
                                ignore_retcode=True) == 0
                ):
                    users.append(default_user)
            for user in users:
                try:
                    cret = set_password(name,
                                        users=[user],
                                        password=password,
                                        encrypted=password_encrypted)
                except (SaltInvocationError, CommandExecutionError) as exc:
                    msg = '{0}: Failed to set password'.format(
                        user) + exc.strerror
                    # only hardfail in unrecoverable situation:
                    # root cannot be setted up
                    if user == 'root':
                        ret['comment'] = msg
                        ret['result'] = False
                    else:
                        log.debug(msg)
            if ret.get('result', True):
                changes.append({'password': 'Password(s) updated'})
                if retcode(name,
                           ('sh -c \'touch "{0}"; test -e "{0}"\''
                            .format(gid)),
                           ignore_retcode=True) != 0:
                    ret['comment'] = 'Failed to set password marker'
                    changes[-1]['password'] += '. ' + ret['comment'] + '.'
                    ret['result'] = False

    # set dns servers if any, only the first time
    if ret.get('result', True) and dnsservers:
        # retro compatibility, test also old markers
        gid = '/.lxc.initial_dns'
        gids = [gid,
                '/lxc.initial_dns',
                '/lxc.{0}.initial_dns'.format(name)]
        if not any(retcode(name,
                           'test -e "{0}"'.format(x),
                           ignore_retcode=True) == 0
                   for x in gids):
            try:
                set_dns(name,
                        dnsservers=dnsservers,
                        searchdomains=searchdomains)
            except (SaltInvocationError, CommandExecutionError) as exc:
                ret['comment'] = 'Failed to set DNS: ' + exc.strerror
                ret['result'] = False
            else:
                changes.append({'dns': 'DNS updated'})
                if retcode(name,
                           ('sh -c \'touch "{0}"; test -e "{0}"\''
                            .format(gid)),
                           ignore_retcode=True) != 0:
                    ret['comment'] = 'Failed to set DNS marker'
                    changes[-1]['dns'] += '. ' + ret['comment'] + '.'
                    ret['result'] = False

    # retro compatibility, test also old markers
    gid = '/.lxc.initial_seed'
    gids = [gid, '/lxc.initial_seed']
    if (
        any(retcode(name,
                    'test -e {0}'.format(x),
                    ignore_retcode=True) == 0
            for x in gids)
        or not ret.get('result', True)
    ):
        pass
    elif seed or seed_cmd:
        if seed:
            try:
                result = bootstrap(
                    name, config=salt_config,
                    approve_key=approve_key,
                    pub_key=pub_key, priv_key=priv_key,
                    install=install,
                    force_install=force_install,
                    unconditional_install=unconditional_install,
                    bootstrap_delay=bootstrap_delay,
                    bootstrap_url=bootstrap_url,
                    bootstrap_shell=bootstrap_shell,
                    bootstrap_args=bootstrap_args)
            except (SaltInvocationError, CommandExecutionError) as exc:
                ret['comment'] = 'Bootstrap failed: ' + exc.strerror
                ret['result'] = False
            else:
                if not result:
                    ret['comment'] = ('Bootstrap failed, see minion log for '
                                      'more information')
                    ret['result'] = False
                else:
                    changes.append(
                        {'bootstrap': 'Continer successfully bootstrapped'}
                    )
        elif seed_cmd:
            try:
                result = __salt__[seed_cmd](info(name)['rootfs'],
                                            name,
                                            salt_config)
            except (SaltInvocationError, CommandExecutionError) as exc:
                ret['comment'] = ('Bootstrap via seed_cmd \'{0}\' failed: {1}'
                                  .format(seed_cmd, exc.strerror))
                ret['result'] = False
            else:
                if not result:
                    ret['comment'] = ('Bootstrap via seed_cmd \'{0}\' failed, '
                                      'see minion log for more information '
                                      .format(seed_cmd))
                    ret['result'] = False
                else:
                    changes.append(
                        {'bootstrap': 'Continer successfully bootstrapped '
                                      'using seed_cmd \'{0}\''
                                      .format(seed_cmd)}
                    )

    if ret.get('result', True) and not start_:
        try:
            stop(name)
        except (SaltInvocationError, CommandExecutionError) as exc:
            ret['comment'] = 'Unable to stop container: {0}'.format(exc)
            ret['result'] = False

    state_post = state(name)
    if state_pre != state_post:
        changes.append({'state': {'old': state_pre, 'new': state_post}})

    if ret.get('result', True):
        ret['comment'] = ('Container \'{0}\' successfully initialized'
                          .format(name))
        ret['result'] = True
    if changes:
        ret['changes'] = changes_dict
    return ret


def cloud_init(name, vm_=None, **kwargs):
    '''
    Thin wrapper to lxc.init to be used from the saltcloud lxc driver

    name
        Name of the container
        may be None and then guessed from saltcloud mapping
    `vm_`
        saltcloud mapping defaults for the vm

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.cloud_init foo
    '''
    init_interface = cloud_init_interface(name, vm_, **kwargs)
    name = init_interface.pop('name', name)
    return init(name, **init_interface)


def images(dist=None):
    '''
    .. versionadded:: 2015.2.0

    List the available images for LXC's ``download`` template.

    dist : None
        Filter results to a single Linux distribution

    CLI Examples:

    .. code-block:: bash

        salt myminion lxc.images
        salt myminion lxc.images dist=centos
    '''
    out = __salt__['cmd.run_stdout'](
        'lxc-create -n __imgcheck -t download -- --list',
        ignore_retcode=True
    )
    if 'DIST' not in out:
        raise CommandExecutionError(
            'Unable to run the \'download\' template script. Is it installed?'
        )

    ret = {}
    passed_header = False
    for line in out.splitlines():
        try:
            distro, release, arch, variant, build_time = line.split()
        except ValueError:
            continue

        if not passed_header:
            if distro == 'DIST':
                passed_header = True
            continue

        dist_list = ret.setdefault(distro, [])
        dist_list.append({
            'release': release,
            'arch': arch,
            'variant': variant,
            'build_time': build_time,
        })

    if dist is not None:
        return dict([(dist, ret.get(dist, []))])
    return ret


def templates():
    '''
    .. versionadded:: 2015.2.0

    List the available LXC template scripts installed on the minion

    CLI Examples:

    .. code-block:: bash

        salt myminion lxc.templates
    '''
    try:
        template_scripts = os.listdir('/usr/share/lxc/templates')
    except OSError:
        return []
    else:
        return [x[4:] for x in template_scripts if x.startswith('lxc-')]


def create(name,
           config=None,
           profile=None,
           network_profile=None,
           nic_opts=None,
           **kwargs):
    '''
    Create a new container.

    name
        Name of the container

    config
        The config file to use for the container. Defaults to system-wide
        config (usually in /etc/lxc/lxc.conf).

    profile
        Profile to use in container creation (see
        :mod:`lxc.get_container_profile
        <salt.modules.lxc.get_container_profile>`). Values in a profile will be
        overridden by the **Container Creation Arguments** listed below.

    network_profile
        Network profile to use for container

        .. versionadded:: 2015.2.0

    **Container Creation Arguments**

    template
        The template to use. For example, ``ubuntu`` or ``fedora``. Conflicts
        with the ``image`` argument.

        .. note::

            The ``download`` template requires the following three parameters
            to be defined in ``options``:

            * **dist** - The name of the distribution
            * **release** - Release name/version
            * **arch** - Architecture of the container

            The available images can be listed using the :mod:`lxc.images
            <salt.modules.lxc.images>` function.

    options
        Template-specific options to pass to the lxc-create command. These
        correspond to the long options (ones beginning with two dashes) that
        the template script accepts. For example:

        .. code-block:: bash

            options='{"dist": "centos", "release": "6", "arch": "amd64"}'

    image
        A tar archive to use as the rootfs for the container. Conflicts with
        the ``template`` argument.

    backing
        The type of storage to use. Set to ``lvm`` to use an LVM group.
        Defaults to filesystem within /var/lib/lxc.

    fstype
        Filesystem type to use on LVM logical volume

    size : 1G
        Size of the volume to create. Only applicable if ``backing=lvm``.

    vgname : lxc
        Name of the LVM volume group in which to create the volume for this
        container. Only applicable if ``backing=lvm``.

    lvname
        Name of the LVM logical volume in which to create the volume for this
        container. Only applicable if ``backing=lvm``.
    nic_opts
        give extra opts overriding network profile values
    '''
    if exists(name):
        raise CommandExecutionError(
            'Container \'{0}\' already exists'.format(name)
        )

    # Required params for 'download' template
    download_template_deps = ('dist', 'release', 'arch')

    cmd = 'lxc-create -n {0}'.format(name)

    profile = get_container_profile(copy.deepcopy(profile))
    kw_overrides = copy.deepcopy(kwargs)

    def select(key, default=None):
        kw_overrides_match = kw_overrides.pop(key, None)
        profile_match = profile.pop(key, default)
        # Return the profile match if the the kwarg match was None, as the
        # lxc.present state will pass these kwargs set to None by default.
        if kw_overrides_match is None:
            return profile_match
        return kw_overrides_match

    tvg = select('vgname')
    vgname = tvg if tvg else __salt__['config.get']('lxc.vgname')

    # The 'template' and 'image' params conflict
    template = select('template')
    image = select('image')
    if template and image:
        raise SaltInvocationError(
            'Only one of \'template\' and \'image\' is permitted'
        )
    elif not any((template, image, profile)):
        raise SaltInvocationError(
            'At least one of \'template\', \'image\', and \'profile\' is '
            'required'
        )

    options = select('options') or {}
    backing = select('backing')
    if vgname and not backing:
        backing = 'lvm'
    lvname = select('lvname')
    fstype = select('fstype')
    size = select('size', '1G')
    if backing in ('dir', 'overlayfs', 'btrfs'):
        fstype = None
        size = None
    # some backends wont support some parameters
    if backing in ('aufs', 'dir', 'overlayfs', 'btrfs'):
        lvname = vgname = None

    if image:
        img_tar = __salt__['cp.cache_file'](image)
        template = os.path.join(
                os.path.dirname(salt.__file__),
                'templates',
                'lxc',
                'salt_tarball')
        options['imgtar'] = img_tar
    if config:
        cmd += ' -f {0}'.format(config)
    if template:
        cmd += ' -t {0}'.format(template)
    if backing:
        backing = backing.lower()
        cmd += ' -B {0}'.format(backing)
        if backing in ('lvm',):
            if lvname:
                cmd += ' --lvname {0}'.format(vgname)
            if vgname:
                cmd += ' --vgname {0}'.format(vgname)
        if backing not in ('dir', 'overlayfs'):
            if fstype:
                cmd += ' --fstype {0}'.format(fstype)
            if size:
                cmd += ' --fssize {0}'.format(size)

    if options:
        if template == 'download':
            missing_deps = [x for x in download_template_deps
                            if x not in options]
            if missing_deps:
                raise SaltInvocationError(
                    'Missing params in \'options\' dict: {0}'
                    .format(', '.join(missing_deps))
                )
        cmd += ' --'
        for key, val in six.iteritems(options):
            cmd += ' --{0} {1}'.format(key, val)

    ret = __salt__['cmd.run_all'](cmd,
                                  python_shell=False,
                                  output_loglevel='trace')
    _clear_context()
    if ret['retcode'] == 0 and exists(name):
        if network_profile:
            network_changes = apply_network_profile(name,
                                                    network_profile,
                                                    nic_opts=nic_opts)

            if network_changes:
                log.info(
                    'Network changes from applying network profile \'{0}\' '
                    'to newly-created container \'{1}\':\n{2}'
                    .format(network_profile, name, network_changes)
                )
        c_state = state(name)
        return {'result': True,
                'state': {'old': None, 'new': c_state}}
    else:
        if exists(name):
            # destroy the container if it was partially created
            cmd = 'lxc-destroy -n {0}'.format(name)
            __salt__['cmd.retcode'](cmd, python_shell=False)
        raise CommandExecutionError(
            'Container could not be created with cmd \'{0}\': {1}'
            .format(cmd, ret['stderr'])
        )


def clone(name,
          orig,
          profile=None,
          **kwargs):
    '''
    Create a new container as a clone of another container

    name
        Name of the container

    orig
        Name of the original container to be cloned

    profile
        Profile to use in container cloning (see
        :mod:`lxc.get_container_profile
        <salt.modules.lxc.get_container_profile>`). Values in a profile will be
        overridden by the **Container Cloning Arguments** listed below.

    **Container Cloning Arguments**

    snapshot
        Use Copy On Write snapshots (LVM)

    size : 1G
        Size of the volume to create. Only applicable if ``backing=lvm``.

    backing
        The type of storage to use. Set to ``lvm`` to use an LVM group.
        Defaults to filesystem within /var/lib/lxc.


    CLI Examples:

    .. code-block:: bash

        salt '*' lxc.clone myclone orig=orig_container
        salt '*' lxc.clone myclone orig=orig_container snapshot=True
    '''
    if exists(name):
        raise CommandExecutionError(
            'Container \'{0}\' already exists'.format(name)
        )

    _ensure_exists(orig)
    if state(orig) != 'stopped':
        raise CommandExecutionError(
            'Container \'{0}\' must be stopped to be cloned'.format(orig)
        )

    profile = get_container_profile(copy.deepcopy(profile))
    kw_overrides = copy.deepcopy(kwargs)

    def select(key, default=None):
        kw_overrides_match = kw_overrides.pop(key, None)
        profile_match = profile.pop(key, default)
        # let kwarg overrides be the preferred choice
        if kw_overrides_match is None:
            return profile_match
        return kw_overrides_match

    backing = select('backing')
    snapshot = select('snapshot')
    if backing in ('dir',):
        snapshot = False
    if not snapshot:
        snapshot = ''
    else:
        snapshot = '-s'

    size = select('size', '1G')
    if backing in ('dir', 'overlayfs', 'btrfs'):
        size = None

    cmd = 'lxc-clone {0} -o {1} -n {2}'.format(snapshot, orig, name)
    if backing:
        backing = backing.lower()
        cmd += ' -B {0}'.format(backing)
        if backing not in ('dir', 'overlayfs'):
            if size:
                cmd += ' --fssize {0}'.format(size)

    ret = __salt__['cmd.run_all'](cmd, python_shell=False)
    _clear_context()
    if ret['retcode'] == 0 and exists(name):
        c_state = state(name)
        return {'result': True,
                'state': {'old': None, 'new': c_state}}
    else:
        if exists(name):
            # destroy the container if it was partially created
            cmd = 'lxc-destroy -n {0}'.format(name)
            __salt__['cmd.retcode'](cmd, python_shell=False)
        raise CommandExecutionError(
            'Container could not be cloned with cmd \'{0}\': {1}'
            .format(cmd, ret['stderr'])
        )


def ls_(active=None, cache=True):
    '''
    Return a list of the containers available on the minion

    active
        If ``True``, return only active (i.e. running) containers

        .. versionadded:: 2015.2.0

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.ls
        salt '*' lxc.ls active=True
    '''
    contextvar = 'lxc.ls'
    if active:
        contextvar += '.active'
    if cache and (contextvar in __context__):
        return __context__[contextvar]
    else:
        ret = []
        cmd = 'lxc-ls'
        if active:
            cmd += ' --active'
        output = __salt__['cmd.run_stdout'](cmd, python_shell=False)
        for line in output.splitlines():
            ret.extend(line.split())
        __context__[contextvar] = ret
        return ret


def list_(extra=False, limit=None):
    '''
    List containers classified by state

    extra
        Also get per-container specific info. This will change the return data.
        Instead of returning a list of containers, a dictionary of containers
        and each container's output from :mod:`lxc.info
        <salt.modules.lxc.info>`.

    limit
        Return output matching a specific state (**frozen**, **running**, or
        **stopped**).

        .. versionadded:: 2015.2.0

    CLI Examples:

    .. code-block:: bash

        salt '*' lxc.list
        salt '*' lxc.list extra=True
        salt '*' lxc.list limit=running
    '''
    ctnrs = ls_()

    if extra:
        stopped = {}
        frozen = {}
        running = {}
    else:
        stopped = []
        frozen = []
        running = []

    ret = {'running': running,
           'stopped': stopped,
           'frozen': frozen}

    for container in ctnrs:
        c_info = __salt__['cmd.run'](
            'lxc-info -n {0}'.format(container),
            python_shell=False,
            output_loglevel='debug'
        )
        c_state = None
        for line in c_info.splitlines():
            stat = line.split(':')
            if stat[0] in ('State', 'state'):
                c_state = stat[1].strip()
                break

        if not c_state or (limit is not None and c_state.lower() != limit):
            continue

        if extra:
            infos = info(container)
            method = 'update'
            value = {container: infos}
        else:
            method = 'append'
            value = container

        if c_state == 'STOPPED':
            getattr(stopped, method)(value)
            continue
        if c_state == 'FROZEN':
            getattr(frozen, method)(value)
            continue
        if c_state == 'RUNNING':
            getattr(running, method)(value)
            continue

    if limit is not None:
        return ret.get(limit, {} if extra else [])
    return ret


def _change_state(cmd, name, expected):
    pre = state(name)
    if pre == expected:
        return {'result': True,
                'state': {'old': expected, 'new': expected},
                'comment': 'Container \'{0}\' already {1}'
                           .format(name, expected)}

    if cmd == 'lxc-destroy':
        # Kill the container first
        __salt__['cmd.run']('lxc-stop -k -n {0}'.format(name),
                            python_shell=False)

    cmd = '{0} -n {1}'.format(cmd, name)
    error = __salt__['cmd.run_stderr'](cmd, python_shell=False)
    if error:
        raise CommandExecutionError(
            'Error changing state for container \'{0}\' using command '
            '\'{1}\': {2}'.format(name, cmd, error)
        )
    if expected is not None:
        # some commands do not wait, so we will
        cmd = 'lxc-wait -n {0} -s {1}'.format(name, expected.upper())
        __salt__['cmd.run'](cmd, python_shell=False, timeout=30)
    _clear_context()
    post = state(name)
    ret = {'result': post == expected,
           'state': {'old': pre, 'new': post}}
    return ret


def _ensure_exists(name):
    '''
    Raise an exception if the container does not exist
    '''
    if not exists(name):
        raise CommandExecutionError(
            'Container \'{0}\' does not exist'.format(name)
        )


def _ensure_running(name, no_start=False):
    '''
    If the container is not currently running, start it. This function returns
    the state that the container was in before changing
    '''
    _ensure_exists(name)
    pre = state(name)
    if pre == 'running':
        # This will be a no-op but running the function will give us a pretty
        # return dict.
        return start(name)
    elif pre == 'stopped':
        if no_start:
            raise CommandExecutionError(
                'Container \'{0}\' is not running'.format(name)
            )
        return start(name)
    elif pre == 'frozen':
        if no_start:
            raise CommandExecutionError(
                'Container \'{0}\' is not running'.format(name)
            )
        return unfreeze(name)


def restart(name, force=False):
    '''
    .. versionadded:: 2015.2.0

    Restart the named container. If the container was not running, the
    container will merely be started.

    name
        The name of the container

    force : False
        If ``True``, the container will be force-stopped instead of gracefully
        shut down

    CLI Example:

    .. code-block:: bash

        salt myminion lxc.restart name
    '''
    _ensure_exists(name)
    orig_state = state(name)
    if orig_state != 'stopped':
        stop(name, kill=force)
    ret = start(name)
    ret['state']['old'] = orig_state
    if orig_state != 'stopped':
        ret['restarted'] = True
    return ret


def start(name, **kwargs):
    '''
    Start the named container

    restart : False
        .. deprecated:: 2015.2.0
            Use :mod:`lxc.restart <salt.modules.lxc.restart>`

        Restart the container if it is already running

    CLI Example:

    .. code-block:: bash

        salt myminion lxc.start name
    '''
    _ensure_exists(name)
    if kwargs.get('restart', False):
        salt.utils.warn_until(
            'Boron',
            'The \'restart\' argument to \'lxc.start\' has been deprecated, '
            'please use \'lxc.restart\' instead.'
        )
        return restart(name)
    if state(name) == 'frozen':
        raise CommandExecutionError(
            'Container \'{0}\' is frozen, use lxc.unfreeze'.format(name)
        )
    return _change_state('lxc-start -d', name, 'running')


def stop(name, kill=False):
    '''
    Stop the named container

    kill : False
        Do not wait for the container to stop, kill all tasks in the container.
        Older LXC versions will stop containers like this irrespective of this
        argument.

        .. versionchanged:: 2015.2.0
            Default value changed to ``False``

    CLI Example:

    .. code-block:: bash

        salt myminion lxc.stop name
    '''
    _ensure_exists(name)
    orig_state = state(name)
    if orig_state == 'frozen' and not kill:
        # Gracefully stopping a frozen container is slower than unfreezing and
        # then stopping it (at least in my testing), so if we're not
        # force-stopping the container, unfreeze it first.
        unfreeze(name)
    cmd = 'lxc-stop'
    if kill:
        cmd += ' -k'
    ret = _change_state(cmd, name, 'stopped')
    ret['state']['old'] = orig_state
    return ret


def freeze(name, **kwargs):
    '''
    Freeze the named container

    start : False
        If ``True`` and the container is stopped, the container will be started
        before attempting to freeze.

        .. versionadded:: 2015.2.0

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.freeze name
    '''
    _ensure_exists(name)
    orig_state = state(name)
    start_ = kwargs.get('start', False)
    if orig_state == 'stopped':
        if not start_:
            raise CommandExecutionError(
                'Container \'{0}\' is stopped'.format(name)
            )
        start(name)
    ret = _change_state('lxc-freeze', name, 'frozen')
    if orig_state == 'stopped' and start_:
        ret['state']['old'] = orig_state
        ret['started'] = True
    ret['state']['new'] = state(name)
    return ret


def unfreeze(name):
    '''
    Unfreeze the named container.

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.unfreeze name
    '''
    _ensure_exists(name)
    if state(name) == 'stopped':
        raise CommandExecutionError(
            'Container \'{0}\' is stopped'.format(name)
        )
    return _change_state('lxc-unfreeze', name, 'running')


def destroy(name, stop=False):
    '''
    Destroy the named container.

    .. warning::

        Destroys all data associated with the container.

    stop : False
        If ``True``, the container will be destroyed even if it is
        running/frozen.

        .. versionchanged:: 2015.2.0
            Default value changed to ``False``. This more closely matches the
            behavior of ``lxc-destroy(1)``, and also makes it less likely that
            an accidental command will destroy a running container that was
            being used for important things.

    CLI Examples:

    .. code-block:: bash

        salt '*' lxc.destroy foo
        salt '*' lxc.destroy foo stop=True
    '''
    _ensure_exists(name)
    if not stop and state(name) != 'stopped':
        raise CommandExecutionError(
            'Container \'{0}\' is not stopped'.format(name)
        )
    return _change_state('lxc-destroy', name, None)

# Compatibility between LXC and nspawn
remove = destroy


def exists(name):
    '''
    Returns whether the named container exists.

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.exists name
    '''

    _exists = name in ls_()
    # container may be just created but we did cached earlier the
    # lxc-ls results
    if not _exists:
        _exists = name in ls_(cache=False)
    return _exists


def state(name):
    '''
    Returns the state of a container.

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.state name
    '''
    # Don't use _ensure_exists() here, it will mess with _change_state()
    try:
        return __context__['lxc.state.{0}'.format(name)]
    except KeyError:
        if not exists(name):
            __context__['lxc.state.{0}'.format(name)] = None
        else:
            cmd = 'lxc-info -n {0}'.format(name)
            ret = __salt__['cmd.run_all'](cmd, python_shell=False)
            if ret['retcode'] != 0:
                _clear_context()
                raise CommandExecutionError(
                    'Unable to get state of container \'{0}\''.format(name)
                )
            c_infos = ret['stdout'].splitlines()
            c_state = None
            for c_info in c_infos:
                stat = c_info.split(':')
                if stat[0].lower() == 'state':
                    c_state = stat[1].strip().lower()
                    break
            __context__['lxc.state.{0}'.format(name)] = c_state
    return __context__['lxc.state.{0}'.format(name)]


def get_parameter(name, parameter):
    '''
    Returns the value of a cgroup parameter for a container

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.get_parameter container_name memory.limit_in_bytes
    '''
    _ensure_exists(name)
    cmd = 'lxc-cgroup -n {0} {1}'.format(name, parameter)
    ret = __salt__['cmd.run_all'](cmd, python_shell=False)
    if ret['retcode'] != 0:
        raise CommandExecutionError(
            'Unable to retrieve value for \'{0}\''.format(parameter)
        )
    return ret['stdout'].strip()


def set_parameter(name, parameter, value):
    '''
    Set the value of a cgroup parameter for a container.

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.set_parameter name parameter value
    '''
    if not exists(name):
        return None

    cmd = 'lxc-cgroup -n {0} {1} {2}'.format(name, parameter, value)
    ret = __salt__['cmd.run_all'](cmd, python_shell=False)
    if ret['retcode'] != 0:
        return False
    else:
        return True


def info(name):
    '''
    Returns information about a container

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.info name
    '''
    try:
        return __context__['lxc.info.{0}'.format(name)]
    except KeyError:
        _ensure_exists(name)
        try:
            conf_file = os.path.join('/var/lib/lxc', name, 'config')
        except AttributeError:
            conf_file = os.path.join('/var/lib/lxc', str(name), 'config')

        if not os.path.isfile(conf_file):
            raise CommandExecutionError(
                'LXC config file {0} does not exist'.format(conf_file)
            )

        ret = {}
        config = []
        with salt.utils.fopen(conf_file) as fp_:
            for line in fp_:
                comps = [x.strip() for x in
                        line.split('#', 1)[0].strip().split('=', 1)]
                if len(comps) == 2:
                    config.append(tuple(comps))

        ifaces = []
        current = None

        for key, val in config:
            if key == 'lxc.network.type':
                current = {'type': val}
                ifaces.append(current)
            elif not current:
                continue
            elif key.startswith('lxc.network.'):
                current[key.replace('lxc.network.', '', 1)] = val
        if ifaces:
            ret['nics'] = ifaces

        ret['rootfs'] = next(
            (x[1] for x in config if x[0] == 'lxc.rootfs'),
            None
        )
        ret['state'] = state(name)
        ret['ips'] = []
        ret['public_ips'] = []
        ret['private_ips'] = []
        ret['public_ipv4_ips'] = []
        ret['public_ipv6_ips'] = []
        ret['private_ipv4_ips'] = []
        ret['private_ipv6_ips'] = []
        ret['ipv4_ips'] = []
        ret['ipv6_ips'] = []
        ret['size'] = None
        ret['config'] = conf_file

        if ret['state'] == 'running':
            try:
                limit = int(get_parameter(name, 'memory.limit_in_bytes'))
            except (CommandExecutionError, TypeError, ValueError):
                limit = 0
            try:
                usage = int(get_parameter(name, 'memory.usage_in_bytes'))
            except (CommandExecutionError, TypeError, ValueError):
                usage = 0
            free = limit - usage
            ret['memory_limit'] = limit
            ret['memory_free'] = free
            size = run_stdout(name, 'df /', python_shell=False)
            # The size is the 2nd column of the last line
            ret['size'] = size.splitlines()[-1].split()[1]

            # First try iproute2
            ip_cmd = run_all(name, 'ip link show', python_shell=False)
            if ip_cmd['retcode'] == 0:
                ip_data = ip_cmd['stdout']
                ip_cmd = run_all(name, 'ip addr show', python_shell=False)
                ip_data += '\n' + ip_cmd['stdout']
                ip_data = salt.utils.network._interfaces_ip(ip_data)
            else:
                # That didn't work, try ifconfig
                ip_cmd = run_all(name, 'ifconfig', python_shell=False)
                if ip_cmd['retcode'] == 0:
                    ip_data = \
                        salt.utils.network._interfaces_ifconfig(
                            ip_cmd['stdout'])
                else:
                    # Neither was successful, give up
                    log.warning(
                        'Unable to run ip or ifconfig in container \'{0}\''
                        .format(name)
                    )
                    ip_data = {}

            ret['ipv4_ips'] = salt.utils.network.ip_addrs(
                include_loopback=True,
                interface_data=ip_data
            )
            ret['ipv6_ips'] = salt.utils.network.ip_addrs6(
                include_loopback=True,
                interface_data=ip_data
            )
            ret['ips'] = ret['ipv4_ips'] + ret['ipv6_ips']
            for address in ret['ipv4_ips']:
                if address == '127.0.0.1':
                    ret['private_ips'].append(address)
                    ret['private_ipv4_ips'].append(address)
                elif salt.utils.cloud.is_public_ip(address):
                    ret['public_ips'].append(address)
                    ret['public_ipv4_ips'].append(address)
                else:
                    ret['private_ips'].append(address)
                    ret['private_ipv4_ips'].append(address)
            for address in ret['ipv6_ips']:
                if address == '::1' or address.startswith('fe80'):
                    ret['private_ips'].append(address)
                    ret['private_ipv6_ips'].append(address)
                else:
                    ret['public_ips'].append(address)
                    ret['public_ipv6_ips'].append(address)

        for key in [x for x in ret if x == 'ips' or x.endswith('ips')]:
            ret[key].sort(key=_ip_sort)
        __context__['lxc.info.{0}'.format(name)] = ret
    return __context__['lxc.info.{0}'.format(name)]


def set_password(name, users, password, encrypted=True):
    '''
    .. versionchanged:: 2015.2.0
        Function renamed from ``set_pass`` to ``set_password``. Additionally,
        this function now supports (and defaults to using) a password hash
        instead of a plaintext password.

    Set the password of one or more system users inside containers


    users
        Comma-separated list (or python list) of users to change password

    password
        Password to set for the specified user(s)

    encrypted : True
        If true, ``password`` must be a password hash. Set to ``False`` to set
        a plaintext password (not recommended).

        .. versionadded:: 2015.2.0

    CLI Example:

    .. code-block:: bash

        salt '*' lxc.set_pass container-name root '$6$uJ2uAyLU$KoI67t8As/0fXtJOPcHKGXmUpcoYUcVR2K6x93walnShTCQvjRwq25yIkiCBOqgbfdKQSFnAo28/ek6716vEV1'
        salt '*' lxc.set_pass container-name root foo encrypted=False

    '''
    def _bad_user_input():
        raise SaltInvocationError('Invalid input for \'users\' parameter')

    if not isinstance(users, list):
        try:
            users = users.split(',')
        except AttributeError:
            _bad_user_input()
    if not users:
        _bad_user_input()

    failed_users = []
    for user in users:
        result = retcode(name,
                         'chpasswd{0}'.format(' -e' if encrypted else ''),
                         stdin=':'.join((user, password)),
                         python_shell=False,
                         output_loglevel='quiet')
        if result != 0:
            failed_users.append(user)
    if failed_users:
        raise CommandExecutionError(
            'Password change failed for the following user(s): {0}'
            .format(', '.join(failed_users))
        )
    return True

set_pass = set_password


def update_lxc_conf(name, lxc_conf, lxc_conf_unset):
    '''
    Edit LXC configuration options

    CLI Example:

    .. code-block:: bash

        salt myminion lxc.update_lxc_conf ubuntu \\
                lxc_conf="[{'network.ipv4.ip':'10.0.3.5'}]" \\
                lxc_conf_unset="['lxc.utsname']"

    '''
    _ensure_exists(name)
    lxc_conf_p = '/var/lib/lxc/{0}/config'.format(name)
    if not os.path.exists(lxc_conf_p):
        raise SaltInvocationError(
            'Configuration file {0} does not exist'.format(lxc_conf_p)
        )

    changes = {'edited': [], 'added': [], 'removed': []}
    ret = {'changes': changes, 'result': True, 'comment': ''}

    # do not use salt.utils.fopen !
    with open(lxc_conf_p, 'r') as fic:
        filtered_lxc_conf = []
        for row in lxc_conf:
            if not row:
                continue
            for conf in row:
                filtered_lxc_conf.append((conf.strip(),
                                          row[conf].strip()))
        ret['comment'] = 'lxc.conf is up to date'
        lines = []
        orig_config = fic.read()
        for line in orig_config.splitlines():
            if line.startswith('#') or not line.strip():
                lines.append([line, ''])
            else:
                line = line.split('=')
                index = line.pop(0)
                val = (index.strip(), '='.join(line).strip())
                if val not in lines:
                    lines.append(val)
        for key, item in filtered_lxc_conf:
            matched = False
            for idx, line in enumerate(lines[:]):
                if line[0] == key:
                    matched = True
                    lines[idx] = (key, item)
                    if '='.join(line[1:]).strip() != item.strip():
                        changes['edited'].append(
                            ({line[0]: line[1:]}, {key: item}))
                        break
            if not matched:
                if not (key, item) in lines:
                    lines.append((key, item))
                changes['added'].append({key: item})
        dest_lxc_conf = []
        # filter unset
        if lxc_conf_unset:
            for line in lines:
                for opt in lxc_conf_unset:
                    if (
                        not line[0].startswith(opt)
                        and line not in dest_lxc_conf
                    ):
                        dest_lxc_conf.append(line)
                    else:
                        changes['removed'].append(opt)
        else:
            dest_lxc_conf = lines
        conf = ''
        for key, val in dest_lxc_conf:
            if not val:
                conf += '{0}\n'.format(key)
            else:
                conf += '{0} = {1}\n'.format(key.strip(), val.strip())
        conf_changed = conf != orig_config
        chrono = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        if conf_changed:
            # DO NOT USE salt.utils.fopen here, i got (kiorky)
            # problems with lxc configs which were wiped !
            with open('{0}.{1}'.format(lxc_conf_p, chrono), 'w') as wfic:
                wfic.write(conf)
            with open(lxc_conf_p, 'w') as wfic:
                wfic.write(conf)
            ret['comment'] = 'Updated'
            ret['result'] = True

    if not any(changes[x] for x in changes):
        # Ensure an empty changes dict if nothing was modified
        ret['changes'] = {}
    return ret


def set_dns(name, dnsservers=None, searchdomains=None):
    '''
    .. versionchanged:: 2015.2.0
        The ``dnsservers`` and ``searchdomains`` parameters can now be passed
        as a comma-separated list.

    Update /etc/resolv.conf

    CLI Example:

    .. code-block:: bash

        salt myminion lxc.set_dns ubuntu "['8.8.8.8', '4.4.4.4']"

    '''
    ret = {'result': False}
    if dnsservers is None:
        dnsservers = ['8.8.8.8', '4.4.4.4']
    elif not isinstance(dnsservers, list):
        try:
            dnsservers = dnsservers.split(',')
        except AttributeError:
            raise SaltInvocationError(
                'Invalid input for \'dnsservers\' parameter'
            )
    if searchdomains is None:
        searchdomains = []
    elif not isinstance(searchdomains, list):
        try:
            searchdomains = searchdomains.split(',')
        except AttributeError:
            raise SaltInvocationError(
                'Invalid input for \'searchdomains\' parameter'
            )
    dns = ['nameserver {0}'.format(x) for x in dnsservers]
    dns.extend(['search {0}'.format(x) for x in searchdomains])
    dns = '\n'.join(dns) + '\n'
    result = run_all(name,
                     'tee /etc/resolv.conf',
                     stdin=dns,
                     python_shell=False)
    if result['retcode'] != 0:
        error = ('Unable to write to /etc/resolv.conf in container \'{0}\''
                 .format(name))
        if result['stderr']:
            error += ': {0}'.format(result['stderr'])
        raise CommandExecutionError(error)
    return True


def _needs_install(name):
    ret = 0
    has_minion = retcode(name, "command -v salt-minion")
    # we assume that installing is when no minion is running
    # but testing the executable presence is not enougth for custom
    # installs where the bootstrap can do much more than installing
    # the bare salt binaries.
    if has_minion:
        processes = run_stdout(name, 'ps aux')
        if 'salt-minion' not in processes:
            ret = 1
        else:
            retcode(name, 'salt-call --local service.stop salt-minion')
    else:
        ret = 1
    return ret


def bootstrap(name,
              config=None,
              approve_key=True,
              install=True,
              pub_key=None,
              priv_key=None,
              bootstrap_url=None,
              force_install=False,
              unconditional_install=False,
              bootstrap_delay=None,
              bootstrap_args=None,
              bootstrap_shell=None):
    '''
    Install and configure salt in a container.

    config
        Minion configuration options. By default, the ``master`` option is set
        to the target host's master.

    approve_key
        Request a pre-approval of the generated minion key. Requires
        that the salt-master be configured to either auto-accept all keys or
        expect a signing request from the target host. Default: ``True``

    pub_key
        Explicit public key to pressed the minion with (optional).
        This can be either a filepath or a string representing the key

    priv_key
        Explicit private key to pressed the minion with (optional).
        This can be either a filepath or a string representing the key

    bootstrap_delay
        Delay in seconds between end of container creation and bootstrapping.
        Useful when waiting for container to obtain a DHCP lease.

        .. versionadded:: 2015.2.0

    bootstrap_url
        url, content or filepath to the salt bootstrap script

    bootstrap_args
        salt bootstrap script arguments

    bootstrap_shell
        shell to execute the script into

    install
        Whether to attempt a full installation of salt-minion if needed.

    force_install
        Force installation even if salt-minion is detected,
        this is the way to run vendor bootstrap scripts even
        if a salt minion is already present in the container

    unconditional_install
        Run the script even if the container seems seeded

    CLI Examples:

    .. code-block:: bash

        salt 'minion' lxc.bootstrap container_name [config=config_data] \\
                [approve_key=(True|False)] [install=(True|False)]

    '''
    if bootstrap_delay is not None:
        try:
            time.sleep(bootstrap_delay)
        except TypeError:
            # Bad input, but assume since a value was passed that
            # a delay was desired, and sleep for 5 seconds
            time.sleep(5)

    c_info = info(name)
    if not c_info:
        return None

    # default set here as we cannot set them
    # in def as it can come from a chain of procedures.
    if bootstrap_args:
        # custom bootstrap args can be totally customized, and user could
        # have inserted the placeholder for the config directory.
        # For example, some salt bootstrap script do not use at all -c
        if '{0}' not in bootstrap_args:
            bootstrap_args += ' -c {0}'
    else:
        bootstrap_args = '-c {0}'
    if not bootstrap_shell:
        bootstrap_shell = 'sh'

    orig_state = _ensure_running(name)
    if not orig_state:
        return orig_state
    if not force_install:
        needs_install = _needs_install(name)
    else:
        needs_install = True
    seeded = retcode(name, 'test -e \'{0}\''.format(SEED_MARKER)) == 0
    tmp = tempfile.mkdtemp()
    if seeded and not unconditional_install:
        ret = True
    else:
        ret = False
        cfg_files = __salt__['seed.mkconfig'](
            config, tmp=tmp, id_=name, approve_key=approve_key,
            pub_key=pub_key, priv_key=priv_key)
        if needs_install or force_install or unconditional_install:
            if install:
                rstr = __salt__['test.rand_str']()
                configdir = '/tmp/.c_{0}'.format(rstr)
                run(name,
                    'install -m 0700 -d {0}'.format(configdir),
                    python_shell=False)
                bs_ = __salt__['config.gather_bootstrap_script'](
                    bootstrap=bootstrap_url)
                dest_dir = os.path.join('/tmp', rstr)
                for cmd in [
                    'mkdir -p {0}'.format(dest_dir),
                    'chmod 700 {0}'.format(dest_dir),
                ]:
                    if run_stdout(name, cmd):
                        log.error(
                            ('tmpdir {0} creation'
                             ' failed ({1}').format(dest_dir, cmd))
                        return False
                copy_to(name,
                        bs_,
                        '{0}/bootstrap.sh'.format(dest_dir))
                copy_to(name, cfg_files['config'],
                        os.path.join(configdir, 'minion'))
                copy_to(name, cfg_files['privkey'],
                        os.path.join(configdir, 'minion.pem'))
                copy_to(name, cfg_files['pubkey'],
                        os.path.join(configdir, 'minion.pub'))
                bootstrap_args = bootstrap_args.format(configdir)
                cmd = ('{0} {2}/bootstrap.sh {1}'
                       .format(bootstrap_shell,
                               bootstrap_args.replace("'", "''"),
                               dest_dir))
                # log ASAP the forged bootstrap command which can be wrapped
                # out of the output in case of unexpected problem
                log.info('Running {0} in LXC container \'{1}\''
                         .format(cmd, name))
                ret = retcode(name, cmd, output_loglevel='info',
                                  use_vt=True) == 0
            else:
                ret = False
        else:
            minion_config = salt.config.minion_config(cfg_files['config'])
            pki_dir = minion_config['pki_dir']
            copy_to(name, cfg_files['config'], '/etc/salt/minion')
            copy_to(name,
                    cfg_files['privkey'],
                    os.path.join(pki_dir, 'minion.pem'))
            copy_to(name,
                    cfg_files['pubkey'],
                    os.path.join(pki_dir, 'minion.pub'))
            run(name,
                'salt-call --local service.enable salt-minion',
                python_shell=False)
            ret = True
        shutil.rmtree(tmp)
        if orig_state == 'stopped':
            stop(name)
        elif orig_state == 'frozen':
            freeze(name)
        # mark seeded upon successful install
        if ret:
            run(name,
                    'touch \'{0}\''.format(SEED_MARKER),
                    python_shell=False)
    return ret


def attachable(name):
    '''
    Return True if the named container can be attached to via the lxc-attach
    command

    CLI Example:

    .. code-block:: bash

        salt 'minion' lxc.attachable ubuntu
    '''
    try:
        return __context__['lxc.attachable']
    except KeyError:
        _ensure_exists(name)
        # Can't use run() here because it uses attachable() and would
        # endlessly recurse, resulting in a traceback
        cmd = 'lxc-attach --clear-env -n {0} -- /usr/bin/env'.format(name)
        result = __salt__['cmd.retcode'](cmd, python_shell=False) == 0
        __context__['lxc.attachable'] = result
    return __context__['lxc.attachable']


def _run(name,
         cmd,
         output=None,
         no_start=False,
         preserve_state=True,
         stdin=None,
         python_shell=True,
         output_loglevel='debug',
         use_vt=False,
         ignore_retcode=False,
         keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    Common logic for lxc.run functions
    '''
    orig_state = state(name)
    try:
        if attachable(name):
            ret = __salt__['container_resource.run'](
                name,
                cmd,
                container_type=__virtualname__,
                exec_driver=EXEC_DRIVER,
                output=output,
                no_start=no_start,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                ignore_retcode=ignore_retcode,
                use_vt=use_vt,
                keep_env=keep_env)
        else:
            rootfs = info(name).get('rootfs')
            # Set context var to make cmd.run_chroot run cmd.run instead of
            # cmd.run_all.
            __context__['cmd.run_chroot.func'] = __salt__['cmd.run']
            ret = __salt__['cmd.run_chroot'](rootfs,
                                             cmd,
                                             stdin=stdin,
                                             python_shell=python_shell,
                                             output_loglevel=output_loglevel,
                                             ignore_retcode=ignore_retcode)
    except Exception:
        raise
    finally:
        # Make sure we honor preserve_state, even if there was an exception
        new_state = state(name)
        if preserve_state:
            if orig_state == 'stopped' and new_state != 'stopped':
                stop(name)
            elif orig_state == 'frozen' and new_state != 'frozen':
                freeze(name, start=True)

    if output in (None, 'all'):
        return ret
    else:
        return ret[output]


def run_cmd(name,
            cmd,
            no_start=False,
            preserve_state=True,
            stdin=None,
            stdout=True,
            stderr=False,
            python_shell=True,
            output_loglevel='debug',
            use_vt=False,
            ignore_retcode=False,
            keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    .. deprecated:: 2015.2.0
        Use :mod:`lxc.run <salt.modules.lxc.run>` instead
    '''
    salt.utils.warn_until(
        'Boron',
        'lxc.run_cmd has been deprecated, please use one of the lxc.run '
        'functions (or lxc.retcode). See the documentation for more '
        'information.'
    )
    if stdout and stderr:
        output = 'all'
    elif stdout:
        output = 'stdout'
    elif stderr:
        output = 'stderr'
    else:
        output = 'retcode'
    return _run(name,
                cmd,
                output=output,
                no_start=no_start,
                preserve_state=preserve_state,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                use_vt=use_vt,
                ignore_retcode=ignore_retcode,
                keep_env=keep_env)


def run(name,
        cmd,
        no_start=False,
        preserve_state=True,
        stdin=None,
        python_shell=True,
        output_loglevel='debug',
        use_vt=False,
        ignore_retcode=False,
        keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    .. versionadded:: 2015.2.0

    Run :mod:`cmd.run <salt.modules.cmdmod.run>` within a container

    .. warning::

        Many shell builtins do not work, failing with stderr similar to the
        following:

        .. code-block:: bash

            lxc_container: No such file or directory - failed to exec 'command'

        The same error will be displayed in stderr if the command being run
        does not exist. If no output is returned using this function, try using
        :mod:`lxc.run_stderr <salt.modules.lxc.run_stderr>` or
        :mod:`lxc.run_all <salt.modules.lxc.run_all>`.

    name
        Name of the container in which to run the command

    cmd
        Command to run

    no_start : False
        If the container is not running, don't start it

    preserve_state : True
        After running the command, return the container to its previous state

    stdin : None
        Standard input to be used for the command

    output_loglevel : debug
        Level at which to log the output from the command. Set to ``quiet`` to
        suppress logging.

    use_vt : False
        Use SaltStack's utils.vt to stream output to console. Assumes
        ``output=all``.

    keep_env : http_proxy,https_proxy,no_proxy
        A list of env vars to preserve. May be passed as commma-delimited list.


    CLI Example:

    .. code-block:: bash

        salt myminion lxc.run mycontainer 'ifconfig -a'
    '''
    return _run(name,
                cmd,
                output=None,
                no_start=no_start,
                preserve_state=preserve_state,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                use_vt=use_vt,
                ignore_retcode=ignore_retcode,
                keep_env=keep_env)


def run_stdout(name,
               cmd,
               no_start=False,
               preserve_state=True,
               stdin=None,
               python_shell=True,
               output_loglevel='debug',
               use_vt=False,
               ignore_retcode=False,
               keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    .. versionadded:: 2015.2.0

    Run :mod:`cmd.run_stdout <salt.modules.cmdmod.run_stdout>` within a container

    .. warning::

        Many shell builtins do not work, failing with stderr similar to the
        following:

        .. code-block:: bash

            lxc_container: No such file or directory - failed to exec 'command'

        The same error will be displayed in stderr if the command being run
        does not exist. If no output is returned using this function, try using
        :mod:`lxc.run_stderr <salt.modules.lxc.run_stderr>` or
        :mod:`lxc.run_all <salt.modules.lxc.run_all>`.

    name
        Name of the container in which to run the command

    cmd
        Command to run

    no_start : False
        If the container is not running, don't start it

    preserve_state : True
        After running the command, return the container to its previous state

    stdin : None
        Standard input to be used for the command

    output_loglevel : debug
        Level at which to log the output from the command. Set to ``quiet`` to
        suppress logging.

    use_vt : False
        Use SaltStack's utils.vt to stream output to console
        ``output=all``.

    keep_env : http_proxy,https_proxy,no_proxy
        A list of env vars to preserve. May be passed as commma-delimited list.


    CLI Example:

    .. code-block:: bash

        salt myminion lxc.run_stdout mycontainer 'ifconfig -a'
    '''
    return _run(name,
                cmd,
                output='stdout',
                no_start=no_start,
                preserve_state=preserve_state,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                use_vt=use_vt,
                ignore_retcode=ignore_retcode,
                keep_env=keep_env)


def run_stderr(name,
               cmd,
               no_start=False,
               preserve_state=True,
               stdin=None,
               python_shell=True,
               output_loglevel='debug',
               use_vt=False,
               ignore_retcode=False,
               keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    .. versionadded:: 2015.2.0

    Run :mod:`cmd.run_stderr <salt.modules.cmdmod.run_stderr>` within a container

    .. warning::

        Many shell builtins do not work, failing with stderr similar to the
        following:

        .. code-block:: bash

            lxc_container: No such file or directory - failed to exec 'command'

        The same error will be displayed if the command being run does not
        exist.

    name
        Name of the container in which to run the command

    cmd
        Command to run

    no_start : False
        If the container is not running, don't start it

    preserve_state : True
        After running the command, return the container to its previous state

    stdin : None
        Standard input to be used for the command

    output_loglevel : debug
        Level at which to log the output from the command. Set to ``quiet`` to
        suppress logging.

    use_vt : False
        Use SaltStack's utils.vt to stream output to console
        ``output=all``.

    keep_env : http_proxy,https_proxy,no_proxy
        A list of env vars to preserve. May be passed as commma-delimited list.


    CLI Example:

    .. code-block:: bash

        salt myminion lxc.run_stderr mycontainer 'ip addr show'
    '''
    return _run(name,
                cmd,
                output='stderr',
                no_start=no_start,
                preserve_state=preserve_state,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                use_vt=use_vt,
                ignore_retcode=ignore_retcode,
                keep_env=keep_env)


def retcode(name,
                cmd,
                no_start=False,
                preserve_state=True,
                stdin=None,
                python_shell=True,
                output_loglevel='debug',
                use_vt=False,
                ignore_retcode=False,
                keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    .. versionadded:: 2015.2.0

    Run :mod:`cmd.retcode <salt.modules.cmdmod.retcode>` within a container

    .. warning::

        Many shell builtins do not work, failing with stderr similar to the
        following:

        .. code-block:: bash

            lxc_container: No such file or directory - failed to exec 'command'

        The same error will be displayed in stderr if the command being run
        does not exist. If the retcode is nonzero and not what was expected,
        try using :mod:`lxc.run_stderr <salt.modules.lxc.run_stderr>`
        or :mod:`lxc.run_all <salt.modules.lxc.run_all>`.

    name
        Name of the container in which to run the command

    cmd
        Command to run

    no_start : False
        If the container is not running, don't start it

    preserve_state : True
        After running the command, return the container to its previous state

    stdin : None
        Standard input to be used for the command

    output_loglevel : debug
        Level at which to log the output from the command. Set to ``quiet`` to
        suppress logging.

    use_vt : False
        Use SaltStack's utils.vt to stream output to console
        ``output=all``.

    keep_env : http_proxy,https_proxy,no_proxy
        A list of env vars to preserve. May be passed as commma-delimited list.


    CLI Example:

    .. code-block:: bash

        salt myminion lxc.retcode mycontainer 'ip addr show'
    '''
    return _run(name,
                cmd,
                output='retcode',
                no_start=no_start,
                preserve_state=preserve_state,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                use_vt=use_vt,
                ignore_retcode=ignore_retcode,
                keep_env=keep_env)


def run_all(name,
            cmd,
            no_start=False,
            preserve_state=True,
            stdin=None,
            python_shell=True,
            output_loglevel='debug',
            use_vt=False,
            ignore_retcode=False,
            keep_env='http_proxy,https_proxy,no_proxy'):
    '''
    .. versionadded:: 2015.2.0

    Run :mod:`cmd.run_all <salt.modules.cmdmod.run_all>` within a container

    .. note::

        While the command is run within the container, it is initiated from the
        host. Therefore, the PID in the return dict is from the host, not from
        the container.

    .. warning::

        Many shell builtins do not work, failing with stderr similar to the
        following:

        .. code-block:: bash

            lxc_container: No such file or directory - failed to exec 'command'

        The same error will be displayed in stderr if the command being run
        does not exist.

    name
        Name of the container in which to run the command

    cmd
        Command to run

    no_start : False
        If the container is not running, don't start it

    preserve_state : True
        After running the command, return the container to its previous state

    stdin : None
        Standard input to be used for the command

    output_loglevel : debug
        Level at which to log the output from the command. Set to ``quiet`` to
        suppress logging.

    use_vt : False
        Use SaltStack's utils.vt to stream output to console
        ``output=all``.

    keep_env : http_proxy,https_proxy,no_proxy
        A list of env vars to preserve. May be passed as commma-delimited list.


    CLI Example:

    .. code-block:: bash

        salt myminion lxc.run_all mycontainer 'ip addr show'
    '''
    return _run(name,
                cmd,
                output='all',
                no_start=no_start,
                preserve_state=preserve_state,
                stdin=stdin,
                python_shell=python_shell,
                output_loglevel=output_loglevel,
                use_vt=use_vt,
                ignore_retcode=ignore_retcode,
                keep_env=keep_env)


def _get_md5(name, path):
    '''
    Get the MD5 checksum of a file from a container
    '''
    output = run_stdout(name, 'md5sum "{0}"'.format(path), ignore_retcode=True)
    try:
        return output.split()[0]
    except IndexError:
        # Destination file does not exist or could not be accessed
        return None


def copy_to(name, source, dest, overwrite=False, makedirs=False):
    '''
    .. versionchanged:: Beryllium
        Function renamed from ``lxc.cp`` to ``lxc.copy_to`` for consistency
        with other container types. ``lxc.cp`` will continue to work, however.
        For versions 2015.2.x and earlier, use ``lxc.cp``.

    Copy a file or directory from the host into a container

    name
        Container name

    source
        File to be copied to the container

    dest
        Destination on the container. Must be an absolute path.

        .. versionchanged:: 2015.2.0
            If the destination is a directory, the file will be copied into
            that directory.

    overwrite : False
        Unless this option is set to ``True``, then if a file exists at the
        location specified by the ``dest`` argument, an error will be raised.

        .. versionadded:: Beryllium

    makedirs : False

        Create the parent directory on the container if it does not already
        exist.

        .. versionadded:: 2015.2.0

    CLI Example:

    .. code-block:: bash

        salt 'minion' lxc.copy_to /tmp/foo /root/foo
        salt 'minion' lxc.cp /tmp/foo /root/foo
    '''
    _ensure_running(name, no_start=True)
    return __salt__['container_resource.copy_to'](
        name,
        source,
        dest,
        container_type=__virtualname__,
        exec_driver=EXEC_DRIVER,
        overwrite=overwrite,
        makedirs=makedirs)

cp = copy_to


def read_conf(conf_file, out_format='simple'):
    '''
    Read in an LXC configuration file. By default returns a simple, unsorted
    dict, but can also return a more detailed structure including blank lines
    and comments.

        out_format:
            set to 'simple' if you need the old and unsupported behavior.
            This wont support the multiple lxc values (eg: multiple network nics)

    CLI Examples:

    .. code-block:: bash

        salt 'minion' lxc.read_conf /etc/lxc/mycontainer.conf
        salt 'minion' lxc.read_conf /etc/lxc/mycontainer.conf out_format=commented
    '''
    ret_commented = []
    ret_simple = {}
    with salt.utils.fopen(conf_file, 'r') as fp_:
        for line in fp_.readlines():
            if '=' not in line:
                ret_commented.append(line)
                continue
            comps = line.split('=')
            value = '='.join(comps[1:]).strip()
            comment = None
            if value.strip().startswith('#'):
                vcomps = value.strip().split('#')
                value = vcomps[1].strip()
                comment = '#'.join(vcomps[1:]).strip()
                ret_commented.append({comps[0].strip(): {
                    'value': value,
                    'comment': comment,
                }})
            else:
                ret_commented.append({comps[0].strip(): value})
                ret_simple[comps[0].strip()] = value

    if out_format == 'simple':
        return ret_simple
    return ret_commented


def write_conf(conf_file, conf):
    '''
    Write out an LXC configuration file

    This is normally only used internally. The format of the data structure
    must match that which is returned from ``lxc.read_conf()``, with
    ``out_format`` set to ``commented``.

    An example might look like:

    .. code-block:: python

        [
            {'lxc.utsname': '$CONTAINER_NAME'},
            '# This is a commented line\\n',
            '\\n',
            {'lxc.mount': '$CONTAINER_FSTAB'},
            {'lxc.rootfs': {'comment': 'This is another test',
                            'value': 'This is another test'}},
            '\\n',
            {'lxc.network.type': 'veth'},
            {'lxc.network.flags': 'up'},
            {'lxc.network.link': 'br0'},
            {'lxc.network.mac': '$CONTAINER_MACADDR'},
            {'lxc.network.ipv4': '$CONTAINER_IPADDR'},
            {'lxc.network.name': '$CONTAINER_DEVICENAME'},
        ]

    CLI Example:

    .. code-block:: bash

        salt 'minion' lxc.write_conf /etc/lxc/mycontainer.conf \\
            out_format=commented
    '''
    if not isinstance(conf, list):
        raise SaltInvocationError('Configuration must be passed as a list')

    # construct the content prior to write to the file
    # to avoid half written configs
    content = ''
    for line in conf:
        if isinstance(line, (six.text_type, six.string_types)):
            content += line
        elif isinstance(line, dict):
            for key in list(line.keys()):
                out_line = None
                if isinstance(
                    line[key],
                    (six.text_type, six.string_types, six.integer_types, float)
                ):
                    out_line = ' = '.join((key, "{0}".format(line[key])))
                elif isinstance(line[key], dict):
                    out_line = ' = '.join((key, line[key]['value']))
                    if 'comment' in line[key]:
                        out_line = ' # '.join((out_line, line[key]['comment']))
                if out_line:
                    content += out_line
                    content += '\n'
    with salt.utils.fopen(conf_file, 'w') as fp_:
        fp_.write(content)
    return {}


def edit_conf(conf_file, out_format='simple', read_only=False, lxc_config=None, **kwargs):
    '''
    Edit an LXC configuration file. If a setting is already present inside the
    file, its value will be replaced. If it does not exist, it will be appended
    to the end of the file. Comments and blank lines will be kept in-tact if
    they already exist in the file.

    out_format:
        Set to simple if you need backward compatbility (multiple items for a
        simple key is not supported)
    read_only:
        return only the edited configuration without applying it
        to the underlying lxc configuration file
    lxc_config:
        List of dict containning lxc configuration items
        For network configuration, you also need to add the device it belongs
        to, otherwise it will default to eth0.
        Also, any change to a network parameter will result in the whole
        network reconfiguration to avoid mismatchs, be aware of that !

    After the file is edited, its contents will be returned. By default, it
    will be returned in ``simple`` format, meaning an unordered dict (which
    may not represent the actual file order). Passing in an ``out_format`` of
    ``commented`` will return a data structure which accurately represents the
    order and content of the file.

    CLI Example:

    .. code-block:: bash

        salt 'minion' lxc.edit_conf /etc/lxc/mycontainer.conf \\
            out_format=commented lxc.network.type=veth
        salt 'minion' lxc.edit_conf /etc/lxc/mycontainer.conf \\
            out_format=commented \\
            lxc_config="[{'lxc.network.name': 'eth0', \\
                          'lxc.network.ipv4': '1.2.3.4'},
                         {'lxc.network.name': 'eth2', \\
                          'lxc.network.ipv4': '1.2.3.5',\\
                          'lxc.network.gateway': '1.2.3.1'}]"
    '''
    data = []

    try:
        conf = read_conf(conf_file, out_format=out_format)
    except Exception:
        conf = []

    if not lxc_config:
        lxc_config = []
    lxc_config = copy.deepcopy(lxc_config)

    # search if we want to access net config
    # in that case, we will replace all the net configuration
    net_config = []
    for lxc_kws in lxc_config + [kwargs]:
        net_params = {}
        for kwarg in [a for a in lxc_kws]:
            if kwarg.startswith('__'):
                continue
            if kwarg.startswith('lxc.network.'):
                net_params[kwarg] = lxc_kws[kwarg]
                lxc_kws.pop(kwarg, None)
        if net_params:
            net_config.append(net_params)
    nic_opts = salt.utils.odict.OrderedDict()
    for params in net_config:
        dev = params.get('lxc.network.name', 'eth0')
        dev_opts = nic_opts.setdefault(dev, salt.utils.odict.OrderedDict())
        for param in params:
            opt = param.replace('lxc.network.', '')
            opt = {'hwaddr': 'mac'}.get(opt, opt)
            dev_opts[opt] = params[param]

    net_changes = []
    if nic_opts:
        net_changes = _config_list(conf, only_net=True,
                                   **{'network_profile': DEFAULT_NIC,
                                      'nic_opts': nic_opts})
        if net_changes:
            lxc_config.extend(net_changes)

    for line in conf:
        if not isinstance(line, dict):
            data.append(line)
            continue
        else:
            for key in list(line.keys()):
                val = line[key]
                if net_changes and key.startswith('lxc.network.'):
                    continue
                found = False
                for ix in range(len(lxc_config)):
                    kw = lxc_config[ix]
                    if key in kw:
                        found = True
                        data.append({key: kw[key]})
                        del kw[key]
                if not found:
                    data.append({key: val})

    for lxc_kws in lxc_config:
        for kwarg in lxc_kws:
            data.append({kwarg: lxc_kws[kwarg]})
    if read_only:
        return data
    write_conf(conf_file, data)
    return read_conf(conf_file, out_format)


def reboot(name):
    '''
    Reboot a container.

    CLI Examples:

    .. code-block:: bash

        salt 'minion' lxc.reboot myvm

    '''
    ret = {'result': True,
           'changes': {},
           'comment': '{0} rebooted'.format(name)}
    does_exist = exists(name)
    if does_exist and (state(name) == 'running'):
        try:
            stop(name)
        except (SaltInvocationError, CommandExecutionError) as exc:
            ret['comment'] = 'Unable to stop container: {0}'.format(exc)
            ret['result'] = False
            return ret
    if does_exist and (state(name) != 'running'):
        try:
            start(name)
        except (SaltInvocationError, CommandExecutionError) as exc:
            ret['comment'] = 'Unable to stop container: {0}'.format(exc)
            ret['result'] = False
            return ret
    ret['changes'][name] = 'rebooted'
    return ret


def reconfigure(name,
                cpu=None,
                cpuset=None,
                cpushare=None,
                memory=None,
                profile=None,
                network_profile=None,
                nic_opts=None,
                bridge=None,
                gateway=None,
                autostart=None,
                **kwargs):
    '''
    Reconfigure a container.

    This only applies to a few property

    name
        Name of the container.
    cpu
        Select a random number of cpu cores and assign it to the cpuset, if the
        cpuset option is set then this option will be ignored
    cpuset
        Explicitly define the cpus this container will be bound to
    cpushare
        cgroups cpu shares.
    autostart
        autostart container on reboot
    memory
        cgroups memory limit, in MB.
        (0 for nolimit, None for old default 1024MB)
    gateway
        the ipv4 gateway to use
        the default does nothing more than lxcutils does
    bridge
        the bridge to use
        the default does nothing more than lxcutils does
    nic
        Network interfaces profile (defined in config or pillar).

    nic_opts
        Extra options for network interfaces, will override

        ``{"eth0": {"mac": "aa:bb:cc:dd:ee:ff", "ipv4": "10.1.1.1", "ipv6": "2001:db8::ff00:42:8329"}}``

        or

        ``{"eth0": {"mac": "aa:bb:cc:dd:ee:ff", "ipv4": "10.1.1.1/24", "ipv6": "2001:db8::ff00:42:8329"}}``

    CLI Example:

    .. code-block:: bash

        salt-call -lall mc_lxc_fork.reconfigure foobar nic_opts="{'eth1': {'mac': '00:16:3e:dd:ee:44'}}" memory=4

    '''
    changes = {}
    ret = {'name': name,
           'comment': 'config for {0} up to date'.format(name),
           'result': True,
           'changes': changes}
    profile = get_container_profile(copy.deepcopy(profile))
    kw_overrides = copy.deepcopy(kwargs)

    def select(key, default=None):
        kw_overrides_match = kw_overrides.pop(key, _marker)
        profile_match = profile.pop(key, default)
        # let kwarg overrides be the preferred choice
        if kw_overrides_match is _marker:
            return profile_match
        return kw_overrides_match
    if nic_opts is not None and not network_profile:
        network_profile = 'eth0'

    if autostart is not None:
        autostart = select('autostart', autostart)
    else:
        autostart = 'keep'
    path = os.path.join('/var/lib/lxc/{0}/config'.format(name))
    if os.path.exists(path):
        old_chunks = read_conf(path, out_format='commented')
        make_kw = salt.utils.odict.OrderedDict([
            ('autostart', autostart),
            ('cpu', cpu),
            ('gateway', gateway),
            ('cpuset', cpuset),
            ('cpushare', cpushare),
            ('network_profile', network_profile),
            ('nic_opts', nic_opts),
            ('bridge', bridge)])
        # match 0 and none as memory = 0 in lxc config is harmful
        if memory:
            make_kw['memory'] = memory
        kw = salt.utils.odict.OrderedDict()
        for key, val in six.iteritems(make_kw):
            if val is not None:
                kw[key] = val
        new_cfg = _config_list(conf_tuples=old_chunks, **kw)
        if new_cfg:
            edit_conf(path, out_format='commented', lxc_config=new_cfg)
        chunks = read_conf(path, out_format='commented')
        if old_chunks != chunks:
            ret['comment'] = '{0} lxc config updated'.format(name)
            if state(name) == 'running':
                cret = reboot(name)
                ret['result'] = cret['result']
    return ret


def apply_network_profile(name, network_profile, nic_opts=None):
    '''
    .. versionadded:: 2015.2.0

    Apply a network profile to a container

    network_profile
        profile name or default values (dict)

    nic_opts
        values to override in defaults (dict)
        indexed by nic card names

    CLI Examples:

    .. code-block:: bash

        salt 'minion' lxc.apply_network_profile web1 centos
        salt 'minion' lxc.apply_network_profile web1 centos \\
                nic_opts="{'eth0': {'mac': 'xx:xx:xx:xx:xx:xx'}}"
        salt 'minion' lxc.apply_network_profile web1 \\
                "{'eth0': {'mac': 'xx:xx:xx:xx:xx:yy'}}"
                nic_opts="{'eth0': {'mac': 'xx:xx:xx:xx:xx:xx'}}"
    '''
    path = '/var/lib/lxc/{0}/config'.format(name)

    before = []
    with salt.utils.fopen(path, 'r') as fp_:
        for line in fp_:
            before.append(line)

    network_params = {}
    for param in _network_conf(
        network_profile=network_profile, nic_opts=nic_opts
    ):
        network_params.update(param)
    if network_params:
        edit_conf(path, out_format='commented', **network_params)

    after = []
    with salt.utils.fopen(path, 'r') as fp_:
        for line in fp_:
            after.append(line)

    diff = ''
    for line in difflib.unified_diff(before,
                                     after,
                                     fromfile='before',
                                     tofile='after'):
        diff += line
    return diff
