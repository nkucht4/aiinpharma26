"""Optional PyTorch inference helpers for first-failure hazards."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import import_module
from math import isfinite
from typing import Any


def first_failure_distribution(hazards, last_n, unit_step=1):
    """Convert per-unit failure hazards into a first-failure distribution."""
    if unit_step <= 0:
        raise ValueError("unit_step must be greater than zero")
    values = [float(value) for value in hazards]
    if not values:
        raise ValueError("hazards must not be empty")
    if any(not isfinite(value) or value < 0.0 or value > 1.0 for value in values):
        raise ValueError("hazards must be finite probabilities in [0, 1]")

    survival = 1.0
    probabilities = []
    median_offset = None
    for offset, hazard in enumerate(values, start=1):
        probability = survival * hazard
        probabilities.append(probability)
        survival *= 1.0 - hazard
        if median_offset is None and 1.0 - survival >= 0.5:
            median_offset = offset

    mode_offset = max(range(1, len(values) + 1), key=lambda index: probabilities[index - 1])
    predicted_offset = median_offset if median_offset is not None else mode_offset
    confidence_start = max(0, predicted_offset - 6)
    confidence_end = min(len(probabilities), predicted_offset + 5)
    return {
        "hazards": values,
        "first_failure_probabilities": probabilities,
        "p_within_horizon": 1.0 - survival,
        "p_next5": sum(probabilities[:5]),
        "confidence": sum(probabilities[confidence_start:confidence_end]),
        "has_estimate": median_offset is not None,
        "median_n": None if median_offset is None else last_n + median_offset * unit_step,
        "mode_n": last_n + mode_offset * unit_step,
        "predicted_n": last_n + predicted_offset * unit_step,
    }


class TorchFirstFailureInference:
    """Run a supplied model that returns one failure hazard per future unit.

    ``feature_builder`` owns model-specific preprocessing and returns a tensor
    accepted by ``model``. The model output must be probabilities shaped [H]
    or [1, H], with sigmoid/other calibration included in the model itself.
    """

    def __init__(
        self,
        model: Any,
        feature_builder: Callable[[Sequence[dict], int, Any, Any], Any],
        torch_module: Any | None = None,
    ) -> None:
        self._torch = torch_module or import_module("torch")
        self.device = self._torch.device(
            "cuda" if self._torch.cuda.is_available() else "cpu"
        )
        self.model = model.to(self.device)
        self.model.eval()
        self.feature_builder = feature_builder

    def predict(self, rows, horizon, last_n, unit_step=1):
        if horizon <= 0:
            raise ValueError("horizon must be greater than zero")
        features = self.feature_builder(rows, horizon, self._torch, self.device)
        if hasattr(features, "to"):
            features = features.to(self.device)
        with self._torch.inference_mode():
            output = self.model(features)

        shape = tuple(output.shape)
        if len(shape) == 2 and shape[0] == 1:
            output = output[0]
            shape = tuple(output.shape)
        if len(shape) != 1 or shape[0] != horizon:
            raise ValueError("model output must have shape [horizon] or [1, horizon]")
        hazards = output.detach().to("cpu").tolist()
        return first_failure_distribution(hazards, last_n, unit_step)