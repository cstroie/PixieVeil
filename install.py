#!/usr/bin/env python3
"""
PixieVeil Installation & Environment Setup
==========================================

Interactive setup script. Run once after cloning / updating the repo:

    python install.py [--venv [PATH]] [--config PATH] [--non-interactive] [--download-model]

Steps
-----
  0. Verify Python >= 3.12 (hard gate).
  1. Detect CUDA version (nvcc, then nvidia-smi fallback).
  2. Ask whether defacing should be enabled and which compute backend to use.
  3. Install / verify torch and nnUNetv2 for the chosen backend (pip subprocess).
  4. Verify Python version and imported packages.
  5. Create required runtime directories.
  6. Download the nnUNet defacing model from Google Drive if missing.
  7. Persist defacing choice to settings.yaml.
  8. Final sanity checks (core imports).

Exit code 0 on success, non-zero on any failure.
"""

import argparse
import importlib
import os
import re
import subprocess
import sys
import venv as _venv_mod
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODEL_DATASET  = "Dataset001_DEFACE"
_MODEL_GDRIVE_URL = (
    "https://drive.google.com/drive/folders/"
    "1k4o35Dkl7PWd2yvHqWA2ia-BNKrWBrqg?usp=sharing"
)

# PyTorch install variants.
# CPU: pinned +cpu builds from the PyTorch wheel index.
#   torch.GradScaler moved to top-level in 2.4 — don't go below 2.4.0.
_TORCH_CPU = [
    "torch==2.4.0+cpu",
    "torchvision==0.19.0+cpu",
    "torchaudio==2.4.0+cpu",
    "--index-url", "https://download.pytorch.org/whl/cpu",
]

# Ascending by CUDA version so ceiling selection (first entry >= installed) works naturally.
_CUDA_WHEEL_MAP = [
    ((11, 8), "cu118", "https://download.pytorch.org/whl/cu118"),
    ((12, 1), "cu121", "https://download.pytorch.org/whl/cu121"),
    ((12, 4), "cu124", "https://download.pytorch.org/whl/cu124"),
    ((12, 6), "cu126", "https://download.pytorch.org/whl/cu126"),
    ((12, 8), "cu128", "https://download.pytorch.org/whl/cu128"),
]


def _detect_cuda_version() -> tuple[int, int] | None:
    """Return (major, minor) of the CUDA version available on this machine, or None."""
    def _run(*cmd: str) -> str | None:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
            return r.stdout if r.returncode == 0 else None
        except FileNotFoundError:
            return None

    # Prefer nvcc — gives the actual toolkit version.
    out = _run("nvcc", "--version")
    if out:
        m = re.search(r"release (\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))

    # Fallback: nvidia-smi table header includes "CUDA Version: X.Y"
    # (this is the max CUDA version the driver supports, good enough for wheel selection)
    out = _run("nvidia-smi")
    if out:
        m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))

    return None


def _select_wheel(cuda_ver: tuple[int, int]) -> tuple[tuple[int, int], str, str]:
    """Return the wheel map entry whose version is the ceiling of *cuda_ver*.

    Ceiling = smallest wheel_ver >= cuda_ver, so CUDA 12.0 → cu121.
    Falls back to the highest entry when cuda_ver exceeds all known wheels.
    """
    for entry in _CUDA_WHEEL_MAP:
        if entry[0] >= cuda_ver:
            return entry
    return _CUDA_WHEEL_MAP[-1]


def _torch_cuda_args(cuda_ver: tuple[int, int]) -> list[str]:
    """Return pip install args for the CUDA-enabled PyTorch wheel matching *cuda_ver*."""
    wheel_ver, tag, index_url = _select_wheel(cuda_ver)
    _info(f"CUDA {cuda_ver[0]}.{cuda_ver[1]} → selecting PyTorch wheels for {tag}")
    return [
        f"torch==2.4.0+{tag}",
        f"torchvision==0.19.0+{tag}",
        f"torchaudio==2.4.0+{tag}",
        "--index-url", index_url,
    ]

_NNUNET_PACKAGES = ["nnunetv2", "gdown"]


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_PASS = "[OK]  "
_FAIL = "[FAIL]"
_SKIP = "[SKIP]"
_INFO = "[INFO]"


