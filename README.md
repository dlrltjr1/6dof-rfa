# 6DoF-RFA: ROS2 기반 6축 협동 로봇팔 포렌식 프레임워크

본 저장소는 한국디지털포렌식학회 학술대회 논문 "6DoF-RFA: ROS2 기반 산업용 6축 협동
로봇팔을 위한 포렌식 프레임워크"의 구현 코드 및 실험 데이터를 포함한다.

## 디렉토리 구조

```
.
├── README.md                       # 본 파일
├── rofas_ws/                       # ROS2 워크스페이스 (코드)
│   └── src/
│       ├── robot_forensics/        # 메인 패키지
│       └── robot_forensics_msgs/   # 메시지 정의 패키지
├── tools/                          # 외부 도구 (프레임워크 외)
│   └── aihub_csv_publisher.py      # AI-Hub CSV → ROS2 토픽 재생기
└── experiment_data/                # 실험 데이터 및 산출물
    ├── aihub_csv/                  # AI-Hub 데이터셋 71937 (180 CSV: 정상 60 + 이상 120)
    ├── normal_log/                 # 정상 운영 시 DLU 통합 로그 (27,033 엔트리)
    │   ├── log_27033_phase1.jsonl  # 베이스라인 로그
    │   ├── observed_gids.json      # 관찰된 publisher GID 전체
    │   └── genesis_*.json          # 체인 시작점 메타데이터
    ├── baseline/                   # S4 검증용 정상 baseline (3개 GID)
    │   └── baseline_only_3gids.json
    ├── scenarios/                  # 위변조 시나리오 적용 결과 (논문 표 2)
    │   ├── s1_payload.jsonl        # 페이로드 변조
    │   ├── s2_delete.jsonl         # 엔트리 삭제
    │   ├── s3_insert.jsonl         # 엔트리 삽입
    │   ├── s4_baseline.jsonl       # 비인가 publisher (재가동 후 로그)
    │   └── run_tampering_test.sh   # S1~S4 자동 적용 스크립트
    └── models/                     # Analyze 단계 학습 모델 산출물
        ├── autoencoder.pt          # LSTM Autoencoder
        ├── classifier.pt           # MLP 분류기
        ├── scaler.npz              # Z-score 정규화 스케일러
        ├── model_config.json
        ├── classifier_confusion_matrix.json
        └── task_latent_centroids.npz
```

## 코드 구성요소 분류

본 코드는 논문의 검증 범위에 따라 두 그룹으로 분리된다.

**그룹 1 — 본 연구 검증 대상 (Acquire·Process 단계)**:
- `robot_forensics/unified_logger_node.py` — DLU (논문 3.3절 식 (1) 구현)
- `robot_forensics/tools/unified_log_verifier.py` — 무결성 검증 도구 (논문 4.2절)
- `tools/aihub_csv_publisher.py` — 입력 스트림 재생 (논문 5.1절)
- `robot_forensics_msgs/msg/UnifiedLogEntry.msg` — 엔트리 타입 정의

**그룹 2 — Analyze 단계 인프라 (6장 향후 연구)**:
- `robot_forensics/trainer_node.py` — LSTM Autoencoder 학습
- `robot_forensics/classifier_trainer_node.py` — MLP 분류기 학습
- `robot_forensics/models.py`, `feature_utils.py` — 보조 모듈

## 데이터셋

본 검증은 AI-Hub의 **「제조 공정 자동화 구현 로봇 티칭 데이터」**
(데이터셋 71937, Beta v1.0)를 입력으로 사용한다.

> ⚠️ **AI-Hub 이용정책상 데이터셋 재배포가 제한되어, 본 저장소에는 원본 CSV가 포함되지 않는다.**
> 재현을 위해서는 아래 절차로 AI-Hub에서 직접 다운로드해야 한다.

1. AI-Hub 데이터셋 페이지 접속:
   https://aihub.or.kr/aihubdata/data/view.do?dataSetSn=71937
2. 휴대폰 인증 및 이용 신청 (자동 승인)
3. 다운로드한 CSV 파일을 `experiment_data/aihub_csv/` 디렉토리에 배치
   - 본 검증은 `normal_0000.csv` ~ `normal_0009.csv` 10건을 순차 재생하여
     27,033개 엔트리를 생성한다.

## 의존성

- Ubuntu 22.04 LTS
- ROS2 Humble Hawksbill
- Python 3.10+
- numpy, pandas, torch (Analyze 인프라용)

## 빌드 및 실행

```bash
# 빌드
cd rofas_ws
colcon build --packages-select robot_forensics_msgs robot_forensics
source install/setup.bash

# DLU 가동 (검증 모드)
ros2 launch robot_forensics forensics_system.launch.py mode:=detect
```

## 실험 재현

```bash
# 1. DLU 가동 (터미널 1)
ros2 launch robot_forensics forensics_system.launch.py mode:=detect

# 2. AI-Hub CSV 재생 (터미널 2) — 약 96초간 27,033개 엔트리 생성
python3 tools/aihub_csv_publisher.py \
    --dir experiment_data/aihub_csv \
    --pattern "normal_*.csv" \
    --max-files 10 --rate 100

# 3. DLU 종료 후 베이스라인 검증
python3 rofas_ws/src/robot_forensics/tools/unified_log_verifier.py \
    /tmp/rofas/unified_log/unified_log_*.jsonl

# 4. 위변조 시나리오 S1~S4 자동 적용
bash experiment_data/scenarios/run_tampering_test.sh
```

## 논문에 보고된 실험 결과 (논문 표 2)

| 시나리오 | 변조 위치 | Verifier 보고 | 파일 |
|---|---|---|---|
| S1 페이로드 변조 | seq=7500 (`/joint_states`) | line 7501 entry_hash 불일치 | `scenarios/s1_payload.jsonl` |
| S2 엔트리 삭제 | seq=7500 제거 | line 7501 시퀀스 불연속 | `scenarios/s2_delete.jsonl` |
| S3 엔트리 삽입 | seq=7500 위치 삽입 | line 7502 시퀀스 비단조 역행 | `scenarios/s3_insert.jsonl` |
| S4 비인가 publisher | 미등록 GID 발행 | 토픽별 비인가 GID 보고 | `scenarios/s4_baseline.jsonl` + `baseline/baseline_only_3gids.json` |


## 인용 (Citation)

본 코드 또는 프레임워크를 인용하실 경우 아래 정보를 사용해 주십시오.

```bibtex
@inproceedings{6dof-rfa-2026,
  title     = {6DoF-RFA: A Forensic Framework for ROS2-based Industrial 6-Axis Collaborative Robots},
  author    = {[]},
  booktitle = {Proceedings of the Korean Institute of Digital Forensics Conference},
  year      = {2026}
}
```

## 라이선스

본 저장소의 코드는 [MIT License](LICENSE) 하에 배포된다.
단, `experiment_data/aihub_csv/` 의 AI-Hub 데이터셋은 AI-Hub 자체 이용정책을 따른다.
