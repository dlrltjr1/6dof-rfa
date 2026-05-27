#!/usr/bin/env python3
"""
models.py
─────────
6DoF-RFA Analyze 단계 인프라 — 학습 모델 구조 정의

본 모듈은 정상 운영 패턴 학습 및 이상 유형 분류를 위한 신경망 구조를
제공한다. classifier_trainer_node가 임포트하여 사용한다.
"""

import torch
import torch.nn as nn


class AnomalyClassifier(nn.Module):
    """
    잠재 벡터(latent vector)를 입력으로 받아 이상 유형 확률을 출력하는
    다층 퍼셉트론(MLP) 분류기.

    LSTM Autoencoder의 인코더 출력(latent_dim 차원)을 받아
    num_classes 개의 이상 유형 중 하나로 분류한다.
    """

    def __init__(self, latent_dim: int = 32, num_classes: int = 6):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(latent_dim, 64),
            nn.ReLU(),
            nn.Linear(64, num_classes),
            nn.Softmax(dim=-1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)
