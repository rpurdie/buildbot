# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Portions Copyright Buildbot Team Members
# Portions Copyright 2014 Longaccess private company

import os

from twisted.trial import unittest

from buildbot.test.util.warnings import assertNotProducesWarnings
from buildbot.warnings import DeprecatedApiWarning

try:
    import boto3
    from botocore.client import ClientError
    from moto import mock_aws
except ImportError:
    boto3 = None
    ClientError = None


if boto3 is not None:
    from buildbot.worker import ec2
else:
    ec2 = None  # type: ignore[assignment]


# Current moto (1.3.7) requires dummy credentials to work
# https://github.com/spulec/moto/issues/1924
os.environ['AWS_SECRET_ACCESS_KEY'] = 'foobar_secret'
os.environ['AWS_ACCESS_KEY_ID'] = 'foobar_key'
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'


# redefine the mock_aws decorator to skip the test if boto3 or moto
# isn't installed
def skip_ec2(f):
    f.skip = "boto3 or moto is not installed"
    return f


if boto3 is None:
    mock_aws = skip_ec2  # type: ignore[assignment]


def anyImageId(c):
    for image in c.describe_images()['Images']:
        return image['ImageId']
    return 'foo'


class TestEC2LatentWorker(unittest.TestCase):
    ec2_connection = None

    def setUp(self):
        super().setUp()
        if boto3 is None:
            raise unittest.SkipTest("moto not found")

    def botoSetup(self, name='latent_buildbot_worker'):
        # the proxy system is also not properly mocked, so we need to delete environment variables
        for env in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']:
            if env in os.environ:
                del os.environ[env]
        # create key pair is not correctly mocked and need to have fake aws creds configured
        kw = {
            "region_name": 'us-east-1',
            "aws_access_key_id": 'ACCESS_KEY',
            "aws_secret_access_key": 'SECRET_KEY',
            "aws_session_token": 'SESSION_TOKEN',
        }
        c = boto3.client('ec2', **kw)
        r = boto3.resource('ec2', **kw)
        try:
            r.create_key_pair(KeyName=name)
        except NotImplementedError as e:
            raise unittest.SkipTest(
                "KeyPairs.create_key_pair not implemented in this version of moto, please update."
            ) from e
        r.create_security_group(GroupName=name, Description='the security group')
        instance = r.create_instances(ImageId=anyImageId(c), MinCount=1, MaxCount=1)[0]
        c.create_image(InstanceId=instance.id, Name="foo", Description="bar")
        c.terminate_instances(InstanceIds=[instance.id])
        return c, r

    def _patch_moto_describe_spot_price_history(self, bs, instance_type, price):
        def fake_describe_price(*args, **kwargs):
            return {'SpotPriceHistory': [{'InstanceType': instance_type, 'SpotPrice': price}]}

        self.patch(bs.ec2.meta.client, "describe_spot_price_history", fake_describe_price)

    def _patch_moto_describe_spot_instance_requests(self, c, r, bs):
        this_call = [0]

        orig_describe_instance = bs.ec2.meta.client.describe_spot_instance_requests

        def fake_describe_spot_instance_requests(*args, **kwargs):
            curr_call = this_call[0]
            this_call[0] += 1
            if curr_call == 0:
                raise ClientError(
                    {'Error': {'Code': 'InvalidSpotInstanceRequestID.NotFound'}},
                    'DescribeSpotInstanceRequests',
                )
            if curr_call == 1:
                return orig_describe_instance(*args, **kwargs)

            response = orig_describe_instance(*args, **kwargs)

            instances = r.instances.filter(
                Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
            )

            response['SpotInstanceRequests'][0]['Status']['Code'] = 'fulfilled'
            response['SpotInstanceRequests'][0]['InstanceId'] = next(iter(instances)).id
            return response

        self.patch(
            bs.ec2.meta.client,
            'describe_spot_instance_requests',
            fake_describe_spot_instance_requests,
        )

    @mock_aws
    def test_constructor_minimal(self):
        _, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name='keypair_name',
            security_name='security_name',
            ami=amis[0].id,
        )
        self.assertEqual(bs.workername, 'bot1')
        self.assertEqual(bs.password, 'sekrit')
        self.assertEqual(bs.instance_type, 'm1.large')
        self.assertEqual(bs.ami, amis[0].id)

    @mock_aws
    def test_constructor_tags(self):
        _, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        tags = {'foo': 'bar'}
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name='keypair_name',
            security_name='security_name',
            tags=tags,
            ami=amis[0].id,
        )
        self.assertEqual(bs.tags, tags)

    @mock_aws
    def test_constructor_region(self):
        _, r = self.botoSetup()
        amis = list(r.images.all())
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            ami=amis[0].id,
            region='us-west-1',
        )
        self.assertEqual(bs.session.region_name, 'us-west-1')

    @mock_aws
    def test_fail_mixing_classic_and_vpc_ec2_settings(self):
        _, r = self.botoSetup()
        amis = list(r.images.all())

        def create_worker():
            ec2.EC2LatentWorker(
                'bot1',
                'sekrit',
                'm1.large',
                keypair_name="test_key",
                identifier='publickey',
                secret_identifier='privatekey',
                ami=amis[0].id,
                security_name="classic",
                subnet_id="sn-1234",
            )

        with self.assertRaises(ValueError):
            create_worker()

    @mock_aws
    def test_start_vpc_instance(self):
        _, r = self.botoSetup()

        vpc = r.create_vpc(CidrBlock="192.168.0.0/24")
        subnet = r.create_subnet(VpcId=vpc.id, CidrBlock="192.168.0.0/24")
        amis = list(r.images.all())

        sg = r.create_security_group(GroupName="test_sg", Description="test_sg", VpcId=vpc.id)
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_group_ids=[sg.id],
            subnet_id=subnet.id,
            ami=amis[0].id,
        )
        bs._poll_resolution = 0
        instance_id, _, _ = bs._start_instance()
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, instance_id)
        self.assertEqual(instances[0].subnet_id, subnet.id)
        self.assertEqual(len(instances[0].security_groups), 1)
        self.assertEqual(instances[0].security_groups[0]['GroupId'], sg.id)
        self.assertEqual(instances[0].key_name, 'latent_buildbot_worker')

    @mock_aws
    def test_start_instance(self):
        _, r = self.botoSetup()
        amis = list(r.images.all())
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name='keypair_name',
            security_name='security_name',
            ami=amis[0].id,
        )
        bs._poll_resolution = 1
        instance_id, image_id, start_time = bs._start_instance()
        self.assertTrue(instance_id.startswith('i-'))
        self.assertTrue(image_id.startswith('ami-'))
        self.assertTrue(start_time > "00:00:00")
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, instance_id)
        self.assertIsNone(instances[0].tags)
        self.assertEqual(instances[0].id, bs.properties.getProperty('instance'))

    @mock_aws
    def test_start_instance_volumes(self):
        _, r = self.botoSetup()
        block_device_map_arg = [
            {
                'DeviceName': "/dev/xvdb",
                'Ebs': {
                    "VolumeType": "io1",
                    "Iops": 10,
                    "VolumeSize": 20,
                },
            },
            {
                'DeviceName': "/dev/xvdc",
                'Ebs': {
                    "VolumeType": "gp2",
                    "VolumeSize": 30,
                    "DeleteOnTermination": False,
                },
            },
        ]
        block_device_map_res = [
            {
                'DeviceName': "/dev/xvdb",
                'Ebs': {
                    "VolumeType": "io1",
                    "Iops": 10,
                    "VolumeSize": 20,
                    "DeleteOnTermination": True,
                },
            },
            {
                'DeviceName': "/dev/xvdc",
                'Ebs': {
                    "VolumeType": "gp2",
                    "VolumeSize": 30,
                    "DeleteOnTermination": False,
                },
            },
        ]

        amis = list(r.images.all())
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            ami=amis[0].id,
            block_device_map=block_device_map_arg,
        )
        # moto does not currently map volumes properly.  below ensures
        # that my conversion code properly composes it, including
        # delete_on_termination default.
        self.assertEqual(block_device_map_res, bs.block_device_map)

    @mock_aws
    def test_start_instance_attach_volume(self):
        _, r = self.botoSetup()
        vol = r.create_volume(Size=10, AvailabilityZone='us-east-1a')
        amis = list(r.images.all())
        ami = amis[0]
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            ami=ami.id,
            volumes=[(vol.id, "/dev/sdz")],
        )
        bs._poll_resolution = 0
        bs._start_instance()
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)
        instance = instances[0]
        sdz = next(bm for bm in instance.block_device_mappings if bm['DeviceName'] == '/dev/sdz')
        self.assertEqual(vol.id, sdz['Ebs']['VolumeId'])

    @mock_aws
    def test_start_instance_tags(self):
        _, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        tags = {'foo': 'bar'}
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            tags=tags,
            ami=amis[0].id,
        )
        bs._poll_resolution = 0
        id, _, _ = bs._start_instance()
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, id)
        self.assertEqual(instances[0].tags, [{'Value': 'bar', 'Key': 'foo'}])

    @mock_aws
    def test_start_instance_ip(self):
        c, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        eip = c.allocate_address(Domain='vpc')
        elastic_ip = eip['PublicIp']
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            elastic_ip=elastic_ip,
            ami=amis[0].id,
        )
        bs._poll_resolution = 0
        bs._start_instance()
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)
        addresses = c.describe_addresses()['Addresses']
        self.assertEqual(instances[0].id, addresses[0]['InstanceId'])

    @mock_aws
    def test_start_vpc_spot_instance(self):
        c, r = self.botoSetup()

        vpc = r.create_vpc(CidrBlock="192.168.0.0/24")
        subnet = r.create_subnet(VpcId=vpc.id, CidrBlock="192.168.0.0/24")
        amis = list(r.images.all())

        sg = r.create_security_group(GroupName="test_sg", Description="test_sg", VpcId=vpc.id)

        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            ami=amis[0].id,
            spot_instance=True,
            max_spot_price=1.5,
            security_group_ids=[sg.id],
            subnet_id=subnet.id,
        )
        bs._poll_resolution = 0

        self._patch_moto_describe_spot_price_history(bs, 'm1.large', price=1.0)
        self._patch_moto_describe_spot_instance_requests(c, r, bs)

        instance_id, _, _ = bs._request_spot_instance()
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)

        self.assertTrue(bs.spot_instance)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, instance_id)
        self.assertEqual(instances[0].subnet_id, subnet.id)
        self.assertEqual(len(instances[0].security_groups), 1)

        # TODO: As of moto 2.0.2 GroupId is not handled in spot requests
        # self.assertEqual(instances[0].security_groups[0]['GroupId'], sg.id)

    @mock_aws
    def test_start_spot_instance(self):
        c, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        product_description = 'Linux/Unix'
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name='keypair_name',
            security_name='security_name',
            ami=amis[0].id,
            spot_instance=True,
            max_spot_price=1.5,
            product_description=product_description,
        )
        bs._poll_resolution = 0

        self._patch_moto_describe_spot_price_history(bs, 'm1.large', price=1.0)
        self._patch_moto_describe_spot_instance_requests(c, r, bs)

        instance_id, _, _ = bs._request_spot_instance()
        instances = r.instances.filter(
            Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]
        )
        instances = list(instances)
        self.assertTrue(bs.spot_instance)
        self.assertEqual(bs.product_description, product_description)
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, instance_id)
        self.assertIsNone(instances[0].tags)

    @mock_aws
    def test_get_image_ami(self):
        _, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        ami = amis[0]
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            ami=ami.id,
        )
        image = bs.get_image()

        self.assertEqual(image.id, ami.id)

    @mock_aws
    def test_get_image_owners(self):
        _, r = self.botoSetup('latent_buildbot_slave')
        amis = list(r.images.all())
        ami = amis[0]
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            valid_ami_owners=[int(ami.owner_id)],
        )
        image = bs.get_image()

        self.assertEqual(image.owner_id, ami.owner_id)

    @mock_aws
    def test_get_image_location(self):
        self.botoSetup('latent_buildbot_slave')
        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            keypair_name="latent_buildbot_worker",
            security_name='latent_buildbot_worker',
            valid_ami_location_regex='amazon/.*',
        )
        image = bs.get_image()

        self.assertTrue(image.image_location.startswith("amazon/"))

    @mock_aws
    def test_get_image_location_not_found(self):
        def create_worker():
            ec2.EC2LatentWorker(
                'bot1',
                'sekrit',
                'm1.large',
                identifier='publickey',
                secret_identifier='privatekey',
                keypair_name="latent_buildbot_worker",
                security_name='latent_buildbot_worker',
                valid_ami_location_regex='foobar.*',
            )

        with self.assertRaises(ValueError):
            create_worker()

    @mock_aws
    def test_fail_multiplier_and_max_are_none(self):
        """
        price_multiplier and max_spot_price may not be None at the same time.
        """
        _, r = self.botoSetup()
        amis = list(r.images.all())

        def create_worker():
            ec2.EC2LatentWorker(
                'bot1',
                'sekrit',
                'm1.large',
                identifier='publickey',
                secret_identifier='privatekey',
                keypair_name="latent_buildbot_worker",
                security_name='latent_buildbot_worker',
                ami=amis[0].id,
                region='us-west-1',
                spot_instance=True,
                price_multiplier=None,
                max_spot_price=None,
            )

        with self.assertRaises(ValueError):
            create_worker()


