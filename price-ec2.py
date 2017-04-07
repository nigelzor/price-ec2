#!/usr/bin/env python
import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta

import boto3
from cached_property import cached_property
from tabulate import tabulate, tabulate_formats

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


class Offers:
    @cached_property
    def ec2(self):
        with open('offers/v1.0/aws/AmazonEC2/current/index.json') as fh:
            return json.load(fh)

    @cached_property
    def rds(self):
        with open('offers/v1.0/aws/AmazonRDS/current/index.json') as fh:
            return json.load(fh)

    @cached_property
    def elasticache(self):
        with open('offers/v1.0/aws/AmazonElastiCache/current/index.json') as fh:
            return json.load(fh)


offers = Offers()


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

    def __init__(self, id, name, type, state, az):
        super().__init__(id, name, type, state, az)
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

        skus = [p['sku'] for p in offers.ec2['products'].values() if p['attributes']['usagetype'] == search_type and p['attributes']['operatingSystem'] == 'Linux']
        if len(skus) != 1:
            raise Exception('found {} skus for {} in {} (expected 1)'.format(len(skus), self.type, self.region))

        for term in offers.ec2['terms']['OnDemand'][skus[0]].values():
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
        instance = EC2Instance(id, name, type, state, az)
        for mapping in json['BlockDeviceMappings']:
            instance.volumes.append(Volume(mapping['Ebs']['VolumeId']))
        return instance


class Volume:
    def __init__(self, id):
        self.id = id
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

        skus = [p['sku'] for p in offers.ec2['products'].values() if p['attributes']['usagetype'] in search_types]
        if len(skus) != len(search_types):
            raise Exception('found {} skus for {} in {} (expected {})'.format(len(skus), self.type, region, len(search_types)))

        for sku in skus:
            for term in offers.ec2['terms']['OnDemand'][sku].values():
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

    def unit_price(self):
        if self.multi_az:
            search_type = region_usagetype[self.region] + 'Multi-AZUsage:' + self.type
        else:
            search_type = region_usagetype[self.region] + 'InstanceUsage:' + self.type

        if self.engine == 'postgres':
            search_engine = 'PostgreSQL'
        elif self.engine == 'mysql':
            search_engine = 'MySQL'
        else:
            search_engine = self.engine

        skus = [p['sku'] for p in offers.rds['products'].values() if p['attributes']['usagetype'] == search_type and p['attributes']['databaseEngine'] == search_engine]
        if len(skus) != 1:
            raise Exception('found {} skus for {} {} in {} (expected 1)'.format(len(skus), self.type, self.engine, self.region))

        for term in offers.rds['terms']['OnDemand'][skus[0]].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])

    @cached_property
    def storage_costs(self):
        return self._storage_costs()

    def _storage_costs(self, override_region=None):
        region = override_region or self.region
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
        search_types = [region_usagetype[region] + search_type_prefix + t for t in search_types]

        skus = [p['sku'] for p in offers.rds['products'].values() if p['attributes']['usagetype'] in search_types]
        # most regions are missing storage costs (!)
        # only ca-central-1, us-east-2, and eu-west-2 show up in the json
        if len(skus) == 0 and override_region is None:
            if region[:3] == 'us-':
                return self._storage_costs(override_region='us-east-2')
            if region[:3] == 'eu-':
                return self._storage_costs(override_region='eu-west-2')
            return [Cost(math.nan, 'Mo')]
        if len(skus) != len(search_types):
            raise Exception('found {} skus for {}'.format(len(skus), search_types))

        costs = defaultdict(float)
        for sku in skus:
            for term in offers.rds['terms']['OnDemand'][sku].values():
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

        skus = [p['sku'] for p in offers.elasticache['products'].values() if p['attributes']['usagetype'] == search_type and p['attributes']['cacheEngine'].lower() == self.engine]
        if len(skus) != 1:
            raise Exception('found {} skus for {} in {} (expected 1)'.format(len(skus), self.type, self.region))

        for term in offers.elasticache['terms']['OnDemand'][skus[0]].values():
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


