cmake_minimum_required(VERSION 3.8)
project(robot_forensics_msgs)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)

# 6DoF-RFA DLU 노드가 사용하는 통합 로그 엔트리 메시지
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/UnifiedLogEntry.msg"
  "msg/AnomalyEvent.msg"
  "msg/ForensicSnapshot.msg"
  DEPENDENCIES std_msgs
)

ament_package()