class TestEC2LatentWorkerDefaultKeyairSecurityGroup(unittest.TestCase):
    ec2_connection = None

    def setUp(self):
        super().setUp()
        if boto3 is None:
            raise unittest.SkipTest("moto not found")

    def botoSetup(self):
        c = boto3.client('ec2', region_name='us-east-1')
        r = boto3.resource('ec2', region_name='us-east-1')
        try:
            r.create_key_pair(KeyName='latent_buildbot_slave')
            r.create_key_pair(KeyName='test_keypair')
        except NotImplementedError as e:
            raise unittest.SkipTest(
                "KeyPairs.create_key_pair not implemented in this version of moto, please update."
            ) from e
        r.create_security_group(GroupName='latent_buildbot_slave', Description='the security group')
        r.create_security_group(GroupName='test_security_group', Description='other security group')
        instance = r.create_instances(ImageId=anyImageId(c), MinCount=1, MaxCount=1)[0]
        c.create_image(InstanceId=instance.id, Name="foo", Description="bar")
        c.terminate_instances(InstanceIds=[instance.id])
        return c, r

    @mock_aws
    def test_no_default_security_warning_when_security_group_ids(self):
        _, r = self.botoSetup()
        amis = list(r.images.all())

        bs = ec2.EC2LatentWorker(
            'bot1',
            'sekrit',
            'm1.large',
            identifier='publickey',
            secret_identifier='privatekey',
            ami=amis[0].id,
            keypair_name='test_keypair',
            subnet_id=["sn-1"],
        )
        self.assertEqual(bs.security_name, None)

    @mock_aws
    def test_use_non_default_keypair_security(self):
        _, r = self.botoSetup()
        amis = list(r.images.all())
        with assertNotProducesWarnings(DeprecatedApiWarning):
            bs = ec2.EC2LatentWorker(
                'bot1',
                'sekrit',
                'm1.large',
                identifier='publickey',
                secret_identifier='privatekey',
                ami=amis[0].id,
                security_name='test_security_group',
                keypair_name='test_keypair',
            )
        self.assertEqual(bs.keypair_name, 'test_keypair')
        self.assertEqual(bs.security_name, 'test_security_group')
