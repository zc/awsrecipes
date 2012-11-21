import os
import re
import subprocess
import sys
import tempfile
import time

def s(command):
    print command
    if subprocess.call(command, shell=True):
        raise SystemError(command)

def p(command):
    print command
    f = tempfile.TemporaryFile('awsrecipes')
    if subprocess.call(command, stdout=f, stderr=subprocess.STDOUT, shell=True):
        raise SystemError(command)
    f.seek(0)
    for line in f:
        yield line
    f.close()

class LogicalVolume:

    def __init__(self, name, sdvols, path):
        self.name = name
        self.path = path
        self.mds = set()
        self.pvs = set()
        self.used = set()
        self.sdvols = set(sdvols)
        self.logical = False

    def add_md(self, mdnum, volumes):
        assert not [v for v in volumes if not v.startswith(self.name)]
        self.mds.add(mdnum)
        assert not [v for v in volumes if v in self.used], (
            'repeated volume', volumes)
        self.used.update(volumes)

    def has_logical_volume(self):
        self.logical = True
        s('/sbin/vgchange -a y vg_'+self.name)

    def setup(self):
        assert self.pvs == self.mds, (
            "Physical volumes in logical volumes don't match the raid"
            " volumes we found.", self.pvs, self.mds
            )
        unused = sorted(self.sdvols - self.used)
        if unused:
            mdnum = 0
            while os.path.exists('/dev/md%s' % mdnum):
                mdnum += 1
            s('/sbin/mdadm --create --metadata 1.2 -l10 -n%s /dev/md%s %s'
              % (len(unused), mdnum, ' '.join('/dev/' + u for u in unused))
              )
            if self.logical:
                s('/usr/sbin/pvcreate /dev/md%s' % mdnum)
                s('/usr/sbin/vgextend vg_%s /dev/md%s' % (self.name, mdnum))
                s('/usr/sbin/lvextend -l +100%%FREE /dev/vg_%s/data'
                  % self.name)
                s('/sbin/resize2fs /dev/vg_%s/data' % self.name)
            else:
                s('/usr/sbin/vgcreate vg_%s /dev/md%s' % (self.name, mdnum))
                s('/usr/sbin/lvcreate -l +100%%FREE -n data vg_%s' % self.name)
                s('/sbin/mkfs -t ext3 /dev/vg_%s/data' % self.name)
                self.logical = True
        else:
            assert self.logical

        path = self.path
        s('/bin/mkdir -p %s' % path)
        s('/bin/mount -t ext3 /dev/vg_%s/data %s' % (self.name, path))


def setup_volumes():
    """Set up md (raid) and lvm modules on a new machine
    """

    # The volumes may have been set up before on a previos machine.
    # Scan for them:
    s('/sbin/mdadm --examine --scan >>/etc/mdadm.conf')
    f = open('/etc/mdadm.conf')
    if f.read().strip():
        s('/sbin/mdadm -A --scan')
    f.close()

    # Get what we want from the ZK tree
    logical_volumes = {}
    expected_sdvols = set()
    f = open('/etc/zim/volumes')
    for line in f:
        line = line.strip()
        if not line:
            continue
        sdvols = line.split()
        mount_point = sdvols.pop(0)
        assert len(set(sdvol[:3] for sdvol in sdvols)) == 1, (
            "Multiple device prefixes")
        sdprefix = sdvols[0][:3]
        logical_volumes[sdprefix] = LogicalVolume(sdprefix, sdvols, mount_point)
        expected_sdvols.update(sdvols)


    # Wait for all of our expected sd volumes to appear. (They may be
    # attaching.)
    for v in expected_sdvols:
        v = '/dev/' + v
        while not os.path.exists(v):
            time.sleep(1)

    # Read /proc/mdstat to find out about existing raid volumes:
    mdstat = re.compile(r'md(\w+) : (\w+) (\w+) (.+)$').match
    mdstatsd = re.compile(r'(sd(\w+))\[\d+\](\(F\))?$').match
    for line in open('/proc/mdstat'):
        if not line.strip():
            continue
        m = mdstat(line)
        if not m:
            assert (line.startswith('Personalities') or
                    line.startswith(' ') or
                    line.startswith('unused devices')), (
                "unexpected line", line
                )
            continue
        mdnum, status, rtype, data = m.group(1, 2, 3, 4)
        data = [mdstatsd(d).groups() for d in data.strip().split()]

        assert not [d for d in data if d[2]], (
            "Failed volume", line
            )

        data = [d[0] for d in data]
        if not [d for d in data if d in expected_sdvols]:
            # Hm, not one weore interested in.
            print 'skipping', line
            continue

        assert not [d for d in data if d not in expected_sdvols], (
            "Unexpected volume", data
            )

        assert status == 'active', status
        assert rtype == 'raid10', rtype

        logical_volumes[data[0][:3]].add_md(mdnum, data)

    # Scan for logical volumes:
    lv_pat = re.compile('Found volume group "vg_(sd\w+)"').search
    for line in p('/usr/sbin/vgscan'):
        m = lv_pat(line)
        if not m:
            continue
        name = m.group(1)
        if name in logical_volumes:
            logical_volumes[name].has_logical_volume()

    # Record the physical volums in each logical_volume so we can see
    # if any are missing:
    PV = re.compile("PV /dev/md(\w+) +VG vg_(sd\w+) ").search
    for line in p("/usr/sbin/pvscan"):
        m = PV(line)
        if not m:
            continue
        mdnum, vgname = m.groups()
        logical_volumes[vgname].pvs.add(mdnum)

    # Finally, create any missing raid volumes and logical volumes
    for lv in logical_volumes.values():
        lv.setup()

    os.rename('/etc/zim/volumes', '/etc/zim/volumes-setup')

def setup_volumes_main(args=None):
    if args is None:
        args = sys.argv[1:]
    assert not args

    setup_volumes()
