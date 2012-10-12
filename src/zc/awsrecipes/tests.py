##############################################################################
#
# Copyright (c) Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.0 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
from zope.testing import setupstack
from os.path import exists as exists_original
import boto.ec2.volume
import doctest
import manuel.capture
import manuel.doctest
import manuel.testing
import mock
import pprint
import StringIO
import subprocess
import sys
import traceback
import unittest
import zc.zk.testing

def side_effect(m, f=None):
    if f is None:
        return lambda f: side_effect(m, f)
    m.side_effect = f

def assert_(cond, mess='assertion failed'):
    if not cond:
        raise AssertionError(mess)

class Ob:

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return '%s(%s)' % (
            self.__class__.__name__, pprint.pformat(self.__dict__, width=1))

class Resource(Ob):

    def __init__(self, id, tags=None, **kw):
        self.id = id
        self.tags = tags or {}
        self.__dict__.update(kw)

class AttachmentSet(Ob):

    status = None

class Volume(Resource):

    attach_data = boto.ec2.volume.AttachmentSet()

    def attach(self, instance_id, device):
        assert_(self.attach_data.status is None, 'already attached')
        self.attach_data = AttachmentSet(
            status='attached', instance_id=instance_id, device=device)

class Instance(Resource):

    def update(self):
        return 'running'

class Reservation(Ob):
    pass

class Connection:

    def __init__(self, name):
        self.name = name
        self.volumes = []
        self.instances = []
        self.resources = {}
        self.security_groups = [
            Resource('gr1', vpc_id='vpc1', name='default'),
            Resource('gr2', vpc_id='vpc1', name='x'),
            Resource('gr3', vpc_id='vpc2', name='default'),
            Resource('gr4', vpc_id='vpc2', name='x'),
            ]
        self.subnets = [
            Resource('subnet-41', dict(scope='public'),  vpc_id='vpc1'),
            Resource('subnet-42', dict(scope='private'), vpc_id='vpc1'),
            Resource('subnet-43', dict(scope='public'),  vpc_id='vpc2'),
            Resource('subnet-44', dict(scope='private'), vpc_id='vpc2'),
            ]
        self.vpcs = [
            Resource(
                'vpc1',
                dict(Name='test_cluster', zone='us-up-1z'),
                region=self, connection=self)
            ]
        self.images = [
            Resource('ami-42', dict(Name='default'))
            ]

    def create_volume(self, size, zone):
        assert_(zone == 'us-up-1z')
        volume = Volume('vol%s' % len(self.volumes), size=size, zone=zone)
        self.volumes.append(volume)
        self.resources[volume.id] = volume
        return volume

    def create_tags(self, ids, tags):
        for id in ids:
            self.resources[id].tags.update(tags)

    def _get_all(self, obs, ids, filters):
        result = []
        for v in obs:
            if ids and v.id not in ids:
                continue

            bad = False
            for name in filters:
                if name.startswith('tag:'):
                    if v.tags.get(name[4:]) != filters[name]:
                        bad = True
                        break
                elif getattr(v, name) != filters[name]:
                    bad = True
                    break
            if not bad:
                result.append(v)
        return result

    def get_all_volumes(self, ids=None, filters={}):
        return self._get_all(self.volumes, ids, filters)

    def get_all_vpcs(self, ids=None, filters={}):
        return self._get_all(self.vpcs, ids, filters)

    def get_all_images(self, filters):
        return [Resource('ami-42')]

    def get_all_instances(self, ids=None, filters={}):
        return [Reservation(instances=[i])
                for i in self._get_all(self.instances, ids, filters)]

    def get_all_subnets(self, ids=None, filters={}):
        return self._get_all(self.subnets, ids, filters)

    def get_all_security_groups(self, ids=None, filters={}):
        return self._get_all(self.security_groups, ids, filters)

    def run_instances(self, security_group_ids, user_data, **kw):
        id = 'inst%s' % len(self.instances)
        instance = Instance(
            id,
            attrs=dict(
                userData=user_data,
                groupSet=[self.get_all_security_groups([name])[0]
                          for name in security_group_ids],
                ),
            )
        self.instances.append(instance)
        self.resources[instance.id] = instance
        return Resource('', instances=[instance])


