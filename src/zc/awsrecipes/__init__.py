import os
import re
import subprocess
import sys
import tempfile
import time

def s(command, should_raise=True):
    print command
    if subprocess.call(command, shell=True):
        if should_raise:
            raise SystemError(command)
        return False
    return True

def p(command):
    print command
    f = tempfile.TemporaryFile(prefix='awsrecipes')
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

def single(mount_point, device):
    if not os.path.exists(mount_point):
        s('/bin/mkdir -p %s' % mount_point)
    wait_for_device(device)
    if not s("/bin/mount -t ext3 %s %s" % (device, mount_point),
             should_raise=False):
        s("/sbin/mkfs.ext3 -F "+device)
        s("/bin/echo %s %s ext3 defaults 0 1 >> /etc/fstab"
          % (device, mount_point))
        s("/bin/mount %s" % mount_point)

lvname = re.compile(r"\w+/\w+$").match
def lvm(mount_point, sdvols):
    # Make a non-raid logical volume
    if not os.path.exists(mount_point):
        s('/bin/mkdir -p %s' % mount_point)
    vg, v = sdvols.pop(0).split('/')
    sdvols = ["/dev/"+pvol for pvol in sdvols]
    make_sure_physical_volumes_dont_exist(sdvols)
    for pvol in sdvols:
        s("/usr/sbin/pvcreate "+pvol)
    s("/usr/sbin/vgcreate %s %s" % (vg, " ".join(sdvols)))
    s("/usr/sbin/lvcreate -l +100%%FREE -n %s %s" % (v, vg))
    s("/sbin/mkfs -t ext3 /dev/%s/%s" % (vg, v))
    s("/bin/echo /dev/mapper/%s-%s %s ext3 defaults 0 1 >> /etc/fstab"
      % (vg, v, mount_point))
    s("/bin/mount %s" % mount_point)

def make_sure_physical_volumes_dont_exist(vols):
    for v in vols:
        wait_for_device(v)
    for line in p("/usr/sbin/pvscan"):
        line = line.strip()
        if not line:
            continue
        line = line.split()
        if line[0] == 'PV':
            if line[1] in vols:
                raise ValueError(line[1], "already in use")

def ln(mount_point, src):
    if not os.path.exists(os.path.dirname(mount_point)):
        s('/bin/mkdir -p %s' % os.path.dirname(mount_point))
    if not os.path.exists(src):
        s('/bin/mkdir -p %s' % src)
    s("/bin/ln -s %s %s" % (src, mount_point))

def wait_for_device(path):
    while not os.path.exists(path):
        time.sleep(1)

def setup_volumes():
    """Set up md (raid) and lvm modules on a new machine
    """

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
        if len(sdvols) == 1:
            dev = sdvols[0]
            if dev[0] == '/':
                ln(mount_point, dev)
            else:
                single(mount_point, '/dev/'+dev)
            continue

        if len(sdvols) < 1:
            raise ValueError(line)

        if lvname(sdvols[0]):
            lvm(mount_point, sdvols)
            continue


        # RAID10:
        assert len(set(sdvol[:3] for sdvol in sdvols)) == 1, (
            "Multiple device prefixes")
        sdprefix = sdvols[0][:3]
        logical_volumes[sdprefix] = LogicalVolume(
            sdprefix, sdvols, mount_point)
        expected_sdvols.update(sdvols)

    if logical_volumes:

        # Wait for all of our expected sd volumes to appear. (They may be
        # attaching.)
        for v in expected_sdvols:
            wait_for_device('/dev/' + v)

        # The volumes may have been set up before on a previous machine.
        # Scan for them:
        s('/sbin/mdadm --examine --scan >>/etc/mdadm.conf')
        f = open('/etc/mdadm.conf')
        if f.read().strip():
            s('/sbin/mdadm -A --scan')
        f.close()

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
