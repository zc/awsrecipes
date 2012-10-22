import boto.ec2
import boto.vpc
import optparse
import pwd
import os
import re
import subprocess
import sys
import tempfile
import time
import zc.metarecipe
import zc.zk

def _zkargs(args):
    [zoo, path] = args

    return zc.zk.ZK(zoo), path


def find_cluster(cluster_name):
    for region in boto.ec2.regions():
        vpc_connection = boto.vpc.VPCConnection(region=region)
        vpcs = vpc_connection.get_all_vpcs(filters={'tag:Name': cluster_name})
        if vpcs:
            [vpc] = vpcs
            return vpc

def _zk(zk, path):

    hosts = dict(zk.properties('/hosts'))
    cluster = hosts['cluster']
    vpc = find_cluster(cluster)

    properties = dict(zk.properties(path))

    return properties, vpc, boto.ec2.connect_to_region(vpc.region.name)

def assert_(cond, *message):
    if not cond:
        raise AssertionError(*message)

def path_to_name(path):
    return path[1:].replace('/', ',')

def tag_filter(**kw):
    return dict(('tag:'+name, kw[name]) for name in kw)


def who():
    return "%s (%s)" % (
        pwd.getpwuid(os.geteuid()).pw_name,
        pwd.getpwuid(os.geteuid()).pw_gecos,
        )

def lebs_main(args=None):
    parser = optparse.OptionParser("""Usage: %prog ZOO PATH""")
    options, args = parser.parse_args(args)
    if args is None:
        args = sys.argv[1:]

    lebs(*_zkargs(args))

def lebs(zk, path):

    properties, vpc, conn = _zk(zk, path)

    [subnet] = vpc.connection.get_all_subnets(
        filters={'tag:scope': 'private', 'vpc_id': vpc.id})

    size = properties['size']
    existing = set()
    for vol in conn.get_all_volumes(filters=tag_filter(logical=path)):
        assert_(vol.size == size, (
            "Existing volumne, %s, has size %s"
            % (vol.tags['Name'], vol.size)))
        existing.add(vol.tags['Name'])

    needed = set()
    replicas = properties['replicas']
    if not isinstance(replicas, (tuple, list)):
        replicas = (replicas, )
    for replica in replicas:
        for index in range(1, properties['n'] + 1):
            name = "%s %s-%s" % (path, replica, index)
            needed.add(name)
            if name in existing:
                print 'exists', name
            else:
                vol = conn.create_volume(size, subnet.availability_zone)
                conn.create_tags(
                    [vol.id], dict(
                        Name=name,
                        logical=path,
                        replica=str(replica),
                        index=str(index),
                        creator=who(),
                        ))
                print 'created', name

    extra = existing - needed
    if extra:
        print 'Unused:', sorted(extra)

storage_user_data_template = """#!/bin/sh
echo %(role)r > /etc/zim/role
hostname %(hostname)s
/usr/bin/yum -y install awsrecipes
/opt/awsrecipes/bin/setup_volumes %(path)s
"""


def storage_server_main(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = optparse.OptionParser("""Usage: %prog ZOO PATH""")

    options, args = parser.parse_args(args)

    storage_server(*_zkargs(args))


def storage_server(zk, path):
    properties, vpc, conn = _zk(zk, path)
    hostname = path.rsplit('/', 1)[1]
    domain = vpc.tags['Name']+'.aws.zope.net'
    hostname += '.' + domain

    existing = conn.get_all_instances(filters=tag_filter(Name=hostname))
    assert_(not existing, "%s exists" % hostname)

    vdata = []
    for name in properties:
        if not name.startswith('sd'):
            continue
        vpath, replica = properties[name].split()
        vproperties = zk.properties(vpath)

        vols = []
        for vol in conn.get_all_volumes(
            filters=tag_filter(
                logical=vpath,
                replica=replica,
                )):
            vols.append(vol)

        if (sorted(v.tags['index'] for v in vols) !=
            map(str, range(1, vproperties['n'] + 1))
            ):
            raise AssertionError(
                "Missing volumes",
                vpath, vproperties['n'],
                sorted(v.tags['index'] for v in vols)
                )
        vdata.append((name, vols))

    [subnet] = vpc.connection.get_all_subnets(
        filters={'tag:scope': 'private', 'vpc_id': vpc.id})
    subnet_id = subnet.id

    [group_id] = [
        g.id
        for g in vpc.connection.get_all_security_groups(
            filters=dict(vpc_id=vpc.id))
        if '-VPCSecurityGroup-' in g.name
        ]

    role = properties.get(
        'role', path[1:].rsplit('/')[0].replace('/', ',')+',storage')

    [image_id] = [i.id for i in conn.get_all_images(
        filters={'tag:Name': properties.get('ami', 'default')})]

    reservation = conn.run_instances(
        image_id = image_id,
        instance_type=properties['instance-type'],
        subnet_id = subnet_id,
        security_group_ids=[group_id],
        user_data=storage_user_data_template % dict(
            role=role,
            hostname=hostname,
            path=path,
            ),
        )
    instance = reservation.instances[0]

    conn.create_tags([instance.id], dict(
        Name=hostname,
        creator=who(),
        ))

    while 1:
        time.sleep(9)
        state = instance.update()
        if state == 'running':
            break
        print state

    for name, vols in vdata:
        for vol in vols:
            vol.attach(instance.id, name+vol.tags['index'])

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

    def __init__(self, name, count, path):
        self.name = name
        self.count = count
        self.path = path
        self.mds = set()
        self.pvs = set()
        self.used = set()
        self.sdvols = set('%s%s' % (name, i+1) for i in range(count))
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

        path = '/home/databases'+self.path
        s('/bin/mkdir -p %s' % path)
        s('/bin/mount -t ext3 /dev/vg_%s/data %s' % (self.name, path))


def setup_volumes(zookkeeper, path):
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
    zk = zc.zk.ZK(zookkeeper)
    logical_volumes = {}
    expected_sdvols = set()
    ebsdev = re.compile(r'sd[a-z]$').match
    vpaths = []
    for property_name, v in sorted(zk.properties(path).items()):
        m = ebsdev(property_name)
        if not m:
            continue
        vpath, replica = v.split()
        vproperties = zk.properties(vpath)
        nvols = vproperties['n']
        sdprefix = m.group(0)
        vpath = vproperties.get('path', vpath.rsplit('/', 1)[0] or vpath)
        if vpath in vpaths:
            raise ValueError("Duplicate mount points", vpath)
        else:
            vpaths.append(vpath)
        logical_volumes[sdprefix] = LogicalVolume(sdprefix, nvols, vpath)
        expected_sdvols.update(logical_volumes[sdprefix].sdvols)

    vpaths.sort()
    for i in range(1, len(vpaths)):
        vpathp = vpaths[i-1]
        if not vpathp.endswith('/'):
            vpathp += '/'
        if vpaths[i].startswith(vpathp):
            raise ValueError("One mount point is a prefix of another",
                             vpathp, vpaths[i])

    # Wait for all of our expected sd volumes to appear. (They may be
    # attaching.)
    for v in expected_sdvols:
        while not os.path.exists('/dev/'+v):
            time.sleep(.1)

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

def setup_volumes_main(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = optparse.OptionParser("""Usage: %prog ZOO PATH""")

    options, args = parser.parse_args(args)
    setup_volumes(*args)
