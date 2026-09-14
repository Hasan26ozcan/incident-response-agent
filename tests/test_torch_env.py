"""Verify the test environment has torch and numpy working correctly."""
import sys

import torch
import numpy as np


def test_torch_available():
    """PyTorch must be importable and report a version."""
    assert torch.__version__, "torch has no __version__"
    # Verify basic tensor ops work
    t = torch.tensor([1.0, 2.0, 3.0])
    assert t.sum().item() == 6.0, "Basic torch tensor ops failed"
    assert torch.cuda.is_available() or True, "CPU torch is acceptable"


def test_numpy_available():
    """NumPy must be importable and report a version."""
    assert np.__version__, "numpy has no __version__"
    arr = np.array([1, 2, 3])
    assert arr.sum() == 6, "Basic numpy ops failed"


def test_torch_numpy_interop():
    """torch and numpy must interoperate without errors."""
    arr = np.array([1.0, 2.0, 3.0])
    t = torch.from_numpy(arr)
    assert t.sum().item() == 6.0