class FauxPopen:

    def __init__(self, handler, command, stdout, stderr):
        if stdout is None:
            stdout = sys.stdout
        if stderr is subprocess.STDOUT:
            stderr = stdout
        elif stderr is None:
            stderr = sys.stdout
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = handler(command, self) or 0

    def wait(self):
        return self.returncode

class FauxVolumes:

    def __init__(self, test):
        self.init()
        test.globs['volumes'] = self
        setupstack.context_manager(
            test, mock.patch('os.path.exists', side_effect=self.exists))
        setupstack.context_manager(
            test, mock.patch('zc.awsrecipes.open', create=True,
                             side_effect=self.open))

        def Popen(command, stdout=None, stderr=None, shell=False):
            meth = getattr(self, command.split()[0].rsplit('/', 1)[1])
            return FauxPopen(meth, command, stdout, stderr)
        setupstack.context_manager(
            test, mock.patch('subprocess.Popen', side_effect=Popen))

    def init(self, sds=None, preexisting_mds=None, preexisting_vgs=None):
        self.sds = sds or [] # Set of attached sds: sdb1, sdb2, ...
        self.mds = {} # {mdname -> [sdname]}: md1 -> [sdb1, sdb2]
        self.vgs = {} # {vgname -> [mdname]}: vg_sdb -> [md1]
        self.lvs = {} # {vgname -> [mdname]}
                      # note may be fewer mds if not extended
        self.fss = {} # {vgname -> [mdname]}
                      # note may be fewer mds if not extended
        self.mounts = {}
        self.pvs = set()
        self.preexisting_mds = preexisting_mds or {}
        self.preexisting_vgs = preexisting_vgs or {}
        self.dirs = set()

    def terminate(self):
        self.init(self.sds, self.mds, self.vgs)

    def exists(self, name):
        if name.startswith('/dev/'):
            name = name[5:]
            return name in self.sds or name in self.mds
        return exists_original(name)

    def open(self, name):
        assert_(name=='/proc/mdstat')
        return StringIO.StringIO(self.mdstat())

    def lvcreate(self, command, p):
        args = command.split()
        assert_(args[1:5] == '-l +100%FREE -n data'.split())
        [vg] = args[5:]
        [md] = self.vgs[vg]
        assert_(vg not in self.lvs)
        self.lvs[vg] = [md]

    def lvextend(self, command, p):
        args = command.split()
        assert_(args[1:3] == '-l +100%FREE'.split())
        [lv] = args[3:]
        assert_(lv.startswith('/dev/'))
        assert_(lv.endswith('/data'))
        vg = lv[5:-5]
        assert_(vg in self.lvs)
        self.lvs[vg] = self.vgs[vg][:]

    mdstat_data = None
    def mdstat(self):
        if self.mdstat_data:
            return self.mdstat_data
        return '\n'.join(
            "%s : active raid10 %s" % (
                md, ' '.join("%s[0]" % sd for sd in data)
                )
            for md, data in sorted(self.mds.items())
            )+'\n'

    def mdadm(self, command, p):
        args = command.split()
        if command == '/sbin/mdadm --examine --scan >>/etc/mdadm.conf':
            self.examined_mds = self.preexisting_mds
        elif command == '/sbin/mdadm -A --scan':
            self.mds.update(self.examined_mds)
            self.examined_mds.clear()
        elif args[1:5] == '--create --metadata 1.2 -l10'.split():
            n, md = args[5:7]
            assert_(md.startswith('/dev/'))
            md = md[5:]
            sds = args[7:]
            assert_(md not in self.mds)
            assert_(n == ('-n%s' % len(sds)))
            assert_(len(set(sds)) == len(sds))
            assert_(not [sd for sd in sds if sd not in self.sds])
            self.mds[md] = sds
        else:
            assert_(0, "Unexpected command %r" % command)

    def mkdir(self, command, p):
        args = command.split()
        assert_(args[1] == '-p')
        [dir] = args[2:]
        self.dirs.add(dir)

    def mkfs(self, command, p):
        args = command.split()
        assert_(args[1:3] == '-t ext3'.split())
        [lv] = args[3:]
        assert_(lv.startswith('/dev/'))
        assert_(lv.endswith('/data'))
        vg = lv[5:-5]
        assert_(vg not in self.fss)
        self.fss[vg] = self.lvs[vg][:]

    def mount(self, command, p):
        args = command.split()
        assert_(args[1:3] == '-t ext3'.split())
        [lv, mp] = args[3:]
        assert_(lv.startswith('/dev/'))
        assert_(lv.endswith('/data'))
        vg = lv[5:-5]
        assert_(vg in self.fss)
        assert_(mp in self.dirs)
        assert_(mp not in self.mounts)
        self.mounts[mp] = vg

    def pvcreate(self, command, p):
        args = command.split()
        [vol] = args[1:]
        assert_(vol.startswith('/dev/'))
        vol = vol[5:]
        assert_(vol in self.mds and vol not in self.pvs)
        self.pvs.add(vol)

    def pvscan(self, command, p):
        args = command.split()
        assert_(not args[1:])
        for vg, mds in self.vgs.items():
            for md in mds:
                print >>p.stdout, (
                    '  PV /dev/%s   VG %s   lvm2 ' % (md, vg)
                    )

    def resize2fs(self, command, p):
        args = command.split()
        [lv] = args[1:]
        assert_(lv.startswith('/dev/'))
        assert_(lv.endswith('/data'))
        vg = lv[5:-5]
        assert_(vg in self.fss)
        self.fss[vg] = self.lvs[vg][:]

    def status(self):
        for dir, vg in sorted(self.mounts.items()):
            print vg, dir
            for md in sorted(self.fss[vg]):
                print '   ', md, self.mds[md]

    def vgchange(self, command, p):
        args = command.split()
        assert_(args[1:3] == '-a y'.split())
        [vg] = args[3:]
        assert_(vg not in self.vgs)
        assert_(vg not in self.lvs)
        assert_(vg not in self.fss)
        for md in self.preexisting_vgs[vg]:
            assert_(md not in self.pvs)
            self.pvs.add(md)
        self.vgs[vg] = self.preexisting_vgs[vg]
        self.lvs[vg] = self.vgs[vg][:]
        self.fss[vg] = self.vgs[vg][:]

    def vgcreate(self, command, p):
        args = command.split()
        vg, md = args[1:]
        assert_(md.startswith('/dev/'))
        md = md[5:]
        assert_(vg not in self.vgs)
        assert_((md in self.mds) and
                not [vg_ for vg_ in self.vgs if md in self.vgs[vg_]])
        self.vgs[vg] = [md]
        self.pvs.add(md)

    def vgextend(self, command, p):
        args = command.split()
        vg, md = args[1:]
        assert_(md.startswith('/dev/'))
        md = md[5:]
        assert_(md in self.pvs)
        self.vgs[vg].append(md)

    def vgscan(self, command, p):
        args = command.split()
        assert_(not args[1:])
        for vg in self.preexisting_vgs:
            print >>p.stdout, (
                '  Found volume group "%s" using metadata type lvm2'
                % vg)

