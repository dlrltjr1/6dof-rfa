#!/bin/bash
# S1~S4 변조 시나리오 자동 실행 스크립트

# 경로 설정 (필요시 수정)
LOG_FILE=$(ls /tmp/log_27033_phase1.jsonl | tail -1)
VERIFIER=$(find ~/rofas_ws -name "unified_log_verifier.py" | head -1)
BASELINE_GIDS=/tmp/rofas/unified_log/observed_gids.json

if [ -z "$LOG_FILE" ] || [ -z "$VERIFIER" ]; then
    echo "❌ 로그 파일 또는 verifier를 찾을 수 없습니다"
    echo "LOG_FILE=$LOG_FILE"
    echo "VERIFIER=$VERIFIER"
    exit 1
fi

echo "========================================="
echo "원본 로그: $LOG_FILE"
echo "Verifier:  $VERIFIER"
echo "========================================="

# 변조 대상 seq 번호 (총 253개이므로 중간쯤)
TARGET_SEQ=7500

# 4개 시나리오용 복사본 생성
cp $LOG_FILE /tmp/s1_payload.jsonl
cp $LOG_FILE /tmp/s2_delete.jsonl
cp $LOG_FILE /tmp/s3_insert.jsonl
cp $LOG_FILE /tmp/s4_baseline.jsonl

# ====== S1: 페이로드 변조 ======
echo ""
echo "========================================="
echo "[S1] 페이로드 변조 (seq=$TARGET_SEQ)"
echo "========================================="
python3 << PYEOF
import json
with open('/tmp/s1_payload.jsonl') as f: lines = f.readlines()
target_line = None
for i, line in enumerate(lines):
    e = json.loads(line)
    if e['seq'] == $TARGET_SEQ:
        target_line = i
        # payload_hash 첫 글자 변경
        orig = e['payload_hash']
        e['payload_hash'] = ('1' if orig[0] != '1' else '2') + orig[1:]
        lines[i] = json.dumps(e) + '\n'
        print(f"  변조 위치: line {i+1} (seq={$TARGET_SEQ})")
        print(f"  원본 payload_hash: {orig[:20]}...")
        print(f"  변조 payload_hash: {e['payload_hash'][:20]}...")
        break
with open('/tmp/s1_payload.jsonl', 'w') as f: f.writelines(lines)
PYEOF
echo "--- Verifier 실행 ---"
python3 $VERIFIER /tmp/s1_payload.jsonl

# ====== S2: 엔트리 삭제 ======
echo ""
echo "========================================="
echo "[S2] 엔트리 삭제 (seq=$TARGET_SEQ)"
echo "========================================="
python3 << PYEOF
import json
with open('/tmp/s2_delete.jsonl') as f: lines = f.readlines()
for i, line in enumerate(lines):
    e = json.loads(line)
    if e['seq'] == $TARGET_SEQ:
        print(f"  삭제 위치: line {i+1} (seq={$TARGET_SEQ})")
        del lines[i]
        break
with open('/tmp/s2_delete.jsonl', 'w') as f: f.writelines(lines)
PYEOF
echo "--- Verifier 실행 ---"
python3 $VERIFIER /tmp/s2_delete.jsonl

# ====== S3: 엔트리 삽입 ======
echo ""
echo "========================================="
echo "[S3] 엔트리 삽입 (seq=$TARGET_SEQ 위치)"
echo "========================================="
python3 << PYEOF
import json
with open('/tmp/s3_insert.jsonl') as f: lines = f.readlines()
for i, line in enumerate(lines):
    e = json.loads(line)
    if e['seq'] == $TARGET_SEQ:
        # 같은 위치에 가짜 엔트리 복제 삽입
        fake = json.loads(line)
        lines.insert(i, json.dumps(fake) + '\n')
        print(f"  삽입 위치: line {i+1} 직전")
        break
with open('/tmp/s3_insert.jsonl', 'w') as f: f.writelines(lines)
PYEOF
echo "--- Verifier 실행 ---"
python3 $VERIFIER /tmp/s3_insert.jsonl

# ====== S4: GID baseline 검사 ======
echo ""
echo "========================================="
echo "[S4] Publisher GID baseline 검사"
echo "========================================="
echo "--- Verifier --check-gids 실행 ---"
if [ -f "$BASELINE_GIDS" ]; then
    python3 $VERIFIER --check-gids /tmp/s4_baseline.jsonl --baseline $BASELINE_GIDS
else
    echo "baseline 파일 없음. observed_gids.json을 baseline으로 사용 시도"
    cp /tmp/rofas/unified_log/observed_gids.json /tmp/empty_baseline.json
    # 빈 baseline 생성 후 비교 (모든 GID가 비인가로 검출되어야 함)
    python3 -c "import json; json.dump({'gids': []}, open('/tmp/empty_baseline.json','w'))"
    python3 $VERIFIER --check-gids /tmp/s4_baseline.jsonl --baseline /tmp/empty_baseline.json
fi

echo ""
echo "========================================="
echo "✅ 모든 시나리오 실행 완료"
echo "========================================="
