#!/usr/bin/env python
import argparse
import json
import math
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from contextlib import contextmanager
from functools import lru_cache

import boto3
from cached_property import cached_property
from tabulate import tabulate, tabulate_formats
from xdg import XDG_CACHE_HOME

ALL_REGIONS = ['us-east-1', 'us-west-2', 'eu-west-1', 'ca-central-1']

# is there any way to get this from boto?
region_usagetype = {
    'us-east-1': '',
    'us-east-2': 'USE2-',
    'us-west-2': 'USW2-',
    'eu-west-1': 'EU-',
    'eu-west-2': 'EUW2-',
    'ca-central-1': 'CAN1-',
}


@contextmanager
def progress(message):
    print('% {}...'.format(message), file=sys.stderr)
    try:
        yield
    finally:
        pass


# clean up old caches
cache_dir = XDG_CACHE_HOME / 'price-ec2' / 'http'
shutil.rmtree(cache_dir, ignore_errors=True)


def fetch_pricing(service, filters):
    return fetch_pricing_(service, tuple((k, filters[k]) for k in sorted(filters.keys())))


@lru_cache(maxsize=1024)
def fetch_pricing_(service, filters):
    client = boto3.client('pricing', region_name='us-east-1')
    response = client.get_products(
        ServiceCode=service,
        Filters=[
            {
                'Type': 'TERM_MATCH',
                'Field': field,
                'Value': value,
            } for (field, value) in filters
        ]
    )
    prices = response['PriceList']
    if len(prices) != 1:
        raise Exception('found {} prices for {} (expected 1)'.format(len(prices), filters))
    return json.loads(prices[0])


class Instance:
    def __init__(self, id, name, type, state, az):
        self.id = id
        self.name = name
        self.type = type
        self.state = state
        self.az = az
        self.cpu_usage = None

    @property
    def running(self):
        return True

    @property
    def region(self):
        return self.az[:-1]

    @property
    def total_storage(self):
        return 0

    def unit_price(self):
        raise NotImplementedError()

    @cached_property
    def instance_costs(self):
        return list(self.unit_price())

    @cached_property
    def storage_costs(self):
        return [Cost(0, 'Mo')]

    def simple_costs(self):
        instance_cost = just_one(self.instance_costs, 'Hrs')
        storage_cost = just_one(self.storage_costs, 'Mo')
        total_cost = instance_cost.per_day() + storage_cost.per_day()
        if self.running:
            actual_cost = total_cost
        else:
            actual_cost = storage_cost
        return instance_cost, storage_cost, total_cost, actual_cost

    @property
    def cloudwatch_namespace(self):
        return self.CLOUDWATCH_NAMESPACE

    @property
    def cloudwatch_dimensions(self):
        return [{
            'Name': self.ID_DIMENSION,
            'Value': self.id
        }]


class EC2Instance(Instance):
    CLOUDWATCH_NAMESPACE = 'AWS/EC2'
    ID_DIMENSION = 'InstanceId'

    def __init__(self, id, name, type, state, az, platform):
        super().__init__(id, name, type, state, az)
        self.platform = platform
        self.volumes = []

    @property
    def running(self):
        return self.state == 'running'

    @property
    def total_storage(self):
        return sum(v.size for v in self.volumes)

    def unit_price(self):
        if self.type == 'm1.small':
            search_type = region_usagetype[self.region] + 'BoxUsage'
        else:
            search_type = region_usagetype[self.region] + 'BoxUsage:' + self.type

        cpu_pricing = fetch_pricing('AmazonEC2', {
            'regionCode': self.region,
            'usageType': search_type,
            'operatingSystem': self.platform,
            'preInstalledSw': 'NA'
        })
        for term in cpu_pricing['terms']['OnDemand'].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])

    @cached_property
    def storage_costs(self):
        costs = defaultdict(float)
        for volume in self.volumes:
            volume_costs = list(volume.unit_price(self.region))
            for c in volume_costs:
                if c.per.startswith('gb-'):
                    costs[c.per[3:]] += c.dollars * volume.size
                elif c.per.startswith('iops-'):
                    costs[c.per[5:]] += c.dollars * volume.iops
                else:
                    costs[c.per] += c.dollars
        if len(costs) == 0:
            return [Cost(0, 'Mo')]
        return [Cost(b, a) for (a, b) in costs.items()]

    @staticmethod
    def from_json(json):
        id = json['InstanceId']
        name = '???'
        for tag in json.get('Tags', []):
            if tag['Key'] == 'Name':
                name = tag['Value']
                break
        type = json['InstanceType']
        state = json['State']['Name']
        az = json['Placement']['AvailabilityZone']
        platform = json.get('Platform', 'linux')
        instance = EC2Instance(id, name, type, state, az, platform)
        for mapping in json['BlockDeviceMappings']:
            instance.volumes.append(Volume(mapping['Ebs']['VolumeId'], instance.region))
        return instance


