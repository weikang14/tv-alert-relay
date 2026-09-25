import math


def alma(values: list[float], length: int, offset: float, sigma: float) -> list[float | None]:
    if length < 1:
        raise ValueError("length must be >= 1")

    m = offset * (length - 1)
    s = length / sigma
    weights = [math.exp(-((j - m) ** 2) / (2 * s * s)) for j in range(length)]
    weight_sum = sum(weights)

    result: list[float | None] = []
    for i in range(len(values)):
        if i < length - 1:
            result.append(None)
            continue
        window = values[i - length + 1 : i + 1]
        result.append(sum(w * v for w, v in zip(weights, window)) / weight_sum)
    return result
