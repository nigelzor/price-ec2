#!/usr/bin/env python

import boto3
import json
import math
from collections import defaultdict
from tabulate import tabulate


class Instance:
    def __init__(self, id, name, type, state, az):
        self.id = id
        self.name = name
        self.type = type
        self.state = state
        self.az = az
        self.volumes = []

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
        instance = Instance(id, name, type, state, az)
        for mapping in json['BlockDeviceMappings']:
            instance.volumes.append(Volume(mapping['Ebs']['VolumeId']))
        return instance


class Volume:
    def __init__(self, id):
        self.id = id
        self.type = None
        self.size = None


class Cost:
    _factors = dict(Hrs=1, Day=24, Mo=24*30)

    def __init__(self, dollars, per):
        self.dollars = float(dollars)
        self.per = per

    def _convert(self, to):
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


def fetch_instance_info(**kwargs):
    instance_metadata = client.describe_instances(**kwargs)

    result = []
    for r in instance_metadata['Reservations']:
        for i in r['Instances']:
            result.append(Instance.from_json(i))
    return result


def fetch_volume_info(instances):
    all_volumes = {}
    for instance in instances:
        for volume in instance.volumes:
            all_volumes[volume.id] = volume

    volumes = client.describe_volumes(VolumeIds=list(all_volumes.keys()))

    for v in volumes['Volumes']:
        volume = all_volumes[v['VolumeId']]
        volume.size = v['Size']
        volume.type = v['VolumeType']


with open('offers/ec2.json') as fh:
    ec2_offers = json.load(fh)


region_names = {
    'us-east-1': 'US East (N. Virginia)',
    'us-west-2': 'US West (Oregon)',
    'eu-west-1': 'EU (Ireland)',
}


def get_instance_cost(instance):
    type = instance.type
    location = region_names[instance.az[:-1]]
    if type == 'r4.large':
        yield Cost(0.133, 'Hrs')
        return
    for p in ec2_offers['products'].values():
        if p['attributes'].get('instanceType') == type and p['attributes']['location'] == location and p['attributes']['operatingSystem'] == 'Linux' and p['attributes']['tenancy'] == 'Shared':
            terms = ec2_offers['terms']['OnDemand'][p['sku']]
            for term in terms.values():
                yield from [Cost(dimension['pricePerUnit']['USD'], dimension['unit']) for dimension in term['priceDimensions'].values()]

region_usagetype = {
    'us-east-1': '',
    'us-west-2': 'USW2-',
    'eu-west-1': 'EU-'
}


def get_volume_cost(type, region):
    if type == 'io1':
        search_type = 'EBS:VolumeUsage.piops'
    elif type == 'standard':
        search_type = 'EBS:VolumeUsage'
    else:  # gp2, st1, sc1
        search_type = 'EBS:VolumeUsage.' + type
    search_type = region_usagetype[region] + search_type
    for p in ec2_offers['products'].values():
        if p['attributes']['usagetype'] == search_type:
            terms = ec2_offers['terms']['OnDemand'][p['sku']]
            for term in terms.values():
                yield from [Cost(dimension['pricePerUnit']['USD'], dimension['unit']) for dimension in term['priceDimensions'].values()]


def get_total_storage_cost(instance):
    costs = defaultdict(float)
    for volume in instance.volumes:
        volume_costs = list(get_volume_cost(volume.type, instance.az[:-1]))
        for c in volume_costs:
            if c.per.startswith('GB-'):
                costs[c.per[3:]] += c.dollars * volume.size
            else:
                costs[c.per] += c.dollars
    if len(costs) == 0:
        return [Cost(0, 'Mo')]
    return [Cost(b, a) for (a, b) in costs.items()]


def just_one(costs, per):
    if len(costs) != 1:
        return Cost(math.nan, per)
    return costs[0]


headers = ('name', 'id', 'az', 'type', 'type $/hr', 'disk GB', 'disk $/mo', 'running $/day', 'state', 'actual $/day')
def build_instance_cost_table(instances):
    for i in instances:
        total_storage = sum(v.size for v in i.volumes)
        instance_cost = just_one(list(get_instance_cost(i)), 'Hrs')
        storage_cost = just_one(get_total_storage_cost(i), 'Mo')
        total_cost = instance_cost.per_day() + storage_cost.per_day()
        if i.state == 'running':
            actual_cost = total_cost
        else:
            actual_cost = storage_cost
        yield (i.name, i.id, i.az, i.type, instance_cost.per_hour().dollars, total_storage, storage_cost.per_month().dollars, total_cost.per_day().dollars, i.state, actual_cost.per_day().dollars)


def print_instance_cost_table(instances):
    table = list(build_instance_cost_table(instances))
    # cost decreasing, name increasing
    table.sort(key=lambda x: (-x[-1], x[0]))
    print(tabulate(table, headers=headers))

instances = []
for region in ['us-east-1', 'us-west-2', 'eu-west-1']:
    client = boto3.client('ec2', region_name=region)
    # instances = fetch_instance_info(Filters=[{'Name': 'tag:Environment', 'Values': ['TUS']}])
    region_instances = fetch_instance_info()
    fetch_volume_info(region_instances)
    instances += region_instances

instances.sort(key=lambda x: x.name)
print_instance_cost_table(instances)
