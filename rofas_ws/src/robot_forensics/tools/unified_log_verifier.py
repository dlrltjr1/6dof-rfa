#!/usr/bin/env python3
"""
unified_log_verifier.py
───────────────────────
6DoF-RFA — DLU 로그 체인 무결성 검증 도구

논문 4.2절 검증 도구 및 5장 위변조 시나리오 평가에 사용되는 스크립트.

기능:
  unified_log_<session>.jsonl 파일을 순회하며 각 엔트리의 SHA-256 해시 체인을 재계산하고,
  첫 불일치 지점(위변조·삭제·삽입)을 보고한다.

사용:
  python3 unified_log_verifier.py /path/to/unified_log_*.jsonl
  python3 unified_log_verifier.py --dir /tmp/rofas/unified_log
  python3 unified_log_verifier.py --check-gids /path/to/file.jsonl \\
          --baseline /tmp/rofas/unified_log/observed_gids.json
  
반환 코드:
  0  체인 무결성 PASS
  1  체인 불일치 (위변조 탐지)
  2  파일 오류
"""

import sys
import os
import json
import hashlib
import argparse
import glob

GENESIS_HASH = '0' * 64


def compute_entry_hash(entry: dict) -> str:
    """unified_logger_node._record() 와 동일한 해시 계산."""
    chain_input = '|'.join([
        entry['prev_hash'],
        str(entry['seq']),
        str(entry['monotonic_ts_ns']),
        entry['topic'],
        entry['publisher_gid'],
        entry['payload_hash'],
        str(entry['drop_estimate']),
    ])
    return hashlib.sha256(chain_input.encode('utf-8')).hexdigest()


def verify_chain(jsonl_path: str, verbose: bool = False) -> dict:
    """
    체인 무결성 검증.
    Returns: {
        'ok': bool,
        'total_entries': int,
        'first_break_at': int | None,
        'reason': str,
        'details': dict,
    }
    """
    result = {
        'file':           jsonl_path,
        'ok':             True,
        'total_entries':  0,
        'first_break_at': None,
        'reason':         '',
        'details':        {},
    }

    if not os.path.exists(jsonl_path):
        result['ok'] = False
        result['reason'] = f'파일 없음: {jsonl_path}'
        return result

    expected_prev = GENESIS_HASH
    expected_seq  = 0

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                result['ok'] = False
                result['first_break_at'] = line_no
                result['reason']  = f'JSON 파싱 오류 (line {line_no}): {e}'
                return result

            result['total_entries'] += 1

            # 1) 시퀀스 연속성
            if entry['seq'] != expected_seq:
                result['ok'] = False
                result['first_break_at'] = line_no
                result['reason']  = (
                    f'시퀀스 불연속 (line {line_no}): '
                    f'expected seq={expected_seq}, got seq={entry["seq"]} '
                    f'→ 엔트리 삭제 또는 삽입 의심'
                )
                result['details'] = {
                    'expected_seq': expected_seq,
                    'got_seq':      entry['seq'],
                }
                return result

            # 2) prev_hash 일치
            if entry['prev_hash'] != expected_prev:
                result['ok'] = False
                result['first_break_at'] = line_no
                result['reason']  = (
                    f'prev_hash 불일치 (line {line_no}, seq={entry["seq"]}): '
                    f'expected={expected_prev[:16]}..., '
                    f'got={entry["prev_hash"][:16]}...'
                )
                result['details'] = {
                    'expected_prev_hash': expected_prev,
                    'got_prev_hash':      entry['prev_hash'],
                }
                return result

            # 3) entry_hash 재계산
            recomputed = compute_entry_hash(entry)
            if recomputed != entry['entry_hash']:
                result['ok'] = False
                result['first_break_at'] = line_no
                result['reason']  = (
                    f'entry_hash 불일치 (line {line_no}, seq={entry["seq"]}, '
                    f'topic={entry["topic"]}): '
                    f'recomputed={recomputed[:16]}..., '
                    f'stored={entry["entry_hash"][:16]}... '
                    f'→ 페이로드 또는 메타데이터 위변조'
                )
                result['details'] = {
                    'topic':       entry['topic'],
                    'recomputed':  recomputed,
                    'stored':      entry['entry_hash'],
                }
                return result

            if verbose and result['total_entries'] % 1000 == 0:
                print(f'  검증 진행: {result["total_entries"]} entries OK')

            expected_prev = entry['entry_hash']
            expected_seq  = entry['seq'] + 1

    if result['ok']:
        result['reason'] = 'PASS — 체인 무결성 검증 완료'
        result['details'] = {
            'last_entry_hash': expected_prev,
            'last_seq':        expected_seq - 1,
        }

    return result


