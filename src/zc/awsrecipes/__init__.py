import boto.ec2.connection


class EBS:
    def __init__(self, buildout, name, options):
        self.size = options['size']
        self.zone = options['zone']
        self.vol_name = options['name']

        self.conn = boto.ec2.connection.EC2Connection(
            region=options['region']
        )

    def install(self):
        '''Create a EBS volumen and set tags
        '''
        vol = self.conn.create_volume(int(self.size), self.zone)
        self.conn.create_tags([vol.id], dict(Name=self.vol_name))
        return ()

    update = install

user_data_start = '''#!/bin/sh
hostname %s.aws.zope.net
bcfg2
'''

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
