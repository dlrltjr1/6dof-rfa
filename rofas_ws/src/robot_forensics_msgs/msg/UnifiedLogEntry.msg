# UnifiedLogEntry.msg
# 분산 로그 통합(DLU, Distributed Log Unification) 엔트리
# 6DoF-RFA 식 (1) 해시체인 엔트리 — DDS 분산통신 환경에서의 단일 시간정렬·위변조방지 로그 기본 단위

std_msgs/Header header

# ── 시퀀스 / 이중 타임스탬프 ──────────────────────────────────
uint64  seq                # 전역 단조 증가 시퀀스 (DLU 시작 시 0)
int64   monotonic_ts_ns    # CLOCK_MONOTONIC 기준 절대 시간 (ns)
int64   ros_ts_ns          # rclpy Clock now() ROS 시간 (ns)

# ── 메시지 식별 ──────────────────────────────────────────────
string  topic              # 토픽 이름 (e.g. "/joint_states")
string  msg_type           # 메시지 타입 (e.g. "sensor_msgs/msg/JointState")
string  msg_uuid           # 본 엔트리 메시지의 UUID (debug/correlation)

# ── 송신자 신원 (DDS) ─────────────────────────────────────────
string  publisher_gid      # DDS Publisher Global Identifier (16-byte hex)
                           # rclpy.subscription에서 추출, 미상 시 "UNKNOWN"
bool    publisher_known    # 정상 운용 시 관찰된 GID 집합에 포함 여부

# ── 페이로드 ─────────────────────────────────────────────────
string  payload_hash       # SHA-256 of serialized message payload (hex)
uint32  payload_bytes      # serialized 크기

# ── QoS / 드롭 추정 ──────────────────────────────────────────
int32   drop_estimate      # 직전 엔트리 이후 발생한 추정 드롭 수 (-1 = 측정 불가)
string  qos_reliability    # "BEST_EFFORT" | "RELIABLE"

# ── 해시 체인 ────────────────────────────────────────────────
string  prev_hash          # 직전 엔트리의 entry_hash (hex)
string  entry_hash         # 본 엔트리의 entry_hash (hex)
                           # = SHA256(prev_hash || seq || monotonic_ts_ns ||
                           #          topic || publisher_gid || payload_hash ||
                           #          drop_estimate)
