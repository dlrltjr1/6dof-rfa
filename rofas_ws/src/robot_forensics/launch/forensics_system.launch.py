"""
forensics_system.launch.py
──────────────────────────
6DoF-RFA — 통합 실행 런치 파일

실행 모드:
  detect : DLU(unified_logger_node) 단독 가동 — 본 연구 검증의 핵심 모드
  train  : Analyze 인프라 학습 (LSTM AE + MLP) — 향후 연구용
  full   : detect + train 동시 가동

사용 예:
  ros2 launch robot_forensics forensics_system.launch.py mode:=detect
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, LogInfo
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch.conditions import IfCondition
from launch_ros.actions import Node


def generate_launch_description():

    # ── 공통 인자 설정 ──────────────────────────────────────────
    mode_arg = DeclareLaunchArgument(
        'mode', default_value='detect',
        description='실행 모드: detect | train | full',
    )
    data_dir = DeclareLaunchArgument(
        'data_dir', default_value='/tmp/rofas/data',
        description='학습용 데이터 루트 (csv 하위)',
    )
    model_dir = DeclareLaunchArgument(
        'model_dir', default_value='/tmp/rofas/models',
        description='Analyze 단계 학습 모델 저장 경로',
    )
    dlu_dir = DeclareLaunchArgument(
        'dlu_dir', default_value='/tmp/rofas/unified_log',
        description='DLU 통합 로그 저장 경로',
    )

    mode = LaunchConfiguration('mode')

    # ── 1. DLU 노드 (detect, full): 6DoF-RFA 핵심 ────────────────
    n_dlu = Node(
        package='robot_forensics',
        executable='unified_logger_node',
        name='unified_logger_node',
        output='screen',
        parameters=[{
            'log_dir': LaunchConfiguration('dlu_dir'),
            'topics_to_log': [
                '/joint_states',
                '/end_effector_pose',
                '/task_label',
                '/rofas/recon_error',
                '/rofas/training_done',
                '/rofas/anomaly_event',
                '/rofas/forensic_snapshot', 
            ],
            'msg_types': [
                'sensor_msgs/msg/JointState',
                'geometry_msgs/msg/PoseStamped',
                'std_msgs/msg/String',
                'std_msgs/msg/Float64',
                'std_msgs/msg/Bool',
                'robot_forensics_msgs/msg/AnomalyEvent', 
                'robot_forensics_msgs/msg/ForensicSnapshot',
            ],
            'rotate_size_mb':  100,
            'gid_refresh_hz':  2.0,
            'publish_entries': True,
        }],
        condition=IfCondition(PythonExpression(["'", mode, "' in ['detect', 'full']"])),
    )

    # ── 2. LSTM Autoencoder 학습 (train, full): Analyze 인프라 ──
    n_trainer = Node(
        package='robot_forensics',
        executable='trainer_node',
        name='trainer_node',
        output='screen',
        parameters=[{
            'csv_dir':    [LaunchConfiguration('data_dir'), '/csv'],
            'model_dir':  LaunchConfiguration('model_dir'),
            'epochs':     20,
            'batch_size': 32,
        }],
        condition=IfCondition(PythonExpression(["'", mode, "' in ['train', 'full']"])),
    )

    # ── 3. MLP 분류기 학습 (train, full): Analyze 인프라 ────────
    n_classifier_trainer = Node(
        package='robot_forensics',
        executable='classifier_trainer_node',
        name='classifier_trainer_node',
        output='screen',
        parameters=[{
            'csv_dir_normal':  [LaunchConfiguration('data_dir'), '/csv'],
            'csv_dir_anomaly': [LaunchConfiguration('data_dir'), '/anomaly_csv'],
            'model_dir':       LaunchConfiguration('model_dir'),
            'epochs':          30,
        }],
        condition=IfCondition(PythonExpression(["'", mode, "' in ['train', 'full']"])),
    )

    return LaunchDescription([
        mode_arg,
        data_dir,
        model_dir,
        dlu_dir,
        LogInfo(msg=['[6DoF-RFA] 실행 모드: ', mode]),
        n_dlu,
        n_trainer,
        n_classifier_trainer,
    ])
