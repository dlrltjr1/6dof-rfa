# AnomalyEvent.msg
# Trigger 단계 자동 발동 이벤트 (향후 연구용 placeholder)
std_msgs/Header header
string event_type           # "torque_spike", "trajectory_dev" 등
float64 severity            # 0.0~1.0
string source_topic         # 이상이 감지된 토픽
string description          # 자유 텍스트