class Volume:
    def __init__(self, id, region):
        self.id = id
        self.region = region
        self.type = None
        self.size = None
        self.iops = None

    def unit_price(self, region):
        if self.type == 'io1':
            search_types = ['EBS:VolumeUsage.piops', 'EBS:VolumeP-IOPS.piops']
        elif self.type == 'standard':
            search_types = ['EBS:VolumeUsage']
        else:  # gp2, st1, sc1
            search_types = ['EBS:VolumeUsage.' + self.type]
        search_types = [region_usagetype[region] + t for t in search_types]

        for search_type in search_types:
            storage_pricing = fetch_pricing('AmazonEC2', {
                'regionCode': self.region,
                'usageType': search_type,
            })

            for term in storage_pricing['terms']['OnDemand'].values():
                for dimension in term['priceDimensions'].values():
                    yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])


class DBInstance(Instance):
    CLOUDWATCH_NAMESPACE = 'AWS/RDS'
    ID_DIMENSION = 'DBInstanceIdentifier'

    def __init__(self, id, name, type, engine, state, az, multi_az, storage_type, size, iops):
        super().__init__(id, name, type, state, az)
        self.engine = engine
        self.multi_az = multi_az
        self.storage_type = storage_type
        self.size = size
        self.iops = iops

    @property
    def total_storage(self):
        return self.size

    @property
    def database_engine(self):
        if self.engine == 'postgres':
            return 'PostgreSQL'
        elif self.engine == 'mysql':
            return 'MySQL'
        return self.engine

    def unit_price(self):
        if self.multi_az:
            deployment_option = 'Multi-AZ'
        else:
            deployment_option = 'Single-AZ'

        pricing = fetch_pricing('AmazonRDS', {
            'regionCode': self.region,
            'instanceType': self.type,
            'databaseEngine': self.database_engine,
            'deploymentOption': deployment_option,
        })

        for term in pricing['terms']['OnDemand'].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])

    @cached_property
    def storage_costs(self):
        if self.multi_az:
            search_type_prefix = 'RDS:Multi-AZ-'
        else:
            search_type_prefix = 'RDS:'
        if self.storage_type == 'io1':
            search_types = ['PIOPS-Storage', 'PIOPS']
        elif self.storage_type == 'gp2':
            search_types = ['GP2-Storage']
        elif self.storage_type == 'standard':
            search_types = ['StorageUsage']
        else:
            raise Exception('unknown search type for ' + self.storage_type)
        search_types = [region_usagetype[self.region] + search_type_prefix + t for t in search_types]

        costs = defaultdict(float)
        for search_type in search_types:
            pricing = fetch_pricing('AmazonRDS', {
                'regionCode': self.region,
                'usagetype': search_type,
                'databaseEngine': self.database_engine,
            })

            for term in pricing['terms']['OnDemand'].values():
                for dimension in term['priceDimensions'].values():
                    c = Cost(dimension['pricePerUnit']['USD'], dimension['unit'])
                    if c.per.startswith('gb-'):
                        costs[c.per[3:]] += c.dollars * self.size
                    elif c.per.startswith('iops-'):
                        costs[c.per[5:]] += c.dollars * self.iops
                    else:
                        costs[c.per] += c.dollars

        if len(costs) == 0:
            return [Cost(0, 'Mo')]
        return [Cost(b, a) for (a, b) in costs.items()]

    @staticmethod
    def from_json(json):
        id = json['DBInstanceIdentifier']
        name = id
        # need to make a second call to client.list_tags_for_resource() to get tags
        # for tag in json.get('Tags', []):
        #     if tag['Key'] == 'Name':
        #         name = tag['Value']
        #         break
        type = json['DBInstanceClass']
        engine = json['Engine']
        state = json['DBInstanceStatus']
        az = json['AvailabilityZone']
        multi_az = json['MultiAZ']
        storage_type = json['StorageType']
        size = json['AllocatedStorage']
        iops = json.get('Iops')
        return DBInstance(id, name, type, engine, state, az, multi_az, storage_type, size, iops)


