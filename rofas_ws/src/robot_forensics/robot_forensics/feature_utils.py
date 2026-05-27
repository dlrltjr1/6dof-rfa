#!/usr/bin/env python3
"""
feature_utils.py
────────────────
6DoF-RFA Analyze 단계 인프라 — CSV 파싱 및 특징 벡터 추출 유틸리티

trainer_node와 classifier_trainer_node가 공유하는 함수 모음:
  - CSV 컬럼 자동 매핑 (joint{1..6}_angle → j{1..6})
  - 관절(6) + 엔드이펙터(3) 9차원 특징 벡터 구성
  - 다수 CSV 파일 일괄 로드
"""

import os
import pandas as pd
import numpy as np

def _parse_csv(fpath):
    """단일 CSV 파일을 읽어 관절 및 엔드이펙터 데이터를 분리하는 내부 함수"""
    try:
        df = pd.read_csv(fpath)
        if len(df) == 0:
            return None
        
        # 데이터셋의 joint{i}_angle 형식을 내부 표준 j{i}로 매핑
        rename_map = {}
        for i in range(1, 7):
            old_col = f'joint{i}_angle'
            if old_col in df.columns:
                rename_map[old_col] = f'j{i}'
        if rename_map:
            df = df.rename(columns=rename_map)

        joint_cols = ['j1', 'j2', 'j3', 'j4', 'j5', 'j6']
        ee_cols = ['ee_x', 'ee_y', 'ee_z']
        
        # 존재하지 않는 컬럼 예외 처리
        for c in joint_cols + ee_cols:
            if c not in df.columns:
                df[c] = 0.0

        task_name = 'unknown'
        if 'task_label' in df.columns:
            task_name = str(df['task_label'].iloc[0])
        elif 'current_task' in df.columns:
            task_name = str(df['current_task'].iloc[0])

        return {
            'joints': df[joint_cols].values,
            'ee': df[ee_cols].values,
            'task_name': task_name
        }
    except Exception:
        return None

def load_csv_files(csv_dir):
    """디렉토리 내 모든 CSV 파일을 파싱하는 함수"""
    import glob
    files = sorted(glob.glob(os.path.join(csv_dir, '*.csv')))
    segments = []
    for fp in files:
        seg = _parse_csv(fp)
        if seg is not None:
            segments.append(seg)
    return segments

def build_feature_vector(joints, ee, mean=None, std=None):
    """관절각과 엔드이펙터 데이터를 결합하고 정규화(Z-score)를 수행하는 함수"""
    # 9차원 피처 결합: j1~j6 + ee_x~ee_z
    feat = np.hstack([joints, ee])
    
    if mean is not None and std is not None:
        # 표준편차 0 컬럼에 대한 분모 보호
        std_eps = np.where(std == 0, 1e-8, std)
        feat = (feat - mean) / std_eps
        
    return feat
