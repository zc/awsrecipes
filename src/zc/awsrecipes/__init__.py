import boto.ec2
import boto.vpc
import optparse
import pwd
import os
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

    if 'zone' not in properties:
        properties['zone'] = vpc.tags['zone']

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

    properties, _, conn = _zk(zk, path)

    size = properties['size']
    existing = set()
    for vol in conn.get_all_volumes(filters=tag_filter(logical=path)):
        assert_(vol.size == size, (
            "Existing volumne, %s, has size %s"
            % (vol.tags['Name'], vol.size)))
        existing.add(vol.tags['Name'])

    needed = set()
    for replica in properties['replicas']:
        for index in range(1, properties['n'] + 1):
            name = "%s %s-%s" % (path, replica, index)
            needed.add(name)
            if name in existing:
                print 'exists', name
            else:
                vol = conn.create_volume(size, properties['zone'])
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

    existing = conn.get_all_instances(filters=tag_filter(Name=hostname))
    assert_(not existing, "%s exists" % hostname)

    vdata = []
    for name in properties:
        if not name.startswith('sd'):
            continue
        vpath, replica = properties[name].split()
        vproperties = zk.properties(vpath)
        vdata.append((vpath, replica, vproperties))

    [subnet] = vpc.connection.get_all_subnets(
        filters={'tag:scope': 'private', 'vpc_id': vpc.id})
    subnet_id = subnet.id

    [group_id] = [
        g.id
        for g in vpc.connection.get_all_security_groups(
            filters=dict(vpc_id=vpc.id))
        if g.name == 'default'
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

    for vpath, replica, vproperties in vdata:
        for vol in conn.get_all_volumes(
            filters=tag_filter(
                logical=vpath,
                replica=replica,
                )):
            vol.attach(instance.id, name+vol.tags['index'])