def setup(test):
    connections = dict(test_region=Connection('test_region'))

    @side_effect(
        setupstack.context_manager(
            test, mock.patch('boto.ec2.connect_to_region')))
    def connect(region):
        return connections[region]

    @side_effect(
        setupstack.context_manager(
            test, mock.patch('boto.vpc.VPCConnection')))
    def vpcconnect(region):
        return connections[region.name]

    @side_effect(
        setupstack.context_manager(
            test, mock.patch('boto.ec2.get_region')))
    def get_region(region):
        return Ob(name=region)

    setupstack.context_manager(
        test, mock.patch(
            'pwd.getpwuid',
            side_effect=lambda uid: Ob(**dict(
                pw_name='testy',
                pw_gecos='Testy Tester',
                ))))

    setupstack.context_manager(test, mock.patch('time.sleep'))
    setupstack.context_manager(
        test,
        mock.patch('boto.ec2.regions', side_effect=connections.values))

    zc.zk.testing.setUp(test, '')

    volumes = FauxVolumes(test)

def test_suite():
    return unittest.TestSuite((
        manuel.testing.TestSuite(
            manuel.doctest.Manuel() + manuel.capture.Manuel(),
            'ebs.test',
            setUp=setup, tearDown=setupstack.tearDown,
            ),
        ))
