#!/usr/bin/env python

import json
import math
from collections import defaultdict

import boto3
from cached_property import cached_property
from tabulate import tabulate

with open('offers/ec2.json') as fh:
    ec2_offers = json.load(fh)

# is there any way to get this from boto?
region_usagetype = {
    'us-east-1': '',
    'us-west-2': 'USW2-',
    'eu-west-1': 'EU-'
}


class Instance:
    def __init__(self, id, name, type, state, az):
        self.id = id
        self.name = name
        self.type = type
        self.state = state
        self.az = az
        self.volumes = []

    @property
    def region(self):
        return self.az[:-1]

    def unit_price(self):
        if self.type == 'r4.large':
            yield Cost(0.133, 'Hrs')
            return
        if self.type == 'm1.small':
            search_type = region_usagetype[self.region] + 'BoxUsage'
        else:
            search_type = region_usagetype[self.region] + 'BoxUsage:' + self.type

        skus = [p['sku'] for p in ec2_offers['products'].values() if p['attributes']['usagetype'] == search_type and p['attributes']['operatingSystem'] == 'Linux']
        if len(skus) != 1:
            raise Exception('found {} skus for {} in {} (expected 1)'.format(len(skus), self.type, self.region))

        for term in ec2_offers['terms']['OnDemand'][skus[0]].values():
            for dimension in term['priceDimensions'].values():
                yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])

    @cached_property
    def instance_costs(self):
        return list(self.unit_price())

    @cached_property
    def storage_costs(self):
        costs = defaultdict(float)
        for volume in self.volumes:
            volume_costs = list(volume.unit_price(self.region))
            for c in volume_costs:
                if c.per.startswith('GB-'):
                    costs[c.per[3:]] += c.dollars * volume.size
                elif c.per.startswith('IOPS-'):
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
        instance = Instance(id, name, type, state, az)
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

        skus = [p['sku'] for p in ec2_offers['products'].values() if p['attributes']['usagetype'] in search_types]
        if len(skus) != len(search_types):
            raise Exception('found {} skus for {} in {} (expected {})'.format(len(skus), self.type, region, len(search_types)))

        for sku in skus:
            for term in ec2_offers['terms']['OnDemand'][sku].values():
                for dimension in term['priceDimensions'].values():
                    yield Cost(dimension['pricePerUnit']['USD'], dimension['unit'])


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


def fetch_instance_info(client, **kwargs):
    instance_metadata = client.describe_instances(**kwargs)

    result = []
    for r in instance_metadata['Reservations']:
        for i in r['Instances']:
            result.append(Instance.from_json(i))
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


def just_one(costs, per):
    if len(costs) != 1:
        return Cost(math.nan, per)
    return costs[0]


def build_instance_cost_table(instances):
    headers = ('name', 'id', 'az', 'type', 'type $/hr', 'disk GB', 'disk $/mo', 'running $/day', 'state', 'actual $/day')

    def build_row(i):
        total_storage = sum(v.size for v in i.volumes)
        instance_cost = just_one(i.instance_costs, 'Hrs')
        storage_cost = just_one(i.storage_costs, 'Mo')
        total_cost = instance_cost.per_day() + storage_cost.per_day()
        if i.state == 'running':
            actual_cost = total_cost
        else:
            actual_cost = storage_cost
        return i.name, i.id, i.az, i.type, instance_cost.per_hour().dollars, total_storage, storage_cost.per_month().dollars, total_cost.per_day().dollars, i.state, actual_cost.per_day().dollars

    return headers, [build_row(i) for i in instances]


def print_instance_cost_table(instances):
    headers, table = build_instance_cost_table(instances)
    # cost decreasing, name increasing
    table.sort(key=lambda x: (-x[-1], x[0]))
    print(tabulate(table, headers=headers))


def fetch_all_instances():
    instances = []
    for region in ['us-east-1', 'us-west-2', 'eu-west-1']:
        client = boto3.client('ec2', region_name=region)
        # instances = fetch_instance_info(Filters=[{'Name': 'tag:Environment', 'Values': ['TUS']}])
        region_instances = fetch_instance_info(client)
        fetch_volume_info(client, region_instances)
        instances += region_instances

    instances.sort(key=lambda x: x.name)
    return instances


def main():
    instances = fetch_all_instances()
    print_instance_cost_table(instances)

if __name__ == '__main__':
    main()
