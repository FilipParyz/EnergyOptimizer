"""A small logistic regression that learns which candidate hours are
actually accepted versus overridden. Pure NumPy, no external ML framework.

This never runs unless there are enough resolved suggestions to fit AND
v7alidate a model, and a freshly trained candidate only replaces the
previously-promoted model if it beats the plain heuristic's own hit-rate on
held-out data. If it doesn't clear that bar, the heuristic just keeps
running untouched -- this module can only ever nudge the final score, never
override it outright.
"""
from __future__ import annotations
import numpy as np
from .const import MIN_SAMPLES_FOR_ML, ML_AUC_PROMOTION_MARGIN


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def build_features(habit_score: float, solar_score: float, urgency: float,
                    hour: int, day_of_week: int) -> np.ndarray:
    return np.array([
        habit_score,
        solar_score,
        urgency,
        np.sin(2 * np.pi * hour / 24),
        np.cos(2 * np.pi * hour / 24),
        np.sin(2 * np.pi * day_of_week / 7),
        np.cos(2 * np.pi * day_of_week / 7),
    ])


def _train_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0,
                     lr: float = 0.2, epochs: int = 800) -> dict:
    n, d = X.shape
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std == 0] = 1.0
    Xs = (X - mean) / std
    Xb = np.hstack([np.ones((n, 1)), Xs])
    w = np.zeros(d + 1)
    for _ in range(epochs):
        p = _sigmoid(Xb @ w)
        grad = Xb.T @ (p - y) / n
        grad[1:] += l2 * w[1:] / n
        w -= lr * grad
    return {"weights": w.tolist(), "mean": mean.tolist(), "std": std.tolist(), "n_samples": int(n)}


def predict(model: dict, X: np.ndarray) -> np.ndarray:
    w = np.array(model["weights"])
    mean = np.array(model["mean"])
    std = np.array(model["std"])
    Xs = (X - mean) / std
    Xb = np.hstack([np.ones((X.shape[0], 1)), Xs])
    return _sigmoid(Xb @ w)


def _auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    """Mann-Whitney rank-sum AUC -- avoids pulling in scikit-learn for one metric."""
    pos = y_score[y_true == 1]
    neg = y_score[y_true == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    ranks = np.argsort(np.argsort(np.concatenate([pos, neg]))) + 1
    rank_sum_pos = ranks[: len(pos)].sum()
    return float((rank_sum_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def maybe_train_and_promote(examples: list[dict], current_model: dict | None) -> dict | None:
    if len(examples) < MIN_SAMPLES_FOR_ML:
        return current_model

    X = np.array([e["features"] for e in examples])
    y = np.array([e["label"] for e in examples], dtype=float)
    heuristic = np.array([e["heuristic_score"] for e in examples])

    split = max(1, int(len(examples) * 0.8))
    X_train, X_hold = X[:split], X[split:]
    y_train, y_hold = y[:split], y[split:]
    heur_hold = heuristic[split:]

    if len(np.unique(y_train)) < 2 or len(X_hold) < 5:
        return current_model

    candidate = _train_logistic(X_train, y_train)
    candidate_auc = _auc(y_hold, predict(candidate, X_hold))
    baseline_auc = _auc(y_hold, heur_hold)

    if candidate_auc >= baseline_auc + ML_AUC_PROMOTION_MARGIN:
        candidate["promoted"] = True
        candidate["auc"] = candidate_auc
        candidate["baseline_auc"] = baseline_auc
        return candidate

    return current_model


def ml_adjusted_scores(model: dict | None, habit: dict[int, float], solar: dict[int, float],
                         urgency_val: float, hours: list[int], day_of_week: int) -> dict[int, float] | None:
    if not model or not model.get("promoted"):
        return None
    X = np.array([
        build_features(habit.get(h, 0.0), solar.get(h, 0.0), urgency_val, h, day_of_week)
        for h in hours
    ])
    preds = predict(model, X)
    return {h: float(v) for h, v in zip(hours, preds)}
