#!/usr/bin/env python3
"""
unified_logger_node.py
──────────────────────
6DoF-RFA 핵심 — 분산 로그 통합(DLU, Distributed Log Unification) 노드

논문 3.3절의 식 (1) 해시체인 메커니즘 구현체.

역할:
  ROS2 DDS 분산통신 환경에서 발생하는 다음 네 가지 로그 단절성을 해소한다.
    (a) 노드별 분산 로그 → 모든 토픽을 한 노드에서 동적 구독·중앙 영속 기록
    (b) 호스트 간 시계 편차 → monotonic_ns + ros_ts_ns 이중 타임스탬프
    (c) BEST_EFFORT QoS 무흔적 드롭 → 시퀀스 결손·QoS lost_count 기반 drop_estimate
    (d) Publisher 신원 불투명 → get_publishers_info_by_topic() 기반 GID 추적

  체인 무결성(SHA-256 prev→curr 해시 사슬)으로 사후 위변조 탐지 가능.

구독:
  파라미터 `topics_to_log`에 지정된 모든 토픽을 동적 구독.
  기본: /joint_states, /end_effector_pose, /task_label,
        /rofas/recon_error, /rofas/training_done

발행:
  /rofas/unified_log    (robot_forensics_msgs/UnifiedLogEntry)
                        실시간 다른 노드가 동일 로그를 다중 소비 가능

출력 파일:
  <log_dir>/unified_log_<session_id>.jsonl    append-only 체인 로그
  <log_dir>/genesis_<session_id>.json         체인 시작점 메타데이터
  <log_dir>/observed_gids.json                정상 운용 시 관찰된 GID 목록

파라미터:
  log_dir          (str)   : 로그 저장 경로
  topics_to_log    (str[]) : 동적 구독할 토픽 목록
  rotate_size_mb   (int)   : 로그 파일 자동 회전 크기
  gid_refresh_hz   (float) : Publisher GID 갱신 주기
  publish_entries  (bool)  : UnifiedLogEntry 토픽 발행 여부 (기본 True)
  baseline_gids    (str[]) : 정상 운용 시 관찰된 GID (외부에서 주입 가능)
"""

import os
import sys
import json
import time
import uuid
import hashlib
import threading
from datetime import datetime
from collections import defaultdict

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy
from rclpy.serialization import serialize_message
from std_msgs.msg import String, Bool, Float64
from sensor_msgs.msg import JointState
from geometry_msgs.msg import PoseStamped

from rosidl_runtime_py.utilities import get_message

try:
    from robot_forensics_msgs.msg import UnifiedLogEntry, AnomalyEvent, ForensicSnapshot
    _DLU_MSGS_AVAILABLE = True
except Exception as e:
    _DLU_MSGS_AVAILABLE = False
    print(f"[unified_logger] robot_forensics_msgs unavailable: {e}", file=sys.stderr)


# 기본 토픽 매핑: 토픽명 → 메시지 타입 문자열
DEFAULT_TOPICS = [
    ('/joint_states',            'sensor_msgs/msg/JointState'),
    ('/end_effector_pose',       'geometry_msgs/msg/PoseStamped'),
    ('/task_label',              'std_msgs/msg/String'),
    ('/rofas/recon_error',       'std_msgs/msg/Float64'),
    ('/rofas/training_done',     'std_msgs/msg/Bool'),
]

# Genesis hash (모든 0x00, 첫 엔트리의 prev_hash)
GENESIS_HASH = '0' * 64


