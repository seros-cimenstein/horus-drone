"""Focused validation checks for perception runtime budgets."""

from __future__ import annotations

import pytest

from horus.config import PerceptionSettings


@pytest.mark.parametrize("field_name", ("onnx_intra_op_num_threads", "onnx_inter_op_num_threads"))
@pytest.mark.parametrize("value", (0, -1, True, 1.5, None, "2"))
def test_onnx_thread_counts_require_positive_integers(field_name, value):
    with pytest.raises(ValueError):
        PerceptionSettings(**{field_name: value})
