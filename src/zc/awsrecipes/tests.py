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
import boto.ec2.volume
import doctest
import manuel.capture
import manuel.doctest
import manuel.testing
import mock
import pprint
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

def test_suite():
    return unittest.TestSuite((
        manuel.testing.TestSuite(
            manuel.doctest.Manuel() + manuel.capture.Manuel(),
            'ebs.test',
            setUp=setup, tearDown=setupstack.tearDown,
            ),
        ))