def _ok(msg: str)   -> None: print(f"  {_PASS}  {msg}")
def _fail(msg: str) -> None: print(f"  {_FAIL}  {msg}", file=sys.stderr)
def _skip(msg: str) -> None: print(f"  {_SKIP}  {msg}")
def _info(msg: str) -> None: print(f"  {_INFO}  {msg}")


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------

def _create_venv(venv_path: Path) -> int:
    """Create a venv at *venv_path*, install requirements.txt, then re-exec inside it.

    Re-exec passes all original sys.argv args unchanged; on re-entry the prefix
    check at the top of main() detects we're already inside the venv and skips
    this function.  Never returns on success (os.execv replaces the process).
    """
    _section("Virtual environment setup")

    if venv_path.exists():
        _info(f"Directory {venv_path} already exists — reusing.")
    else:
        _info(f"Creating virtual environment at {venv_path} ...")
        try:
            _venv_mod.create(str(venv_path), with_pip=True)
            _ok(f"Virtual environment created at {venv_path}")
        except Exception as exc:
            _fail(f"venv creation failed: {exc}")
            return 1

    venv_python = venv_path / "bin" / "python"
    if not venv_python.exists():
        _fail(f"Python binary not found at {venv_python}")
        return 1

    req = Path("requirements.txt")
    if req.exists():
        _info("Installing requirements.txt ...")
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(req)]
        )
        if result.returncode != 0:
            _fail("requirements.txt installation failed.")
            return 1
        _ok("requirements.txt installed.")
    else:
        _info("requirements.txt not found — skipping base install.")

    _info("Re-launching inside the virtual environment ...")
    script = os.path.abspath(__file__)
    os.execv(str(venv_python), [str(venv_python), script] + sys.argv[1:])
    return 0  # unreachable


# ---------------------------------------------------------------------------
# Step 0 – Interactive defacing prompt
# ---------------------------------------------------------------------------

class DefacingChoice:
    NONE = "none"
    CPU  = "cpu"
    CUDA = "cuda"


def ask_defacing_choice(
    non_interactive: bool,
    current_enabled: bool,
    cuda_ver: tuple[int, int] | None,
) -> str:
    """Ask the user whether to enable defacing and which backend to use.

    Returns one of DefacingChoice.{NONE, CPU, CUDA}.
    Option 2 (CUDA) is only offered when *cuda_ver* is not None.
    """
    _section("Step 1 — Defacing configuration")

    if non_interactive:
        choice = DefacingChoice.CPU if current_enabled else DefacingChoice.NONE
        _info(f"Non-interactive mode: using current setting "
              f"({'enabled/cpu' if current_enabled else 'disabled'}).")
        return choice

    cuda_label = (
        f"Enable defacing, CUDA {cuda_ver[0]}.{cuda_ver[1]} (GPU)"
        if cuda_ver else None
    )
    print("\n  Defacing removes facial features from head CT scans (requires nnUNetv2).\n")
    print("  Options:")
    print("    0 – No defacing")
    print("    1 – Enable defacing, CPU only")
    if cuda_label:
        print(f"    2 – {cuda_label}")
    else:
        _info("CUDA not detected — GPU option unavailable.")
    print()

    valid = {"0", "1"} | ({"2"} if cuda_ver else set())
    default = "1" if current_enabled else "0"
    prompt = f"  Your choice [{'0/1/2' if cuda_ver else '0/1'}] (default: {default}): "
    while True:
        raw = input(prompt).strip() or default
        if raw == "0":
            return DefacingChoice.NONE
        if raw == "1":
            return DefacingChoice.CPU
        if raw == "2" and cuda_ver:
            return DefacingChoice.CUDA
        print(f"  Please enter one of: {', '.join(sorted(valid))}.")


# ---------------------------------------------------------------------------
# Step 1 – Install packages
# ---------------------------------------------------------------------------

def _check_package(module: str, attribute: str = "__version__") -> Optional[str]:
    """Return the installed version string, or None if not importable."""
    try:
        mod = importlib.import_module(module)
        return str(getattr(mod, attribute, "installed"))
    except ImportError:
        return None


