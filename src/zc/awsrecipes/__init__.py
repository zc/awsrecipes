import boto.ec2.connection
import zc.metarecipe
import zc.zk
import tempfile


ZK_LOCATION = 'zookeeper:2181'

class EBS:
    def __init__(self, buildout, name, options):
        self.size = options['size']
        self.zone = options['zone']
        self.volname = options['name']

        self.conn = boto.ec2.connection.EC2Connection(
            region=options['region']
        )

    def install(self):
        '''Create a EBS volumen and set tags
        '''
        vol = self.conn.create_volume(int(self.size), self.zone)
        self.conn.create_tags([vol.id], dict(Name=self.volname))
        return ()

    def update(self):
        '''Update does not create a new volume if one with the
        name tag exists
        '''
        if self.volname in [v.tags['Name']
                            for v in self.conn.get_all_volumes()]:
            return ()
        else:
            return self.install()

user_data_start = '''#!/bin/sh
hostname %s.aws.zope.net
bcfg2
'''

def uninstall_ebs_volume(name, options):
    pass


class EC2:

    def __init__(self, buildout, name, options):
        self.options = options

    def install(self):
        options = self.options
        conn = boto.ec2.connection.EC2Connection(region=options['region'])

        [ami] = conn.get_all_images(
            filters={'tag-key': 'Name', 'tag-value': 'default'})

        user_data = user_data_start % options['name']

        role = options.get('role')
        if role:
            user_data += '/opt/awshelpers/bin/set_role %s\n' % role

        res = conn.run_instances(
            ami.id,
            security_groups = options.get(
                'security-groups', '').split() + ['default'],
            placement = options['zone'],
            user_data = user_data,
            instance_type=options['type'],
            )
        conn.create_tags([instance.id for instance in res[0].instances],
                         dict(Name=self.options['name']))

        return ()

    update = install

def uninstall_ec2_instance(name, options):
    '''Uninstall an ec2 instance by its name
    '''

    conn = boto.ec2.connection.EC2Connection(region=options['region'])
    reservations = conn.get_all_instances(
        filters={'tag-key': 'Name', 'tag-value': self.options['name']})

    # stop instances
    return [instance.terminate() for instance in reservation.instances
            for reservation in reservations if instance.state == u'running']


# EC2 ebs-based instance


# AWS meta-recipe
class AWS(zc.metarecipe.Recipe):
    '''Meta recipe to create a AWS cluster
    '''

    def __init__(self, buildout, name, options):
        super(AWS, self).__init__(buildout, name, options)

        zk = zc.zk.ZK(ZK_LOCATION)

        zk_options = zk.properties(
            '/' + name.replace(',', '/').rsplit('.', 1)[0]
        )

        self['ebs_volumes'] = dict(
            recipe = 'zc.awsrecipes.EBS',
        )

        self['ec2_instances'] = dict(
            recipe = 'zc.awsrecipes.EC2',
        )