class Cost:
    _factors = dict(hrs=1, day=24, mo=24*30)

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

    return [DBInstance.from_json(d) for d in db_metadata['DBInstances']]


def fetch_cache_info(client, **kwargs):
    cache_metadata = client.describe_cache_clusters(**kwargs)

    result = []
    for c in cache_metadata['CacheClusters']:
        for i in range(c['NumCacheNodes']):
            result.append(CacheInstance.from_json(c, client.meta.region_name))
    return result


def just_one(costs, per):
    if len(costs) != 1:
        return Cost(math.nan, per)
    return costs[0]


def build_instance_cost_table(instances, include_cpu=False):
    headers = ('name', 'id', 'az', 'type', 'type $/day', 'disk GB', 'disk $/day', 'running $/day', 'state', 'actual $/day')
    if include_cpu:
        headers += ('avg %cpu', 'max %cpu')  # hourly, but that's too much text to put in the column heading

    def build_row(i):
        instance_cost, storage_cost, total_cost, actual_cost = i.simple_costs()
        row = (i.name, i.id, i.az, i.type, instance_cost.per_day().dollars, i.total_storage, storage_cost.per_day().dollars, total_cost.per_day().dollars, i.state, actual_cost.per_day().dollars)
        if include_cpu:
            if i.cpu_usage and len(i.cpu_usage):
                row += (round(sum(i.cpu_usage) / len(i.cpu_usage), 1), round(max(i.cpu_usage), 1))
            else:
                row += (None, None)
        return row

    return headers, [build_row(i) for i in instances]


def print_instance_cost_table(instances, total=True, tablefmt='simple'):
    include_cpu = any(i.cpu_usage for i in instances)
    cost_index = -1
    if include_cpu:
        cost_index = -3

    headers, table = build_instance_cost_table(instances, include_cpu=include_cpu)
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
    client = boto3.client('ec2', region_name=region_name)
    # instances = fetch_instance_info(Filters=[{'Name': 'tag:Environment', 'Values': ['TUS']}])
    instances = fetch_instance_info(client)
    fetch_volume_info(client, instances)
    return instances


def fetch_all_db_instances(region_name=None):
    client = boto3.client('rds', region_name=region_name)
    return fetch_db_info(client)


def fetch_all_cache_instances(region_name=None):
    client = boto3.client('elasticache', region_name=region_name)
    return fetch_cache_info(client)


def print_breakdown(instances):
    count = defaultdict(int)
    cost = defaultdict(float)
    for i in instances:
        instance_cost, storage_cost, total_cost, actual_cost = i.simple_costs()

        m = re.search('(...)\d?(.+?)\d*$', i.name)
        if m:
            env, cat = m.groups()
            count[(env, cat)] += 1
            cost[(env, cat)] += actual_cost.per_day().dollars

    rows = [(k[0], k[1], count[k], cost[k]) for k in count.keys()]
    rows.sort(key=lambda x: (-x[-1], x[0], x[1]))
    print(tabulate(rows))


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
    p.add_argument('--all-services', action='store_true')
    p.add_argument('--region', nargs='+', dest='regions', metavar='REGION')
    p.add_argument('--all-regions', action='store_const', const=ALL_REGIONS, dest='regions')
    p.add_argument('--tablefmt', choices=tabulate_formats)
    p.add_argument('--cpu-usage', action='store_true')  # note that this costs money; $0.01 per thousand requests

    args = p.parse_args()

    # default to showing ec2, if nothing selected
    if not any((args.ec2, args.rds, args.elasticache)):
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

        if args.cpu_usage:
            fetch_cpu_usage(instances, region_name=region)

        all_instances += instances

    print_instance_cost_table(all_instances, tablefmt=args.tablefmt)


if __name__ == '__main__':
    main()
