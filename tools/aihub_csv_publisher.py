#!/usr/bin/env python3
"""
aihub_csv_publisher.py
──────────────────────
6DoF-RFA — AI-Hub 「제조 공정 자동화 구현 로봇 티칭 데이터」 ROS2 토픽 재생 도구

논문 4.3절 및 5.1절 검증 실험에 사용. AI-Hub 데이터셋 71937의 정상 trajectory CSV
파일을 ROS2 표준 메시지 타입으로 매핑하여 발행하며, DLU 노드(unified_logger_node)의
입력 스트림을 제공한다.

본 도구는 6DoF-RFA 프레임워크의 일부가 아닌 별도의 검증용 데이터 재생기로서,
프레임워크 코드(robot_forensics 패키지)를 수정 없이 유지하기 위해 분리되었다.

매핑 규약:
  CSV 컬럼            → ROS2 토픽 / 메시지 타입
  joint{1..6}_*       → /joint_states  (sensor_msgs/JointState)
  EE_x, EE_y, ...     → /end_effector_pose (geometry_msgs/PoseStamped)
  current_task        → /task_label (std_msgs/String)

사용 예:
  # 단일 CSV 1회 재생
  python3 aihub_csv_publisher.py /path/to/normal_0000.csv --rate 50

  # 디렉토리 내 normal_*.csv 10개 순차 재생
  python3 aihub_csv_publisher.py \
      --dir /tmp/rofas/data/anomaly_csv --pattern "normal_*.csv" \
      --max-files 10 --rate 100
"""

import os, sys, glob, argparse, time
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from std_msgs.msg import String
from sensor_msgs.msg import JointState
from geometry_msgs.msg import PoseStamped
import pandas as pd

class AIHubCSVPublisher(Node):
    def __init__(self, csv_files, rate_hz=100.0):
        super().__init__('aihub_csv_publisher')
        self.csv_files = csv_files
        self.dt = 1.0 / rate_hz
        qos = QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                         history=HistoryPolicy.KEEP_LAST, depth=10)
        self.pub_joints = self.create_publisher(JointState, '/joint_states', qos)
        self.pub_ee = self.create_publisher(PoseStamped, '/end_effector_pose', qos)
        self.pub_task = self.create_publisher(String, '/task_label', qos)
        self.get_logger().info(f'시작 | 파일 {len(csv_files)}개 | {rate_hz}Hz')
        self.total_msgs = 0
        self.start_ts = time.time()

    def _parse_csv(self, fpath):
        try:
            df = pd.read_csv(fpath)
            df.columns = [c.replace('\ufeff', '').strip() for c in df.columns]
            return df
        except Exception as e:
            self.get_logger().error(f'CSV 로드 실패 {fpath}: {e}')
            return None

    def _make_joint_state(self, row):
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = [f'joint{i}' for i in range(1, 7)]
        msg.position = [float(row.get(f'joint{i}_angle', 0.0)) for i in range(1, 7)]
        msg.velocity = [float(row.get(f'joint{i}_velocity', 0.0)) for i in range(1, 7)]
        msg.effort = [float(row.get(f'joint{i}_torque', 0.0)) for i in range(1, 7)]
        return msg

    def _make_ee_pose(self, row):
        msg = PoseStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'base_link'
        msg.pose.position.x = float(row.get('EE_x', 0.0))
        msg.pose.position.y = float(row.get('EE_y', 0.0))
        msg.pose.position.z = float(row.get('EE_z', 0.0))
        msg.pose.orientation.x = float(row.get('EE_rx', 0.0))
        msg.pose.orientation.y = float(row.get('EE_ry', 0.0))
        msg.pose.orientation.z = float(row.get('EE_rz', 0.0))
        msg.pose.orientation.w = 1.0
        return msg

    def _make_task_label(self, row):
        msg = String()
        msg.data = str(row.get('current_task', 'unknown'))
        return msg

    def run(self):
        try:
            for csv_idx, csv_path in enumerate(self.csv_files):
                df = self._parse_csv(csv_path)
                if df is None: continue
                fname = os.path.basename(csv_path)
                self.get_logger().info(f'[{csv_idx+1}/{len(self.csv_files)}] {fname} ({len(df)} rows)')
                for row_idx, (_, row) in enumerate(df.iterrows()):
                    if not rclpy.ok(): return
                    self.pub_joints.publish(self._make_joint_state(row))
                    self.pub_ee.publish(self._make_ee_pose(row))
                    self.pub_task.publish(self._make_task_label(row))
                    self.total_msgs += 3
                    time.sleep(self.dt)
                    if row_idx > 0 and row_idx % 100 == 0:
                        elapsed = time.time() - self.start_ts
                        self.get_logger().info(f'  {row_idx}/{len(df)} | 누적 {self.total_msgs}건 | {elapsed:.1f}s')
            elapsed = time.time() - self.start_ts
            self.get_logger().info(f'\n✅ 완료 | 총 {self.total_msgs}건 | {elapsed:.1f}초')
        except KeyboardInterrupt:
            self.get_logger().info('\n⚠️  중단')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', nargs='?')
    parser.add_argument('--dir')
    parser.add_argument('--pattern', default='normal_*.csv')
    parser.add_argument('--rate', type=float, default=100.0)
    parser.add_argument('--max-files', type=int, default=0)
    args = parser.parse_args()

    csv_files = []
    if args.csv:
        csv_files = [args.csv]
    elif args.dir:
        csv_files = sorted(glob.glob(os.path.join(args.dir, args.pattern)))
        if args.max_files > 0:
            csv_files = csv_files[:args.max_files]
    else:
        parser.error('CSV 파일 또는 --dir 옵션 필요')

    if not csv_files:
        print('❌ CSV 파일 없음'); sys.exit(1)

    print(f'📂 대상: {len(csv_files)}개')
    rclpy.init()
    node = AIHubCSVPublisher(csv_files, rate_hz=args.rate)
    try:
        node.run()
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
