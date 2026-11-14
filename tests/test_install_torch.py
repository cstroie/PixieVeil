"""install.py's PyTorch wheel selection (pure functions — nothing installed).

Pins are chosen per CUDA index from what download.pytorch.org actually
publishes; torch < 2.10 has a HIGH advisory for RCE via untrusted
checkpoint files, which matters because nnU-Net torch.load()s downloaded
model weights.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pv_install", Path(__file__).resolve().parent.parent / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)


@pytest.mark.parametrize("cuda, tag", [
    ((11, 0), "cu118"), ((11, 8), "cu118"),
    ((12, 0), "cu126"), ((12, 4), "cu126"), ((12, 8), "cu126"), ((12, 9), "cu126"),
    ((13, 0), "cu130"), ((13, 2), "cu130"),
    ((14, 0), "cu130"),           # newer driver runs the newest build we know
])
def test_wheel_follows_the_cuda_major_version(cuda, tag):
    # A cuXY build needs a driver of the same CUDA major (minor-version
    # compatibility); rounding 12.8 *up* to a CUDA 13 build would not run.
    assert install._select_wheel(cuda)[1] == tag


def test_cuda_args_pin_existing_matching_builds():
    args = install._torch_cuda_args((12, 4))
    assert args[:2] == ["torch==2.14.1+cu126", "torchvision==0.29.1+cu126"]
    assert args[-2:] == ["--index-url", "https://download.pytorch.org/whl/cu126"]
    assert not any(a.startswith("torchaudio") for a in args)     # unused


def test_cpu_args_pin_a_patched_build():
    assert install._TORCH_CPU[:2] == ["torch==2.14.1+cpu", "torchvision==0.29.1+cpu"]


@pytest.mark.parametrize("installed, tag, outdated", [
    ("2.4.0+cpu", "cpu", True),
    ("2.14.1+cpu", "cpu", False),
    ("2.15.0+cpu", "cpu", False),
    ("2.7.1+cu118", "cu118", False),    # newest the cu118 index has
    ("2.6.0+cu118", "cu118", True),
    ("2.4.0+cu121", "cu126", True),
    ("garbage", "cpu", True),
])
def test_outdated_torch_is_reinstalled(installed, tag, outdated):
    assert install._torch_outdated(installed, tag) is outdated
