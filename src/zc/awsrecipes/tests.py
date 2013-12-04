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
import doctest
import manuel.capture
import manuel.doctest
import manuel.testing
import mock
import pprint
import StringIO
import subprocess
import sys
import unittest

def side_effect(m, f=None):
    if f is None:
        return lambda f: side_effect(m, f)
    m.side_effect = f

def assert_(cond, mess='assertion failed'):
    if not cond:
        raise AssertionError(mess)

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
        try:
            self.returncode = handler(command, self) or 0
        except AssertionError, e:
            print 'AssertionError:', e
            self.returncode = -1

    def wait(self):
        return self.returncode

class FauxVolumes:

    def __init__(self, test):
        self.init()
        test.globs['volumes'] = self
        setupstack.context_manager(
            test, mock.patch('os.path.exists', side_effect=self.exists))
        setupstack.context_manager(
            test, mock.patch('os.rename', side_effect=self.rename))
        setupstack.context_manager(
            test, mock.patch('zc.awsrecipes.open', create=True,
                             side_effect=self.open))

        def Popen(command, stdout=None, stderr=None, shell=False):
            assert_(shell)
            meth = command.split()[0].rsplit('/', 1)[-1].replace('.', '_')
            meth = getattr(self, meth)
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
        self.fstab = {}

    def terminate(self):
        self.init(self.sds, self.mds, self.vgs)
        if hasattr(self, 'etc_zim_volumes_setup'):
            self.etc_zim_volumes = self.etc_zim_volumes_setup
            del self.etc_zim_volumes_setup

    def exists(self, name):
        if name.startswith('/dev/'):
            name = name[5:]
            return name in self.sds or name in self.mds
        if [d for d in self.dirs if d.startswith(name+'/') or d == name]:
            return True
        return exists_original(name)

    def echo(self, command, p):
        assert_(command.endswith('>> /etc/fstab'))
        command = command.split()[1:-2]
        assert_(command[-4:] == ['ext3', 'defaults', '0', '1'])
        dev, mp = command[:-4]
        assert_(mp not in self.fstab)
        assert_(not [v for v in self.fstab.values() if v == dev])
        assert_(self.exists(mp))
        mapper = '/dev/mapper/'
        if dev.startswith(mapper):
            vg, v = dev[len(mapper):].split('-')
            assert_(vg in self.vgs, "bad vg")
            assert_(v == 'data')
            assert_(vg in self.lvs)
        else:
            assert_(dev.startswith('/dev/'))
            assert_(dev[5:] in self.fss)

        self.fstab[mp] = dev

    def open(self, name):
        if name == '/etc/mdadm.conf':
            return StringIO.StringIO('x' if self.examined_mds else '')
        elif name == '/etc/zim/volumes':
            return StringIO.StringIO(self.etc_zim_volumes)
        assert_(name=='/proc/mdstat')
        return StringIO.StringIO(self.mdstat())

    def rename(self, src, dest):
        assert_(src == '/etc/zim/volumes')
        assert_(dest == '/etc/zim/volumes-setup')
        self.etc_zim_volumes_setup = self.etc_zim_volumes
        del self.etc_zim_volumes
        print 'rename', src, dest

    def ln(self, command, p):
        args = command.split()
        assert_(args[1] == '-s')
        src, dest = args[2:]
        # give up. doctest handles other assertions :)

    def lvcreate(self, command, p):
        args = command.split()
        assert_(args[1:5] == '-l +100%FREE -n data'.split())
        [vg] = args[5:]
        assert_(vg not in self.lvs)
        self.lvs[vg] = self.vgs[vg]

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
            assert_(self.examined_mds)
            self.mds.update(self.examined_mds)
        elif args[1:5] == '--create --metadata 1.2 -l10'.split():
            n, md = args[5:7]
            assert_(md.startswith('/dev/'))
            md = md[5:]
            sds = args[7:]
            assert_(not [sd for sd in sds if not sd.startswith('/dev/')])
            sds = [sd[5:] for sd in sds]
            assert_(md not in self.mds)
            assert_(n == ('-n%s' % len(sds)))
            assert_(len(set(sds)) == len(sds))
            assert_(not [sd for sd in sds if sd not in self.sds])
            self.mds[md] = sds
        else:
            assert_(0, "Unexpected command %r" % command)

        self.preexisting_mds = self.mds.copy()

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

    def mkfs_ext3(self, command, p):
        force, dev = command.split()[1:]
        assert_(force == '-F', "bad force")
        assert_(dev.startswith('/dev/'))
        dev = dev[5:]
        assert_(dev in self.sds)
        assert_(dev not in self.fss)
        self.fss[dev] = dev

    def mount(self, command, p):
        args = command.split()
        if len(args) == 2:
            assert(args[1] in self.fstab)
            return

        assert_(args[1:3] == '-t ext3'.split())
        [lv, mp] = args[3:]
        assert_(lv.startswith('/dev/'))
        vg = lv[5:]
        if lv.endswith('/data'):
            vg = vg[:-5]
        assert_(vg in self.fss, "no file system")
        assert_(mp in self.dirs, "dirs")
        assert_(mp not in self.mounts, "mounts")
        self.mounts[mp] = vg

    def pvcreate(self, command, p):
        args = command.split()
        [vol] = args[1:]
        assert_(vol.startswith('/dev/'))
        vol = vol[5:]
        assert_((vol in self.mds or vol in self.sds) and vol not in self.pvs)
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
        args = command.split()[1:]
        vg = args.pop(0)
        vols = args
        assert_(not [v for v in vols if not v.startswith('/dev/')],
                "starts w /dev/ %r" % vols)
        vols = [v[5:] for v in vols]
        assert_(not [v for v in vols if not (
            (v in self.pvs or v in self.mds) and
            not [vg_ for vg_ in self.vgs if v in self.vgs[vg_]]
            )], "invalid volume")
        self.vgs[vg] = vols
        self.pvs.update(vols)

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

    setupstack.context_manager(
        test, mock.patch(
            'pwd.getpwuid',
            side_effect=lambda uid: Ob(**dict(
                pw_name='testy',
                pw_gecos='Testy Tester',
                ))))

    setupstack.context_manager(test, mock.patch('time.sleep'))

    volumes = FauxVolumes(test)

def test_suite():
    return unittest.TestSuite((
        manuel.testing.TestSuite(
            manuel.doctest.Manuel() + manuel.capture.Manuel(),
            'main.test',
            setUp=setup, tearDown=setupstack.tearDown,
            ),
        ))