def check_publisher_gids(jsonl_path: str, baseline_path: str) -> dict:
    """관찰된 GID 중 baseline에 없는 것(즉 비인가 publisher 후보)을 식별."""
    if not os.path.exists(baseline_path):
        return {'ok': False, 'reason': f'baseline 파일 없음: {baseline_path}'}

    with open(baseline_path) as f:
        baseline = set(json.load(f).get('gids', []))

    suspicious = {}  # topic → set(gid)
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            gid = e.get('publisher_gid', 'UNKNOWN')
            if gid in ('UNKNOWN',) or gid.endswith(':MULTI'):
                # 분리 처리 가능 — 일단 의심에 포함
                topic = e.get('topic', 'unknown')
                suspicious.setdefault(topic, set()).add(gid)
                continue
            # MULTI 표기에서 첫 부분만 추출
            base_gid = gid.split(':', 1)[0]
            if base_gid not in baseline:
                topic = e.get('topic', 'unknown')
                suspicious.setdefault(topic, set()).add(gid)

    return {
        'ok': len(suspicious) == 0,
        'suspicious_publishers': {k: sorted(v) for k, v in suspicious.items()},
        'baseline_size': len(baseline),
    }


def main():
    ap = argparse.ArgumentParser(description='6DoF-RFA DLU 로그 체인 무결성 검증')
    ap.add_argument('path', nargs='?', help='JSONL 로그 파일 경로')
    ap.add_argument('--dir', help='디렉토리 내 unified_log_*.jsonl 일괄 검증')
    ap.add_argument('--verbose', '-v', action='store_true')
    ap.add_argument('--check-gids', action='store_true',
                    help='관찰된 publisher_gid 비인가 검사')
    ap.add_argument('--baseline', help='baseline observed_gids.json 경로')
    args = ap.parse_args()

    # 대상 파일 목록 결정
    if args.dir:
        files = sorted(glob.glob(os.path.join(args.dir, 'unified_log_*.jsonl')))
        if not files:
            print(f'[ERROR] 디렉토리에 JSONL 파일 없음: {args.dir}', file=sys.stderr)
            sys.exit(2)
    elif args.path:
        files = [args.path]
    else:
        ap.print_help()
        sys.exit(2)

    all_ok = True
    for fp in files:
        print(f'\n── 검증: {fp}')
        result = verify_chain(fp, verbose=args.verbose)
        status = '✅ PASS' if result['ok'] else '❌ FAIL'
        print(f'  {status} | 총 {result["total_entries"]} 엔트리')
        print(f'  사유: {result["reason"]}')
        if not result['ok']:
            all_ok = False
            if result['details']:
                for k, v in result['details'].items():
                    print(f'    {k}: {v}')

        if args.check_gids and args.baseline:
            print(f'\n  ── publisher_gid 검사 (baseline: {args.baseline})')
            gid_res = check_publisher_gids(fp, args.baseline)
            if gid_res['ok']:
                print(f'  ✅ 모든 GID가 baseline에 등록됨')
            else:
                print(f'  ⚠️  비인가/UNKNOWN GID 발견:')
                for topic, gids in gid_res.get('suspicious_publishers', {}).items():
                    print(f'    {topic}:')
                    for g in gids:
                        print(f'      - {g}')
                all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == '__main__':
    main()
