from setuptools import find_packages, setup
import os
from glob import glob

package_name = 'robot_forensics'

setup(
    name=package_name,
    version='1.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'), glob('launch/*.launch.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='6DoF-RFA',
    maintainer_email='maintainer@todo.todo',
    description='6DoF-RFA: ROS2 기반 6축 협동 로봇팔 포렌식 프레임워크',
    license='TODO',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            # 핵심: DLU (3.3절)
            'unified_logger_node = robot_forensics.unified_logger_node:main',
            # Analyze 단계 인프라 (6장 향후 연구)
            'trainer_node = robot_forensics.trainer_node:main',
            'classifier_trainer_node = robot_forensics.classifier_trainer_node:main',
        ],
    },
)
