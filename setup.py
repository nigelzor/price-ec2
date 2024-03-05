from setuptools import setup

setup(
    name='price-ec2',
    version='0.0.1',
    py_modules=['price-ec2'],
    install_requires=[
        'boto3',
        'cached-property',
        'tabulate',
        'xdg',
    ],
    entry_points={
        'console_scripts': [
            'price-ec2=price_ec2:main',
        ]
    }
)
