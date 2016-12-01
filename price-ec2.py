#!/usr/bin/env python

import json
from collections import defaultdict

import boto3

client = boto3.client('ec2')


class Instance:
    def __init__(self, id, name, type, state):
        self.id = id
        self.name = name
        self.type = type
        self.state = state
        self.volumes = []

    @staticmethod
    def from_json(json):
        id = json['InstanceId']
        name = None
        for tag in json['Tags']:
            if tag['Key'] == 'Name':
                name = tag['Value']
                break
        type = json['InstanceType']
        state = json['State']['Name']
        instance = Instance(id, name, type, state)
        for mapping in json['BlockDeviceMappings']:
            instance.volumes.append(Volume(mapping['Ebs']['VolumeId']))
        return instance


class Volume:
    def __init__(self, id):
        self.id = id
        self.type = None
        self.size = None


class Cost:
    def __init__(self, dollars, per):
        self.dollars = float(dollars)
        self.per = per

    def per_day(self):
        if self.per == 'Day':
            return self
        if self.per == 'Hrs':
            return Cost(self.dollars * 24, 'Day')
        if self.per == 'Mo':
            return Cost(self.dollars / 30, 'Day')
        raise Exception("can't convert {} to Day".format(self.per))

    def __str__(self):
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


def get_instance_cost(type):
    if type == 'r4.large':
        yield from [('0.133', 'Hrs')]
    for p in ec2_offers['products'].values():
        if p['attributes'].get('instanceType') == type and p['attributes']['location'] == 'US East (N. Virginia)' and p['attributes']['operatingSystem'] == 'Linux' and p['attributes']['tenancy'] == 'Shared':
            terms = ec2_offers['terms']['OnDemand'][p['sku']]
            for term in terms.values():
                yield from [(dimension['pricePerUnit']['USD'], dimension['unit']) for dimension in term['priceDimensions'].values()]


def get_volume_cost(type):
    if type == 'io1':
        search_type = 'EBS:VolumeUsage.piops'
    else:  # gp2, st1, sc1
        search_type = 'EBS:VolumeUsage.' + type
    for p in ec2_offers['products'].values():
        if p['attributes']['usagetype'] == search_type:
            terms = ec2_offers['terms']['OnDemand'][p['sku']]
            for term in terms.values():
                yield from [(dimension['pricePerUnit']['USD'], dimension['unit']) for dimension in term['priceDimensions'].values()]


def get_total_storage_cost(instance):
    costs = defaultdict(float)
    for volume in instance.volumes:
        cost = list(get_volume_cost(volume.type))
        if len(cost) != 1:
            return '?'
        dollars, per = cost[0]
        dollars = float(dollars)
        if per.startswith('GB-'):
            dollars *= volume.size
            per = per[3:]
        costs[per] += dollars
    return [(b, a) for (a, b) in costs.items()]


def add_costs(a, b):
    def to_perday(dollars, per):
        dollars = float(dollars)
        if per == 'Day':
            return dollars
        if per == 'Hrs':
            return 24 * dollars
        if per == 'Mo':
            return dollars / 30

    if len(a) != 1 or len(b) != 1:
        return '?'
    a = a[0]
    b = b[0]
    if len(a) != 2 or len(b) != 2:
        return '?'
    return to_perday(*a) + to_perday(*b), 'Day'


def format_cost(cost):
    if len(cost) == 1:
        cost = cost[0]
    if len(cost) == 2:
        dollars, per = cost
        return '{:.3g}/{}'.format(float(dollars), per)
    return '?'  # not sure what


def print_instance_cost_table(instances):
    print('{:<10}  {:<16}  {:<10}  {:>10}  {:>8}  {:>8}  {:>8}  {:<8}'.format('id', 'name', 'type', '$', 'disk', '$', 'total $', 'state'))
    for i in instances:
        total_storage = sum(v.size for v in i.volumes)
        instance_cost = list(get_instance_cost(i.type))
        storage_cost = get_total_storage_cost(i)
        total_cost = add_costs(instance_cost, storage_cost)
        print('{0.id:<10}  {0.name:<16}  {0.type:<10}  {1:>10}  {2:>8}  {3:>8}  {4:>8}  {0.state:<8}'.format(
            i, format_cost(instance_cost),
            '{} GB'.format(total_storage), format_cost(storage_cost),
            format_cost([total_cost])))

instances = fetch_instance_info(Filters=[{'Name': 'tag:Environment', 'Values': ['TUS']}])
fetch_volume_info(instances)

instances.sort(key=lambda x: x.name)
print_instance_cost_table(instances)
