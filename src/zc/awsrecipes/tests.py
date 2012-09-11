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

import doctest
import unittest
import mock
from zope.testing import setupstack

def side_effect(m, f=None):
    if f is None:
        return lambda f: side_effect(m, f)
    m.side_effect = f

def assert_(cond, mess='assertion failed'):
    if not cond:
        raise AssertionError(mess)

class Resource:
    def __init__(self, id, tags=None, attrs=None, **kw):
        self.id = id
        self.tags = tags or {}
        self.attrs = attrs or {}
        self.__dict__.update(kw)

def filter(data, filters):
    assert_(filters['tag-key'] == 'Name')
    return [
        r for r in data
        if r.tags['Name'] == filters['tag-value']
        ]

class Connection:

    def __init__(self):
        self.volumes = []
        self.instances = []
        self.resources = {}
        self.groups = dict(
            default=Resource('gr1'),
            sg1=Resource('gr2'),
            )

    def create_volume(self, size, zone):
        assert_(zone == 'us-east-1b')
        volume = Resource('vol%s' % len(self.volumes), size=size, zone=zone)
        self.volumes.append(volume)
        self.resources[volume.id] = volume
        return volume

    def create_tags(self, ids, tags):
        for id in ids:
            self.resources[id].tags.update(tags)

    def get_all_volumes(self, ids=None):
        if ids is None:
            return list(self.volumes)
        else:
            return [v for v in self.volumes if v.id in ids]

    def get_all_images(self, filters):
        return [Resource('ami-42')]

    def get_all_instances(self, filters):
        return [Resource('', instances=filter(self.instances, filters))]

    def run_instances(
        self, ami_id, security_groups, placement, user_data, instance_type):
        id = 'inst%s' % len(self.instances)
        instance = Resource(
            id,
            image_id=ami_id,
            placement=placement,
            attrs=dict(
                userData=user_data.encode('base64'),
                groupSet=[self.groups[name] for name in security_groups],
                ),
            )
        self.instances.append(instance)
        self.resources[instance.id] = instance
        return [Resource('', instances=[instance])]

def setup(test):
    connections = dict(test_region=Connection())

    @side_effect(
        setupstack.context_manager(
            test, mock.patch('boto.ec2.connection.EC2Connection')))
    def connect(region):
        return connections[region]



def test_suite():
    return unittest.TestSuite((
        doctest.DocFileSuite(
            'ebs.test', 'ec2.test',
            setUp=setup, tearDown=setupstack.tearDown),
        ))

if __name__ == '__main__':
    unittest.main(defaultTest='test_suite')