class CacheInstance(Instance):
    CLOUDWATCH_NAMESPACE = 'AWS/ElastiCache'
    ID_DIMENSION = 'CacheClusterId'  # this isn't quite right. we're ignoring CacheNodeId

    def __init__(self, id, name, type, state, region, engine):
        super().__init__(id, name, type, state, region)
        self._region = region
        self.engine = engine

    @property
    def region(self):
        return self._region

    def unit_price(self):
        search_type = region_usagetype[self.region] + 'NodeUsage:' + self.type

        pricing = fetch_pricing('AmazonElastiCache', {
            'regionCode': self.region,
            'usagetype': search_type,
            'cacheEngine': self.engine,
        })

        for term in pricing['terms']['OnDemand'].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])

    @staticmethod
    def from_json(json, region):
        id = json['CacheClusterId']
        name = id
        type = json['CacheNodeType']
        state = json['CacheClusterStatus']
        engine = json['Engine']
        return CacheInstance(id, name, type, state, region, engine)


class FargateInstance(Instance):
    CLOUDWATCH_NAMESPACE = 'AWS/ECS'
    # ID_DIMENSION doesn't work -- stat is by cluster/service not instance

    def __init__(self, id, name, cpu, memory, arch, region):
        super().__init__(id, name, f'{cpu}/{memory} ({arch})', 'running', None)
        self._region = region
        self.cpu = cpu
        self.memory = memory
        self.arch = arch

    @property
    def region(self):
        return self._region

    def unit_price(self):
        cpu_usagetype = {
            'x86_64': 'Fargate-vCPU-Hours:perCPU',
            'ARM64': 'Fargate-ARM-vCPU-Hours:perCPU',
        }
        search_type = region_usagetype[self.region] + cpu_usagetype[self.arch]
        cpu_pricing = fetch_pricing("AmazonECS", {
            'regionCode': self.region,
            'usageType': search_type,
        })
        for term in cpu_pricing['terms']['OnDemand'].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit']) * self.cpu

        memory_usagetype = {
            'x86_64': 'Fargate-GB-Hours',
            'ARM64': 'Fargate-ARM-GB-Hours',
        }
        search_type = region_usagetype[self.region] + memory_usagetype[self.arch]
        memory_pricing = fetch_pricing("AmazonECS", {
            'regionCode': self.region,
            'usageType': search_type,
        })
        for term in memory_pricing['terms']['OnDemand'].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit']) * (self.memory / 1024)

    @staticmethod
    def from_json(json, region):
        id = json['taskArn'].split('/', 1)[1]
        name = json['taskDefinitionArn'].split('/', 1)[1]
        cpu = int(json['cpu']) / 1024
        memory = int(json['memory'])
        arch = [a['value'] for a in json['attributes'] if a['name'] == 'ecs.cpu-architecture'][0]
        return FargateInstance(id, name, cpu, memory, arch, region)


