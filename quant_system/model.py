from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .features import FEATURES


@dataclass
class LogisticModel:
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        x = frame[FEATURES].to_numpy(float)
        z = np.clip(((x - self.mean) / self.scale) @ self.weights[:-1] + self.weights[-1], -30, 30)
        return 1 / (1 + np.exp(-z))


def fit_logistic(frame: pd.DataFrame, seed: int = 42) -> LogisticModel:
    if frame.empty:
        raise ValueError("模型训练集为空")
    x = frame[FEATURES].to_numpy(float)
    y = frame["target"].to_numpy(float)
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-10] = 1
    x = (x - mean) / scale
    x = np.column_stack([x, np.ones(len(x))])
    weights = np.zeros(x.shape[1])
    rng = np.random.default_rng(seed)
    order = np.arange(len(x))
    for epoch in range(500):
        rng.shuffle(order)
        xb, yb = x[order], y[order]
        prediction = 1 / (1 + np.exp(-np.clip(xb @ weights, -30, 30)))
        gradient = xb.T @ (prediction - yb) / len(xb)
        gradient[:-1] += 0.002 * weights[:-1]
        weights -= 0.08 / (1 + epoch / 150) * gradient
    return LogisticModel(mean, scale, weights)
