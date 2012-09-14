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


def _zk(zk, path):

    hosts = dict(zk.properties('/hosts'))
    region = hosts['region']
    properties = dict(zk.properties(path))

    if 'zone' not in properties:
        assert 'default-zone' in hosts, "no zone specified"
        properties['zone'] = hosts['default-zone']

    return properties, hosts, boto.ec2.connect_to_region(region)

def assert_(cond, *message):
    if not cond:
        raise AssertionError(*message)

def path_to_name(path):
    return path[1:].replace('/', ',')

def tag_filter(**kw):
    return dict(('tag:'+name, kw[name]) for name in kw)

def default_security_group_for_subnet(region, subnet_id):
    vpc_conn = boto.vpc.VPCConnection(region=boto.ec2.get_region(region))
    [sub] = vpc_conn.get_all_subnets([subnet_id])
    [group] = [g for g in vpc_conn.get_all_security_groups()
               if g.name == 'default' and g.vpc_id == sub.vpc_id
               ]
    return group.id

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

    properties, hosts, conn = _zk(zk, path)

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
    properties, hosts, conn = _zk(zk, path)
    hostname = path.rsplit('/')[1]

    existing = conn.get_all_instances(
        filters=tag_filter(Name=hostname))
    assert_(not existing, "%s exists" % hostname)

    subnet_id = hosts['subnet']

    role = properties.get(
        'role', path[1:].rsplit('/')[0].replace('/', ',')+',storage')

    reservation = conn.run_instances(
        image_id = hosts['ami'],
        security_group_ids=[default_security_group_for_subnet(
            hosts['region'], subnet_id)],
        user_data=storage_user_data_template % dict(
            role=role,
            hostname=hostname,
            path=path,
            ),
        instance_type=properties['instance-type'],
        subnet_id = subnet_id,
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

    for name in properties:
        if not name.startswith('sd'):
            continue
        vpath, replica = properties[name].split()
        vproperties = zk.properties(vpath)
        for vol in conn.get_all_volumes(
            filters=tag_filter(
                logical=vpath,
                replica=replica,
                )):
            vol.attach(instance.id, name+vol.tags['index'])

if __name__ == '__main__':
    main()
