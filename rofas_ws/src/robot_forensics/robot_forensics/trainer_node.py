#!/usr/bin/env python3
"""
trainer_node.py
───────────────
6DoF-RFA Analyze 단계 인프라 — 정상 궤적 기반 LSTM Autoencoder 프로파일러 학습 노드

역할:
  1. 정상 운영 CSV 데이터를 로드한다.
  2. 시계열 데이터를 정규화하고 Z-score 스케일러(scaler.npz)를 저장한다.
  3. 로봇의 정상 시계열 특징을 압축·복원하는 LSTM Autoencoder를 학습한다.
  4. 학습된 모델(autoencoder.pt)과 설정 파일을 저장하여 classifier_trainer_node가
     인코더 가중치를 재사용할 수 있도록 한다.
"""

import os
import json
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

import rclpy
from rclpy.node import Node

from robot_forensics.feature_utils import load_csv_files, build_feature_vector


# ── LSTM Autoencoder 모델 구조 ────────────────────────────────────────────────

class Encoder(nn.Module):
    """입력 시계열을 latent 벡터 시퀀스로 압축한다."""

    def __init__(self, input_dim, hidden_dim, latent_dim):
        super().__init__()
        self.lstm = nn.LSTM(input_dim, hidden_dim, batch_first=True)
        self.fc = nn.Linear(hidden_dim, latent_dim)

    def forward(self, x):
        out, _ = self.lstm(x)
        return self.fc(out)


class Decoder(nn.Module):
    """latent 벡터 시퀀스로부터 원본 시계열을 복원한다."""

    def __init__(self, latent_dim, hidden_dim, output_dim):
        super().__init__()
        self.lstm = nn.LSTM(latent_dim, hidden_dim, batch_first=True)
        self.fc = nn.Linear(hidden_dim, output_dim)

    def forward(self, z):
        out, _ = self.lstm(z)
        return self.fc(out)


class LSTMAutoencoder(nn.Module):
    """Encoder–Decoder 결합 모델."""

    def __init__(self, input_dim=9, hidden_dim=64, latent_dim=32, seq_len=100):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.seq_len = seq_len

        self.encoder = Encoder(input_dim, hidden_dim, latent_dim)
        self.decoder = Decoder(latent_dim, hidden_dim, input_dim)

    def encode(self, x):
        return self.encoder(x)

    def forward(self, x):
        z = self.encoder(x)
        return self.decoder(z)


# ── ROS2 학습 노드 ────────────────────────────────────────────────────────────

class TrainerNode(Node):
    def __init__(self):
        super().__init__('trainer_node')

        self.declare_parameter('csv_dir',    '/tmp/rofas/data/csv')
        self.declare_parameter('model_dir',  '/tmp/rofas/models')
        self.declare_parameter('seq_len',    100)
        self.declare_parameter('stride',     10)
        self.declare_parameter('epochs',     20)
        self.declare_parameter('batch_size', 32)
        self.declare_parameter('lr',         1e-3)

        self.csv_dir    = self.get_parameter('csv_dir').value
        self.model_dir  = self.get_parameter('model_dir').value
        self.seq_len    = self.get_parameter('seq_len').value
        self.stride     = self.get_parameter('stride').value
        self.epochs     = self.get_parameter('epochs').value
        self.batch_size = self.get_parameter('batch_size').value
        self.lr         = self.get_parameter('lr').value

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        os.makedirs(self.model_dir, exist_ok=True)

        self._started = False
        self.create_timer(1.0, self._start_once)

    def _start_once(self):
        if self._started:
            return
        self._started = True
        self._run_training_pipeline()

    def _run_training_pipeline(self):
        self.get_logger().info('======= [AE Trainer] LSTM Autoencoder 학습 시작 =======')

        # CSV 로드 (지정 경로 비어있을 시 anomaly_csv 폴더로 fallback)
        self.get_logger().info(f'데이터 로드: {self.csv_dir}')
        if not os.path.exists(self.csv_dir) or len(os.listdir(self.csv_dir)) == 0:
            fallback_dir = '/tmp/rofas/data/anomaly_csv'
            if os.path.exists(fallback_dir):
                self.csv_dir = fallback_dir
                self.get_logger().warn(f'정상 CSV 폴더가 비어 fallback 사용: {fallback_dir}')

        segments = load_csv_files(self.csv_dir)
        if not segments:
            self.get_logger().error('학습할 CSV 데이터가 없습니다. 디렉토리를 확인하세요.')
            return

        # Z-score 스케일러 산출
        all_raw_feats = [np.hstack([seg['joints'], seg['ee']]) for seg in segments]
        combined_raw = np.vstack(all_raw_feats)
        scaler_mean = combined_raw.mean(axis=0)
        scaler_std  = combined_raw.std(axis=0)

        scaler_path = os.path.join(self.model_dir, 'scaler.npz')
        np.savez(scaler_path, mean=scaler_mean, std=scaler_std)
        self.get_logger().info(f'Z-score 스케일러 저장: {scaler_path}')

        # 슬라이딩 윈도우 시퀀스 빌드
        X_sequences = []
        for seg in segments:
            feat = build_feature_vector(seg['joints'], seg['ee'], scaler_mean, scaler_std)
            T = feat.shape[0]
            if T < self.seq_len:
                pad = np.tile(feat[-1:], (self.seq_len - T, 1))
                feat = np.vstack([feat, pad])
                X_sequences.append(feat[:self.seq_len])
            else:
                for start in range(0, T - self.seq_len + 1, self.stride):
                    X_sequences.append(feat[start:start + self.seq_len])

        X_train = np.stack(X_sequences)
        self.get_logger().info(f'학습 시퀀스 텐서: {X_train.shape}')

        dataset = TensorDataset(torch.tensor(X_train, dtype=torch.float32))
        dataloader = DataLoader(dataset, batch_size=self.batch_size, shuffle=True)

        # 모델·옵티마이저 초기화
        input_dim = X_train.shape[2]  # 보통 9 (6관절 + 3좌표)
        model = LSTMAutoencoder(
            input_dim=input_dim, hidden_dim=64, latent_dim=32, seq_len=self.seq_len
        ).to(self.device)

        optimizer = torch.optim.Adam(model.parameters(), lr=self.lr)
        criterion = nn.MSELoss()

        # 학습 루프
        model.train()
        for epoch in range(1, self.epochs + 1):
            epoch_loss = 0.0
            for batch in dataloader:
                x = batch[0].to(self.device)
                recon = model(x)
                loss = criterion(recon, x)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                epoch_loss += loss.item() * x.size(0)

            epoch_loss /= len(dataloader.dataset)
            if epoch % 5 == 0 or epoch == 1:
                self.get_logger().info(
                    f'Epoch [{epoch:2d}/{self.epochs}] Loss(MSE): {epoch_loss:.6f}'
                )

        # 모델 가중치·설정 저장
        model_path = os.path.join(self.model_dir, 'autoencoder.pt')
        torch.save(model.state_dict(), model_path)

        config_path = os.path.join(self.model_dir, 'model_config.json')
        with open(config_path, 'w') as f:
            json.dump({
                'input_dim': input_dim,
                'hidden_dim': 64,
                'latent_dim': 32,
                'seq_len': self.seq_len,
            }, f, indent=2)

        self.get_logger().info(f'LSTM Autoencoder 저장: {model_path}')
        self.get_logger().info('======= [AE Trainer] 학습 완료 =======')


def main(args=None):
    rclpy.init(args=args)
    node = TrainerNode()
    try:
        rclpy.spin_once(node, timeout_sec=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