class Cost:
    _factors = dict(hr=1, hrs=1, hours=1, day=24, mo=24 * 30, yr=24 * 365)

    def __init__(self, dollars, per):
        self.dollars = float(dollars)
        self.per = per.lower()

    def _convert(self, to):
        to = to.lower()
        if self.per == to:
            return self
        return Cost(self._factors[to] / self._factors[self.per] * self.dollars, to)

    def per_hour(self):
        return self._convert('Hrs')

    def per_day(self):
        return self._convert('Day')

    def per_month(self):
        return self._convert('Mo')

    def __mul__(self, other):
        return Cost(self.dollars * other, self.per)

    def __str__(self):
        if math.isnan(self.dollars):
            return '?/{}'.format(self.per)
        return '{self.dollars:.3g}/{self.per}'.format(self=self)

    def __repr__(self):
        return 'Cost({self.dollars!r}, {self.per!r})'.format(self=self)

    def __add__(self, other):
        if self.per != other.per:
            raise Exception("can't add {} and {}".format(self.per, other.per))
        return Cost(self.dollars + other.dollars, self.per)


def fetch_instance_info(client, **kwargs):
    instance_metadata = client.describe_instances(**kwargs)

    result = []
    for r in instance_metadata['Reservations']:
        for i in r['Instances']:
            result.append(EC2Instance.from_json(i))
    return result


def fetch_volume_info(client, instances):
    all_volumes = {}
    for instance in instances:
        for volume in instance.volumes:
            all_volumes[volume.id] = volume

    volumes = client.describe_volumes(VolumeIds=list(all_volumes.keys()))

    for v in volumes['Volumes']:
        volume = all_volumes[v['VolumeId']]
        volume.size = v['Size']
        volume.type = v['VolumeType']
        volume.iops = v.get('Iops')


def fetch_db_info(client, **kwargs):
    db_metadata = client.describe_db_instances(**kwargs)

    # TODO: DocumentDB instances get returned here, but use a totally different pricing model
    return [DBInstance.from_json(d) for d in db_metadata['DBInstances'] if d['Engine'] != 'docdb']


def fetch_cache_info(client, **kwargs):
    cache_metadata = client.describe_cache_clusters(**kwargs)

    result = []
    for c in cache_metadata['CacheClusters']:
        for i in range(c['NumCacheNodes']):
            result.append(CacheInstance.from_json(c, client.meta.region_name))
    return result


def fetch_fargate_info(client, **kwargs):
    result = []
    clusters = client.list_clusters(**kwargs)['clusterArns']
    for cluster in clusters:
        tasks = client.list_tasks(cluster=cluster, launchType='FARGATE')['taskArns']
        if tasks:
            tasks = client.describe_tasks(cluster=cluster, tasks=tasks)['tasks']
            for t in tasks:
                result.append(FargateInstance.from_json(t, client.meta.region_name))
    return result


def just_one(costs, per):
    """
    >>> just_one([], 'hr')
    Cost(nan, 'hr')
    >>> just_one([Cost(1, 'day')], 'hr')
    Cost(1.0, 'day')
    >>> just_one([Cost(1, 'day'), Cost(2, 'day')], 'hr')
    Cost(3.0, 'day')
    """
    if len(costs) < 1:
        return Cost(math.nan, per)
    total = costs[0]
    for c in costs[1:]:
        total += c
    return total


def build_instance_cost_table(instances, include_cpu=False, per='day'):
    headers = ('name', 'id', 'az', 'type', 'type $/' + per, 'disk GB', 'disk $/' + per, 'running $/' + per, 'state', 'actual $/' + per)
    if include_cpu:
        headers += ('avg %cpu', 'max %cpu')  # hourly, but that's too much text to put in the column heading

    def dollars(stat):
        return stat._convert(per).dollars

    def build_row(i):
        instance_cost, storage_cost, total_cost, actual_cost = i.simple_costs()
        row = (i.name, i.id, i.az, i.type, dollars(instance_cost), i.total_storage, dollars(storage_cost), dollars(total_cost), i.state, dollars(actual_cost))
        if include_cpu:
            if i.cpu_usage and len(i.cpu_usage):
                row += (round(sum(i.cpu_usage) / len(i.cpu_usage), 1), round(max(i.cpu_usage), 1))
            else:
                row += (None, None)
        return row

    return headers, [build_row(i) for i in instances]


