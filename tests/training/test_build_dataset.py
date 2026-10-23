import pytest
from build_dataset import argmax


# REQ-ROUTER-S1-004
def test_argmax_basic():
    d = {"neutral": 0.1, "angry": 0.8, "frustrated": 0.1}
    assert argmax(d) == "angry"


# REQ-ROUTER-S1-004
def test_argmax_tie_breaker():
    d = {"a": 0.5, "b": 0.5}
    # Should pick either a or b consistently without error
    assert argmax(d) in ("a", "b")


# REQ-ROUTER-S1-004
def test_argmax_integer_keys():
    d = {0: 0.1, 1: 0.2, 2: 0.7}
    assert argmax(d) == 2
