# ForensicSnapshot.msg
# Acquire 단계 사고 발생 시점 전후 데이터 스냅샷 (향후 연구용 placeholder)
std_msgs/Header header
string incident_id          # Trigger event 식별자
string[] phase_labels       # "PRE", "INCIDENT", "POST"
float64[] timestamps        # 각 스텝 타임스탬프
float64[] joint_positions   # 관절 상태 시계열 (flat)