def _pip_install(packages: list[str], label: str) -> bool:
    """Run pip install for *packages*. Returns True on success."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
    )
    if result.returncode != 0:
        _fail(f"{label} installation failed.")
        return False
    _ok(f"{label} installed.")
    return True


def install_packages(choice: str, cuda_ver: tuple[int, int] | None) -> bool:
    _section("Step 2 — Package installation")

    if choice == DefacingChoice.NONE:
        _skip("Defacing disabled — skipping torch / nnUNetv2 installation.")
        return True

    # --- PyTorch ---
    torch_ver = _check_package("torch")
    needs_install = not torch_ver

    if torch_ver and choice == DefacingChoice.CUDA:
        try:
            import torch as _torch
            if not _torch.cuda.is_available():
                _info(f"torch {torch_ver} installed but CUDA unavailable — reinstalling CUDA build.")
                needs_install = True
            elif cuda_ver is not None:
                # Check if the installed wheel matches the expected ceiling wheel.
                installed_cuda = _torch.version.cuda  # e.g. "12.1" or None
                if installed_cuda:
                    installed_parts = tuple(int(x) for x in installed_cuda.split(".")[:2])
                    expected_ver, expected_tag, _ = _select_wheel(cuda_ver)
                    if installed_parts != expected_ver:
                        _info(
                            f"torch built for CUDA {installed_cuda}, "
                            f"expected {expected_tag} for CUDA {cuda_ver[0]}.{cuda_ver[1]} "
                            f"— reinstalling."
                        )
                        needs_install = True
                    else:
                        _ok(f"torch {torch_ver} (CUDA {installed_cuda}) matches {expected_tag} — skipping.")
                else:
                    _info("torch CUDA build version unknown — reinstalling to be safe.")
                    needs_install = True
        except ImportError:
            needs_install = True

    if needs_install:
        if choice == DefacingChoice.CPU:
            _info("Installing CPU-only PyTorch.")
            torch_args = _TORCH_CPU
        elif cuda_ver is not None:
            torch_args = _torch_cuda_args(cuda_ver)
        else:
            _info("CUDA version unknown — installing unpinned CUDA wheels from PyPI.")
            torch_args = ["torch==2.4.0", "torchvision==0.19.0", "torchaudio==2.4.0"]
        if not _pip_install(torch_args, f"PyTorch ({choice.upper()})"):
            return False
    elif not (torch_ver and choice == DefacingChoice.CUDA):
        _ok(f"torch {torch_ver} already installed — skipping.")

    # --- nnunetv2 ---
    nnunet_ver = _check_package("nnunetv2")
    if nnunet_ver:
        _ok(f"nnunetv2 {nnunet_ver} already installed — skipping.")
    else:
        _info("nnunetv2 not found — installing.")
        if not _pip_install(["nnunetv2"], "nnunetv2"):
            return False

    # --- gdown ---
    gdown_ver = _check_package("gdown")
    if gdown_ver:
        _ok(f"gdown {gdown_ver} already installed — skipping.")
    else:
        _info("gdown not found — installing (required for model download).")
        if not _pip_install(["gdown"], "gdown"):
            return False

    return True


# ---------------------------------------------------------------------------
# Step 2 – Python version & defacing imports
# ---------------------------------------------------------------------------

def check_python_and_deps(choice: str) -> bool:
    _section("Step 3 — Runtime requirements")

    ok = True

    vi = sys.version_info
    _ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")  # already gated in main()

    if choice == DefacingChoice.NONE:
        _skip("Defacing disabled — skipping torch / nnUNetv2 checks.")
        return ok

    try:
        import torch
        _ok(f"torch {torch.__version__}")
    except ImportError:
        _fail("torch not importable — run install.py again or install PyTorch manually.")
        ok = False
        return ok  # remaining checks all need torch

    if choice == DefacingChoice.CUDA:
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            _ok(f"CUDA available — {device_name}")
            try:
                t = torch.tensor([1.0]).cuda()
                _ = (t * 2).item()
                _ok("CUDA tensor round-trip passed")
            except Exception as e:
                _fail(f"CUDA tensor round-trip failed: {e}")
                ok = False
        else:
            _fail(
                "CUDA not available — drivers may be missing or incompatible. "
                "Re-run install.py and choose CPU, or fix your CUDA/driver install."
            )
            ok = False

    try:
        import nnunetv2
        _ok(f"nnunetv2 {getattr(nnunetv2, '__version__', 'installed')}")
    except ImportError:
        _fail("nnunetv2 not importable — run install.py again or install nnUNetv2 manually.")
        ok = False

    return ok


# ---------------------------------------------------------------------------
# Step 3 – Directory layout
# ---------------------------------------------------------------------------

def prepare_directories(settings) -> bool:
    _section("Step 4 — Directory layout")

    ok = True
    dirs = [
        Path(settings.storage.get("base_path", "./data/pixieveil")),
        Path(settings.storage.get("temp_path",  "./data/tmp")),
        Path(settings.logging.get("file", "./data/log/pixieveil.log")).parent,
    ]

    for d in dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            _ok(str(d))
        except OSError as exc:
            _fail(f"{d}: {exc}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Step 4 – Model download
# ---------------------------------------------------------------------------

def download_model(model_root: Path) -> bool:
    """
    Download the nnUNet defacing model from Google Drive into *model_root*.

    The expected result is ``model_root / Dataset001_DEFACE/``.
    Returns True on success, False on failure (with instructions printed).
    """
    dataset_dir = model_root / _MODEL_DATASET

    if dataset_dir.is_dir():
        _ok(f"Model already present at {dataset_dir}")
        return True

    _info(f"Model not found at {dataset_dir} — downloading ...")
    _info(f"Source: {_MODEL_GDRIVE_URL}")

    try:
        import gdown
    except ImportError:
        _fail("gdown is not installed — cannot download model automatically.")
        _print_manual_download(model_root)
        return False

    try:
        gdown.download_folder(
            url=_MODEL_GDRIVE_URL,
            output=str(model_root),
            quiet=False,
            use_cookies=False,
        )
    except Exception as exc:
        _fail(f"gdown download failed: {exc}")
        _print_manual_download(model_root)
        return False

    if not dataset_dir.is_dir():
        _fail(
            f"Download completed but '{_MODEL_DATASET}' was not found in {model_root}."
        )
        _print_manual_download(model_root)
        return False

    _ok(f"Model downloaded to {dataset_dir}")
    return True


def _print_manual_download(model_root: Path) -> None:
    print(
        f"\n  Please download the model manually:\n"
        f"    URL : {_MODEL_GDRIVE_URL}\n"
        f"    Place the '{_MODEL_DATASET}' folder inside: {model_root}\n",
        file=sys.stderr,
    )


def prepare_model(settings, choice: str) -> bool:
    _section("Step 5 — nnUNet defacing model")

    if choice == DefacingChoice.NONE:
        _skip("Defacing disabled — skipping model setup.")
        return True

    from pixieveil.processing.defacer import Defacer

    defacer   = Defacer(config=settings.defacing)
    data_dir  = Path(settings.storage.get("base_path", "./data/pixieveil"))

    if defacer.model_dir is not None:
        model_root = defacer.model_dir
    else:
        model_root = data_dir.parent / "nnUNet"

    model_root.mkdir(parents=True, exist_ok=True)
    return download_model(model_root)


# ---------------------------------------------------------------------------
# Step 5 – Update settings.yaml
# ---------------------------------------------------------------------------

def update_settings(settings, choice: str, config_path: Optional[str]) -> bool:
    _section("Step 6 — Persist defacing choice to settings.yaml")

    enabled = choice != DefacingChoice.NONE

    # Find the actual settings file path
    try:
        import yaml
    except ImportError:
        _skip("PyYAML not available — skipping settings.yaml update.")
        _info(
            f"Set  defacing.enabled: {'true' if enabled else 'false'}  "
            "in settings.yaml manually."
        )
        return True

    if config_path:
        cfg_file = Path(config_path)
    else:
        # Replicate Settings.load() search logic
        candidates = [
            Path("config/settings.yaml"),
            Path("settings.yaml"),
        ]
        cfg_file = next((p for p in candidates if p.exists()), None)
        if cfg_file is None:
            _skip("settings.yaml not found — skipping auto-update.")
            return True

    try:
        text = cfg_file.read_text()
        data = yaml.safe_load(text)
        data.setdefault("defacing", {})["enabled"] = enabled
        cfg_file.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True))
        _ok(f"defacing.enabled = {enabled}  →  {cfg_file}")
    except Exception as exc:
        _fail(f"Could not update {cfg_file}: {exc}")
        return False

    return True


# ---------------------------------------------------------------------------
# Step 6 – Sanity checks
# ---------------------------------------------------------------------------

def sanity_checks(settings, choice: str) -> bool:
    _section("Step 7 — Sanity checks")

    ok = True

    try:
        from pixieveil.config import Settings as _S
        _S.load()
        _ok("settings.yaml loads without errors")
    except Exception as exc:
        _fail(f"settings.yaml failed to load: {exc}")
        ok = False

    core_packages = {"pydicom": "pydicom"}
    defacing_packages = {"SimpleITK": "simpleitk", "nibabel": "nibabel", "numpy": "numpy"}

    check_sets = list(core_packages.items())
    if choice != DefacingChoice.NONE:
        check_sets += list(defacing_packages.items())
    else:
        for module in defacing_packages:
            _skip(f"{module} — defacing disabled")

    for module, pkg in check_sets:
        try:
            mod = importlib.import_module(module)
            ver = getattr(mod, "__version__", "?")
            _ok(f"{module} {ver}")
        except ImportError:
            _fail(f"{module} not importable — install {pkg}")
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="PixieVeil installation helper")
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help="Path to settings.yaml (default: auto-discovered)",
    )
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Skip prompts and use current settings.yaml values",
    )
    parser.add_argument(
        "--download-model", action="store_true",
        help="Download the nnUNet defacing model and exit (skips all other steps)",
    )
    parser.add_argument(
        "--venv", metavar="PATH", nargs="?", const=".venv",
        help="Create a virtual environment at PATH (default: .venv) and run setup inside it",
    )
    args = parser.parse_args()

    # Venv: create and re-exec unless we're already running inside it.
    if args.venv:
        venv_path = Path(args.venv).resolve()
        if Path(sys.prefix).resolve() != venv_path:
            return _create_venv(venv_path)
        _ok(f"Running inside virtual environment at {venv_path}")

    print("PixieVeil setup")
    print(f"Python : {sys.version}")
    print(f"Prefix : {sys.prefix}")

    # Hard gate: Python version must be satisfied before anything else.
    if sys.version_info < (3, 12):
        print(
            f"\n{_FAIL}  Python >= 3.12 required "
            f"(running {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}).\n"
            f"  Re-run install.py with Python 3.12 or later.",
            file=sys.stderr,
        )
        return 1

    for _mod, _pkg in [("yaml", "PyYAML"), ("pydantic", "pydantic")]:
        try:
            importlib.import_module(_mod)
        except ImportError:
            print(
                f"\n{_FAIL}  {_pkg} is required to load settings.\n"
                f"  Run: pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 1

    try:
        from pixieveil.config import Settings
        settings = Settings.load(args.config) if args.config else Settings.load()
    except Exception as exc:
        print(f"\n{_FAIL}  Cannot load settings: {exc}", file=sys.stderr)
        return 1

    # --- Standalone model download
    if args.download_model:
        _section("Model download")
        from pixieveil.processing.defacer import Defacer
        defacer  = Defacer(config=settings.defacing)
        data_dir = Path(settings.storage.get("base_path", "./data/pixieveil"))
        model_root = defacer.model_dir if defacer.model_dir else data_dir.parent / "nnUNet"
        model_root.mkdir(parents=True, exist_ok=True)
        return 0 if download_model(model_root) else 1

    # Detect CUDA once; everything downstream reuses this result.
    _section("Step 0 — System detection")
    cuda_ver = _detect_cuda_version()
    if cuda_ver:
        _ok(f"CUDA {cuda_ver[0]}.{cuda_ver[1]} detected")
    else:
        _info("No CUDA detected — GPU option will not be available.")

    current_enabled = settings.defacing.get("enabled", False)

    choice = ask_defacing_choice(args.non_interactive, current_enabled, cuda_ver)

    results = [
        install_packages(choice, cuda_ver),
        check_python_and_deps(choice),
        prepare_directories(settings),
        prepare_model(settings, choice),
        update_settings(settings, choice, args.config),
        sanity_checks(settings, choice),
    ]

    _section("Summary")
    if all(results):
        print("\n  All steps passed. PixieVeil is ready to run.\n")
        return 0

    failed = sum(1 for r in results if not r)
    print(f"\n  {failed} step(s) failed — review errors above.\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
