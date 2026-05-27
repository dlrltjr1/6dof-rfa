#!/usr/bin/env python3
"""
classifier_trainer_node.py
──────────────────────────
6DoF-RFA Analyze 단계 인프라 — 다중 클래스 이상 유형 분류기 학습 노드

본 노드는 사후 분석(Analyze) 단계의 학습 파이프라인 인프라를 제공한다.
학습된 분류기 가중치(classifier.pt)는 본 노드에서만 생성되며,
학습 파이프라인은 본 노드에서 일괄 수행한다.

역할:
  1. 학습된 LSTM Autoencoder의 인코더(가중치 동결) 재사용
  2. 정상 CSV 및 이상 시나리오별 CSV 로드
  3. 잠재 벡터(마지막 타임스텝)로 차원 축소
  4. MLP 분류기 학습 (CrossEntropy + 클래스 가중)
  5. 분류기 가중치(classifier.pt) 저장

  추가: 각 task별 잠재 centroid 산출 (task별 운영 패턴 표현)
       → task_latent_centroids.npz 저장

파라미터:
  csv_dir_normal     : 정상 데이터 CSV 경로
  csv_dir_anomaly    : 이상 데이터 CSV 경로
  model_dir          : 학습된 AE 로드 / classifier 저장 경로
  epochs             : 에폭 수 (기본 30)
  batch_size         : 미니배치 (기본 32)
  lr                 : 학습률 (기본 1e-3)
  val_split          : 검증 비율 (기본 0.15)

라벨 규약 (CSV 파일명 prefix):
  normal_*.csv            → class 0 (normal)
  torque_spike_*.csv      → class 1
  trajectory_dev_*.csv    → class 2
  timing_anomaly_*.csv    → class 3
  joint_freeze_*.csv      → class 4
  replay_attack_*.csv     → class 5
"""

import os
import glob
import json
import time
import threading
from collections import Counter

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

import rclpy
from rclpy.node import Node
from std_msgs.msg import String, Bool

from robot_forensics.feature_utils import load_csv_files, build_feature_vector
from robot_forensics.trainer_node import LSTMAutoencoder
from robot_forensics.models import AnomalyClassifier


# 파일명 prefix → 클래스 인덱스
CLASS_MAP = {
    'normal':           0,
    'torque_spike':     1,
    'trajectory_dev':   2,
    'timing_anomaly':   3,
    'joint_freeze':     4,
    'replay_attack':    5,
}
NUM_CLASSES = 6


def infer_class_from_filename(fname: str) -> int:
    """파일명 prefix로부터 클래스 라벨 추정."""
    base = os.path.basename(fname).lower()
    for prefix, idx in CLASS_MAP.items():
        if base.startswith(prefix):
            return idx
    return -1  # 미상