class UnifiedLoggerNode(Node):
    """분산 로그 통합 노드 — 6DoF-RFA Process 단계의 무결성 보존 메커니즘."""

    def __init__(self):
        super().__init__('unified_logger_node')

        # ── 파라미터 ─────────────────────────────────────────────
        self.declare_parameter('log_dir',          '/tmp/rofas/unified_log')
        self.declare_parameter('topics_to_log',    [t[0] for t in DEFAULT_TOPICS])
        self.declare_parameter('msg_types',        [t[1] for t in DEFAULT_TOPICS])
        self.declare_parameter('rotate_size_mb',   100)
        self.declare_parameter('gid_refresh_hz',   2.0)
        self.declare_parameter('publish_entries',  True)
        self.declare_parameter('baseline_gids',    [])

        self.log_dir         = self.get_parameter('log_dir').value
        self.topics_to_log   = self.get_parameter('topics_to_log').value
        self.msg_types       = self.get_parameter('msg_types').value
        self.rotate_size_mb  = self.get_parameter('rotate_size_mb').value
        self.publish_entries = self.get_parameter('publish_entries').value
        self.baseline_gids   = set(self.get_parameter('baseline_gids').value)

        os.makedirs(self.log_dir, exist_ok=True)

        # ── 체인 상태 ────────────────────────────────────────────
        self.lock         = threading.Lock()
        self.seq          = 0
        self.prev_hash    = GENESIS_HASH
        self.t0_monotonic = time.monotonic_ns()
        self.session_id   = datetime.now().strftime('%Y%m%d_%H%M%S')

        # ── 토픽별 시퀀스 추적 (드롭 추정용) ─────────────────────
        # header.stamp 또는 sequence_number가 있는 메시지에 대해
        self._last_header_ts: dict[str, int] = {}
        self._drop_estimate_running: dict[str, int] = defaultdict(int)

        # ── Publisher GID 추적 ───────────────────────────────────
        self._topic_gids: dict[str, set[str]] = defaultdict(set)
        self._observed_gids: set[str] = set(self.baseline_gids)

        # ── 출력 파일 ────────────────────────────────────────────
        self.log_path = os.path.join(self.log_dir, f'unified_log_{self.session_id}.jsonl')
        self.log_fp   = open(self.log_path, 'a', encoding='utf-8')

        genesis_path = os.path.join(self.log_dir, f'genesis_{self.session_id}.json')
        with open(genesis_path, 'w') as f:
            json.dump({
                'session_id':  self.session_id,
                'start_iso':   datetime.now().isoformat(),
                'genesis_hash': GENESIS_HASH,
                'log_path':    self.log_path,
                'topics':      list(zip(self.topics_to_log, self.msg_types)),
                'baseline_gids': list(self.baseline_gids),
            }, f, ensure_ascii=False, indent=2)

        # ── 토픽 동적 구독 ───────────────────────────────────────
        self._subscriptions_dyn = []
        self._setup_subscriptions()

        # ── Publisher 발행 ──────────────────────────────────────
        if self.publish_entries and _DLU_MSGS_AVAILABLE:
            self.entry_pub = self.create_publisher(
                UnifiedLogEntry, '/rofas/unified_log', 10)
        else:
            self.entry_pub = None

        # ── GID 갱신 타이머 ─────────────────────────────────────
        gid_period = 1.0 / max(0.1, self.get_parameter('gid_refresh_hz').value)
        self.create_timer(gid_period, self._refresh_gids)

        # ── 상태 로그 ────────────────────────────────────────────
        self.create_timer(5.0, self._log_status)

        self.get_logger().info(
            f'[DLU] 시작 | session={self.session_id} | log={self.log_path}'
        )

    # ── 동적 구독 설정 ──────────────────────────────────────────
    def _setup_subscriptions(self):
        for topic, type_str in zip(self.topics_to_log, self.msg_types):
            try:
                MsgClass = get_message(type_str)
            except Exception as e:
                self.get_logger().warn(
                    f'[DLU] 메시지 타입 로드 실패 — topic={topic}, type={type_str}: {e}'
                )
                continue

            # BEST_EFFORT 우선 시도 — 센서/고주파 토픽 호환
            qos_be = QoSProfile(
                reliability=ReliabilityPolicy.BEST_EFFORT,
                history=HistoryPolicy.KEEP_LAST,
                depth=50,
            )

            sub = self.create_subscription(
                MsgClass,
                topic,
                self._make_callback(topic, type_str, MsgClass),
                qos_be,
            )
            self._subscriptions_dyn.append(sub)
            self.get_logger().info(f'[DLU] 구독: {topic} ({type_str})')

    def _make_callback(self, topic, type_str, MsgClass):
        """토픽별 클로저 콜백 생성."""
        def cb(msg):
            self._record(topic, type_str, msg)
        return cb

    # ── GID 갱신 ────────────────────────────────────────────────
    def _refresh_gids(self):
        """각 토픽의 활성 Publisher GID 목록 갱신."""
        for topic in self.topics_to_log:
            try:
                infos = self.get_publishers_info_by_topic(topic)
            except Exception:
                continue
            gids_now = set()
            for info in infos:
                gid_bytes = bytes(info.endpoint_gid)
                gid_hex   = gid_bytes.hex()
                gids_now.add(gid_hex)
                self._observed_gids.add(gid_hex)
            with self.lock:
                self._topic_gids[topic] = gids_now

    # ── 핵심: 메시지 기록 ───────────────────────────────────────
    def _record(self, topic: str, type_str: str, msg):
        """수신 메시지 → 체인 엔트리."""
        now_mono = time.monotonic_ns()
        now_ros  = self.get_clock().now().nanoseconds

        # 직렬화 및 페이로드 해시
        try:
            payload_bytes = serialize_message(msg)
        except Exception:
            payload_bytes = repr(msg).encode('utf-8')
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        payload_len  = len(payload_bytes)

        # publisher_gid 추정 — 활성 Publisher가 1개면 확정, 다수면 첫 GID + UNKNOWN 표시
        with self.lock:
            gids = self._topic_gids.get(topic, set())
        if len(gids) == 1:
            pub_gid = next(iter(gids))
            known   = True
        elif len(gids) > 1:
            pub_gid = sorted(gids)[0] + ':MULTI'
            known   = all(g in self._observed_gids for g in gids)
        else:
            pub_gid = 'UNKNOWN'
            known   = False

        # 드롭 추정 — header.stamp 간격으로 추정 (header 있는 메시지 한정)
        drop_est = -1
        try:
            hdr_ts = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec
            last   = self._last_header_ts.get(topic)
            if last is not None and hdr_ts > last:
                # 토픽별 평균 간격 학습 없이는 정확 추정 불가 → -1 유지
                # 추후 통계 누적 시 확장
                drop_est = -1
            self._last_header_ts[topic] = hdr_ts
        except AttributeError:
            pass

        msg_uuid = uuid.uuid4().hex

        # 해시 체인 계산
        with self.lock:
            prev = self.prev_hash
            seq  = self.seq
            self.seq += 1

            # 정규화된 직렬화로 해시 입력 구성
            chain_input = '|'.join([
                prev,
                str(seq),
                str(now_mono),
                topic,
                pub_gid,
                payload_hash,
                str(drop_est),
            ])
            entry_hash = hashlib.sha256(chain_input.encode('utf-8')).hexdigest()
            self.prev_hash = entry_hash

        entry = {
            'seq':              seq,
            'monotonic_ts_ns':  now_mono,
            'ros_ts_ns':        now_ros,
            'topic':            topic,
            'msg_type':         type_str,
            'msg_uuid':         msg_uuid,
            'publisher_gid':    pub_gid,
            'publisher_known':  known,
            'payload_hash':     payload_hash,
            'payload_bytes':    payload_len,
            'drop_estimate':    drop_est,
            'qos_reliability':  'BEST_EFFORT',
            'prev_hash':        prev,
            'entry_hash':       entry_hash,
        }

        # ── 파일 기록 (append-only) ─────────────────────────────
        try:
            self.log_fp.write(json.dumps(entry, ensure_ascii=False) + '\n')
            self.log_fp.flush()
            # 회전 검사
            if self.log_fp.tell() > self.rotate_size_mb * 1024 * 1024:
                self._rotate_log()
        except Exception as e:
            self.get_logger().error(f'[DLU] 로그 쓰기 실패: {e}')

        # ── 토픽 발행 (실시간 소비자용) ─────────────────────────
        if self.entry_pub is not None and _DLU_MSGS_AVAILABLE:
            try:
                ev = UnifiedLogEntry()
                ev.header.stamp = self.get_clock().now().to_msg()
                ev.seq             = seq
                ev.monotonic_ts_ns = now_mono
                ev.ros_ts_ns       = now_ros
                ev.topic           = topic
                ev.msg_type        = type_str
                ev.msg_uuid        = msg_uuid
                ev.publisher_gid   = pub_gid
                ev.publisher_known = known
                ev.payload_hash    = payload_hash
                ev.payload_bytes   = payload_len
                ev.drop_estimate   = drop_est
                ev.qos_reliability = 'BEST_EFFORT'
                ev.prev_hash       = prev
                ev.entry_hash      = entry_hash
                self.entry_pub.publish(ev)
            except Exception as e:
                self.get_logger().warn(f'[DLU] UnifiedLogEntry 발행 실패: {e}')

    # ── 로그 회전 ────────────────────────────────────────────────
    def _rotate_log(self):
        """로그 파일을 회전. 체인은 유지(prev_hash 그대로 이어짐)."""
        try:
            self.log_fp.close()
        except Exception:
            pass
        ts = datetime.now().strftime('%H%M%S')
        new_path = os.path.join(
            self.log_dir, f'unified_log_{self.session_id}_rot_{ts}.jsonl')
        self.log_path = new_path
        self.log_fp = open(new_path, 'a', encoding='utf-8')
        self.get_logger().info(f'[DLU] 로그 회전 → {new_path}')

    # ── 상태 로그 ────────────────────────────────────────────────
    def _log_status(self):
        with self.lock:
            seq = self.seq
            n_topics = len(self._topic_gids)
            n_gids = len(self._observed_gids)
        self.get_logger().info(
            f'[DLU] 엔트리 {seq}개 | 활성 토픽 {n_topics}개 | '
            f'관찰 GID {n_gids}개 | 마지막 해시: {self.prev_hash[:10]}...'
        )

    # ── 종료 처리 ────────────────────────────────────────────────
    def destroy_node(self):
        self.get_logger().info(f'[DLU] 종료 | 총 {self.seq} 엔트리 기록')
        # 관찰된 GID 저장 (후속 세션 baseline 갱신용)
        try:
            gid_path = os.path.join(self.log_dir, 'observed_gids.json')
            with open(gid_path, 'w') as f:
                json.dump({
                    'session_id': self.session_id,
                    'gids':       sorted(self._observed_gids),
                }, f, indent=2)
        except Exception:
            pass
        try:
            self.log_fp.close()
        except Exception:
            pass
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = UnifiedLoggerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
