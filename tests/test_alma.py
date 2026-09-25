import pytest

from alma import alma


def test_alma_length_one_returns_input_unchanged():
    assert alma([5.0, 10.0, 15.0], length=1, offset=0.85, sigma=5) == [5.0, 10.0, 15.0]


def test_alma_pads_none_until_enough_history():
    result = alma([1.0], length=2, offset=0.85, sigma=5)
    assert result == [None]


def test_alma_matches_hand_computed_value_for_length_two():
    # weights: w0=exp(-((0-0.85)**2)/(2*0.4**2)), w1=exp(-((1-0.85)**2)/(2*0.4**2))
    # m = 0.85*(2-1) = 0.85, s = 2/5 = 0.4
    result = alma([10.0, 20.0], length=2, offset=0.85, sigma=5)
    assert result[0] is None
    assert result[1] == pytest.approx(18.993, abs=0.01)


def test_alma_continues_correctly_across_a_longer_series():
    result = alma([10.0, 20.0, 10.0], length=2, offset=0.85, sigma=5)
    assert result[0] is None
    assert result[1] == pytest.approx(18.993, abs=0.01)
    # window is now [20.0, 10.0]: weighted mostly toward the newest value (10.0)
    assert result[2] == pytest.approx(11.009, abs=0.01)