class ClassifierTrainerNode(Node):
    """6-class MLP 분류기 학습 노드."""

    def __init__(self):
        super().__init__('classifier_trainer_node')

        self.declare_parameter('csv_dir_normal',  '/tmp/rofas/data/csv')
        self.declare_parameter('csv_dir_anomaly', '/tmp/rofas/data/anomaly_csv')
        self.declare_parameter('model_dir',       '/tmp/rofas/models')
        self.declare_parameter('seq_len',         100)
        self.declare_parameter('stride',          10)
        self.declare_parameter('epochs',          30)
        self.declare_parameter('batch_size',      32)
        self.declare_parameter('lr',              1e-3)
        self.declare_parameter('val_split',       0.15)

        self.csv_dir_normal  = self.get_parameter('csv_dir_normal').value
        self.csv_dir_anomaly = self.get_parameter('csv_dir_anomaly').value
        self.model_dir       = self.get_parameter('model_dir').value
        self.seq_len         = self.get_parameter('seq_len').value
        self.stride          = self.get_parameter('stride').value
        self.epochs          = self.get_parameter('epochs').value
        self.batch_size      = self.get_parameter('batch_size').value
        self.lr              = self.get_parameter('lr').value
        self.val_split       = self.get_parameter('val_split').value

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.get_logger().info(f'[ClsTrainer] device={self.device}')

        self.done_pub = self.create_publisher(Bool, '/rofas/classifier_done', 10)
        self.log_pub  = self.create_publisher(String, '/rofas/classifier_log', 10)

        self.create_timer(2.0, self._start_once)
        self._started = False

    def _start_once(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    # ── 메인 파이프라인 ─────────────────────────────────────────
    def _run(self):
        try:
            self._log('분류기 학습 시작')

            # 1. AE 로드
            config_path = os.path.join(self.model_dir, 'model_config.json')
            if not os.path.exists(config_path):
                self.get_logger().error(
                    '[ClsTrainer] AE가 먼저 학습되어야 합니다. trainer_node를 먼저 실행하세요.')
                return
            with open(config_path) as f:
                cfg = json.load(f)
            INPUT_DIM  = cfg['input_dim']
            HID_DIM    = cfg['hidden_dim']
            LAT_DIM    = cfg['latent_dim']
            SEQ_LEN    = cfg['seq_len']

            ae = LSTMAutoencoder(INPUT_DIM, HID_DIM, LAT_DIM)
            ae.load_state_dict(torch.load(
                os.path.join(self.model_dir, 'autoencoder.pt'),
                map_location=self.device))
            ae.to(self.device).eval()
            for p in ae.parameters():
                p.requires_grad_(False)  # encoder freeze
            self._log(f'AE 로드 완료 (input={INPUT_DIM}, hidden={HID_DIM}, latent={LAT_DIM})')

            # 정규화 스케일러 로드
            sc = np.load(os.path.join(self.model_dir, 'scaler.npz'))
            scaler_mean = sc['mean']
            scaler_std  = sc['std']

            # 2. 데이터 로드 (정상 + 6-class 이상)
            X_seq, y_lbl, task_lbl = self._load_all_data(scaler_mean, scaler_std, SEQ_LEN)
            if len(X_seq) == 0:
                self.get_logger().error('[ClsTrainer] 학습 가능한 데이터가 없음')
                return

            counts = Counter(y_lbl)
            self._log(f'데이터 분포: {dict(counts)}')

            # 클래스 불균형 대응 가중치
            class_weights = np.zeros(NUM_CLASSES, dtype=np.float32)
            for c, n in counts.items():
                if 0 <= c < NUM_CLASSES and n > 0:
                    class_weights[c] = 1.0 / n
            # class_weights 정규화
            if class_weights.sum() > 0:
                class_weights = class_weights / class_weights.sum() * NUM_CLASSES
            class_weights_t = torch.tensor(class_weights, dtype=torch.float32).to(self.device)

            # 3. 잠재 벡터 추출 (배치 단위)
            self._log('잠재 벡터 추출 중...')
            X_tensor = torch.tensor(np.stack(X_seq), dtype=torch.float32).to(self.device)
            with torch.no_grad():
                latents = []
                for i in range(0, len(X_tensor), 64):
                    batch = X_tensor[i:i+64]
                    z = ae.encode(batch)        # (B, T, latent_dim)
                    latents.append(z[:, -1, :].cpu())  # 마지막 타임스텝
                latents = torch.cat(latents, dim=0)    # (N, latent_dim)
            labels = torch.tensor(y_lbl, dtype=torch.long)
            self._log(f'잠재 벡터: {latents.shape}')

            # 4. Train/Val 분할
            n = len(latents)
            idx = np.random.RandomState(42).permutation(n)
            n_val = max(1, int(n * self.val_split))
            tr_idx = idx[n_val:]
            val_idx = idx[:n_val]

            tr_ds  = TensorDataset(latents[tr_idx],  labels[tr_idx])
            val_ds = TensorDataset(latents[val_idx], labels[val_idx])
            tr_dl  = DataLoader(tr_ds,  batch_size=self.batch_size, shuffle=True)
            val_dl = DataLoader(val_ds, batch_size=self.batch_size)

            # 5. 분류기 학습
            clf = AnomalyClassifier(latent_dim=LAT_DIM, num_classes=NUM_CLASSES).to(self.device)
            opt = torch.optim.Adam(clf.parameters(), lr=self.lr)
            crit = nn.NLLLoss(weight=class_weights_t)  # Softmax 내장, log 필요

            best_val_acc = 0.0
            best_state   = None
            patience     = 0
            PATIENCE_MAX = 8

            for epoch in range(1, self.epochs + 1):
                clf.train()
                tr_loss = 0.0; tr_correct = 0; tr_total = 0
                for z, y in tr_dl:
                    z, y = z.to(self.device), y.to(self.device)
                    probs = clf(z)
                    # AnomalyClassifier가 Softmax를 마지막에 적용하므로
                    # 수치 안정성을 위해 log 후 NLLLoss 사용
                    log_probs = torch.log(probs + 1e-9)
                    loss = crit(log_probs, y)
                    opt.zero_grad()
                    loss.backward()
                    opt.step()
                    tr_loss += loss.item() * len(y)
                    tr_correct += (probs.argmax(dim=-1) == y).sum().item()
                    tr_total += len(y)
                tr_loss /= tr_total
                tr_acc = tr_correct / tr_total

                # Val
                clf.eval()
                val_correct = 0; val_total = 0
                per_class = Counter()
                per_class_correct = Counter()
                with torch.no_grad():
                    for z, y in val_dl:
                        z, y = z.to(self.device), y.to(self.device)
                        probs = clf(z)
                        pred = probs.argmax(dim=-1)
                        val_correct += (pred == y).sum().item()
                        val_total += len(y)
                        for yi, pi in zip(y.cpu().tolist(), pred.cpu().tolist()):
                            per_class[yi] += 1
                            if yi == pi:
                                per_class_correct[yi] += 1
                val_acc = val_correct / max(1, val_total)

                if epoch % 5 == 0 or epoch == 1:
                    self._log(
                        f'Epoch {epoch:3d}/{self.epochs} | '
                        f'TrLoss={tr_loss:.4f} TrAcc={tr_acc:.3f} | '
                        f'ValAcc={val_acc:.3f}'
                    )

                if val_acc > best_val_acc:
                    best_val_acc = val_acc
                    best_state   = {k: v.clone() for k, v in clf.state_dict().items()}
                    patience = 0
                else:
                    patience += 1
                    if patience >= PATIENCE_MAX:
                        self._log(f'Early stopping at epoch {epoch} (val_acc={best_val_acc:.3f})')
                        break

            # 6. 최적 모델 저장
            clf.load_state_dict(best_state)
            clf_path = os.path.join(self.model_dir, 'classifier.pt')
            torch.save(clf.state_dict(), clf_path)
            self._log(f'✅ 분류기 저장: {clf_path} | best val_acc={best_val_acc:.3f}')

            # 7. 검증 셋 혼동 행렬 산출
            cm = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int32)
            clf.eval()
            with torch.no_grad():
                for z, y in val_dl:
                    z, y = z.to(self.device), y.to(self.device)
                    pred = clf(z).argmax(dim=-1)
                    for yi, pi in zip(y.cpu().tolist(), pred.cpu().tolist()):
                        cm[yi, pi] += 1
            cm_path = os.path.join(self.model_dir, 'classifier_confusion_matrix.json')
            with open(cm_path, 'w') as f:
                json.dump({
                    'classes': list(CLASS_MAP.keys()),
                    'confusion_matrix': cm.tolist(),
                    'val_accuracy': float(best_val_acc),
                    'class_counts': dict(counts),
                }, f, indent=2)
            self._log(f'혼동 행렬 저장: {cm_path}')

            # 8. task별 잠재 centroid 산출 (향후 task-aware 이상 탐지 기반)
            self._compute_task_centroids(latents, task_lbl, y_lbl)

            # 학습 완료 신호 발행
            self.done_pub.publish(Bool(data=True))
            self.get_logger().info('[ClsTrainer] ✅ 학습 파이프라인 완료')

        except Exception as e:
            self.get_logger().error(f'[ClsTrainer] ❌ 오류: {e}')
            import traceback
            traceback.print_exc()

    # ── 데이터 로드 ─────────────────────────────────────────────
    def _load_all_data(self, mean, std, seq_len):
        """정상 + 이상 CSV 모두 로드. 파일명에서 라벨 추론."""
        X_seq, y_lbl, task_lbl = [], [], []

        # 정상
        for fp in sorted(glob.glob(os.path.join(self.csv_dir_normal, 'normal_*.csv'))):
            self._add_file(fp, 0, mean, std, seq_len, X_seq, y_lbl, task_lbl)

        # 이상
        if os.path.exists(self.csv_dir_anomaly):
            for fp in sorted(glob.glob(os.path.join(self.csv_dir_anomaly, '*.csv'))):
                cls = infer_class_from_filename(fp)
                if cls < 0:
                    continue
                self._add_file(fp, cls, mean, std, seq_len, X_seq, y_lbl, task_lbl)

        return X_seq, y_lbl, task_lbl

    def _add_file(self, fpath, cls, mean, std, seq_len, X_seq, y_lbl, task_lbl):
        # 단일 파일 파싱 (load_csv_files는 디렉토리 단위)
        from robot_forensics.feature_utils import _parse_csv, build_feature_vector
        seg = _parse_csv(fpath)
        if seg is None:
            return
        feat = build_feature_vector(seg['joints'], seg['ee'], mean, std)
        T = feat.shape[0]
        if T < seq_len:
            pad = np.tile(feat[-1:], (seq_len - T, 1))
            feat = np.vstack([feat, pad])
            X_seq.append(feat[:seq_len])
            y_lbl.append(cls)
            task_lbl.append(seg.get('task_name', 'unknown'))
        else:
            for start in range(0, T - seq_len + 1, self.stride):
                X_seq.append(feat[start:start + seq_len])
                y_lbl.append(cls)
                task_lbl.append(seg.get('task_name', 'unknown'))

    # ── task별 잠재 centroid ──────────────────────────────────
    def _compute_task_centroids(self, latents, task_lbl, y_lbl):
        """정상 클래스(0)에 한해 task별 잠재 centroid와 공분산 계산."""
        latents_np = latents.numpy()
        task_arr   = np.array(task_lbl)
        y_arr      = np.array(y_lbl)

        normal_mask = (y_arr == 0)
        task_set = set(task_arr[normal_mask].tolist())

        centroids = {}
        covariances = {}
        for task in task_set:
            mask = (task_arr == task) & normal_mask
            if mask.sum() < 5:
                continue
            sub = latents_np[mask]
            centroids[task]   = sub.mean(axis=0)
            covariances[task] = np.cov(sub, rowvar=False) + np.eye(sub.shape[1]) * 1e-4

        if not centroids:
            self._log('centroid 산출 가능한 정상 데이터 부족 — 스킵')
            return

        out_path = os.path.join(self.model_dir, 'task_latent_centroids.npz')
        np.savez(out_path,
                 task_names = np.array(list(centroids.keys())),
                 centroids  = np.stack(list(centroids.values())),
                 covariances= np.stack(list(covariances.values())))
        self._log(f'task별 잠재 centroid 저장: {out_path} ({len(centroids)} tasks)')

    # ── 로그 헬퍼 ───────────────────────────────────────────────
    def _log(self, msg: str):
        self.get_logger().info(f'[ClsTrainer] {msg}')
        self.log_pub.publish(String(data=msg))


def main(args=None):
    rclpy.init(args=args)
    node = ClassifierTrainerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