def print_instance_cost_table(instances, total=True, tablefmt='simple', per='day'):
    include_cpu = any(i.cpu_usage for i in instances)
    cost_index = -1
    if include_cpu:
        cost_index = -3

    headers, table = build_instance_cost_table(instances, include_cpu=include_cpu, per=per)
    # cost decreasing, name increasing
    table.sort(key=lambda x: (-x[cost_index], x[0]))
    if total:
        total_row = ('Total', '', '', '', sum(r[4] for r in table), sum(r[5] for r in table), sum(r[6] for r in table),
                     sum(r[7] for r in table), '', sum(r[9] for r in table))
        if include_cpu:
            total_row += (None, None)
        table.append(total_row)
    print(tabulate(table, headers=headers, tablefmt=tablefmt))


def fetch_all_instances(region_name=None):
    with progress('fetching EC2 instances'):
        client = boto3.client('ec2', region_name=region_name)
        # instances = fetch_instance_info(Filters=[{'Name': 'tag:Environment', 'Values': ['TUS']}])
        instances = fetch_instance_info(client)
        fetch_volume_info(client, instances)
        return instances


def fetch_all_db_instances(region_name=None):
    with progress('fetching RDS instances'):
        client = boto3.client('rds', region_name=region_name)
        return fetch_db_info(client)


def fetch_all_cache_instances(region_name=None):
    with progress('fetching ElastiCache instances'):
        client = boto3.client('elasticache', region_name=region_name)
        return fetch_cache_info(client)


def fetch_all_fargate_instances(region_name=None):
    with progress('fetching Fargate instances'):
        client = boto3.client('ecs', region_name=region_name)
        return fetch_fargate_info(client)


def fetch_cpu_usage(instances, region_name=None):
    client = boto3.client('cloudwatch', region_name=region_name)
    end_time = datetime.now()
    start_time = end_time + timedelta(weeks=-1)

    for i in instances:
        i.cpu_usage = cloudwatch_cpu_usage(client, i.cloudwatch_namespace, i.cloudwatch_dimensions, start_time, end_time)


def cloudwatch_cpu_usage(client, namespace, dimensions, start_time, end_time):
    stats = client.get_metric_statistics(
        Namespace=namespace,
        MetricName='CPUUtilization',
        Dimensions=dimensions,
        StartTime=start_time,
        EndTime=end_time,
        Period=3600,  # hourly
        Statistics=['Average'],
    )
    return [p['Average'] for p in stats['Datapoints']]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--ec2', action='store_true')
    p.add_argument('--rds', action='store_true')
    p.add_argument('--elasticache', action='store_true')
    p.add_argument('--fargate', action='store_true')
    p.add_argument('--all-services', action='store_true')
    p.add_argument('--region', nargs='+', dest='regions', metavar='REGION')
    p.add_argument('--all-regions', action='store_const', const=ALL_REGIONS, dest='regions')
    p.add_argument('--tablefmt', choices=tabulate_formats)
    p.add_argument('--cpu-usage', action='store_true')  # note that this costs money; $0.01 per thousand requests
    p.add_argument('--cost-per', choices=['hr', 'day', 'mo', 'yr'], default='day')

    args = p.parse_args()

    # default to showing ec2, if nothing selected
    if not any((args.ec2, args.rds, args.elasticache, args.fargate)):
        args.ec2 = True

    all_instances = []
    for region in (args.regions or [None]):
        instances = []
        if args.ec2 or args.all_services:
            instances += fetch_all_instances(region_name=region)
        if args.rds or args.all_services:
            instances += fetch_all_db_instances(region_name=region)
        if args.elasticache or args.all_services:
            instances += fetch_all_cache_instances(region_name=region)
        if args.fargate or args.all_services:
            instances += fetch_all_fargate_instances(region_name=region)

        if args.cpu_usage:
            fetch_cpu_usage(instances, region_name=region)

        all_instances += instances

    print_instance_cost_table(all_instances, tablefmt=args.tablefmt, per=args.cost_per)


if __name__ == '__main__':
    main()
