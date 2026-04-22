#!/usr/bin/env python3
"""
PixieVeil Installation & Environment Setup
==========================================

Interactive setup script. Run once after cloning / updating the repo:

    python install.py [--config PATH] [--non-interactive]

Steps
-----
  1. Ask whether defacing should be enabled and which compute backend to use.
  2. Install torch and nnUNetv2 for the chosen backend (pip subprocess).
  3. Verify Python version and imported packages.
  4. Create required runtime directories.
  5. Download the nnUNet defacing model from Google Drive if missing.
  6. Final sanity checks (nnUNetv2_predict on PATH, core imports).

Exit code 0 on success, non-zero on any failure.
"""

import argparse
import importlib
import shutil
import subprocess
import sys
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
_TORCH_CUDA = ["torch", "torchvision", "torchaudio"]  # default PyPI wheels include CUDA

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
# Step 0 – Interactive defacing prompt
# ---------------------------------------------------------------------------

class DefacingChoice:
    NONE = "none"
    CPU  = "cpu"
    CUDA = "cuda"


def ask_defacing_choice(non_interactive: bool, current_enabled: bool) -> str:
    """
    Ask the user whether to enable defacing and which backend to use.

    Returns one of DefacingChoice.{NONE, CPU, CUDA}.  Always returns NONE when
    the running Python is older than 3.12.
    """
    _section("Step 0 — Defacing configuration")

    if sys.version_info < (3, 12):
        print(
            f"\n  WARNING: Python {sys.version_info.major}.{sys.version_info.minor} detected.\n"
            f"  Defacing requires Python >= 3.12.\n"
            f"  Defacing installation will be skipped.\n"
            f"  Re-run install.py with Python 3.12 or later to enable defacing.\n",
            file=sys.stderr,
        )
        return DefacingChoice.NONE

    if non_interactive:
        choice = DefacingChoice.CPU if current_enabled else DefacingChoice.NONE
        _info(f"Non-interactive mode: using current setting "
              f"({'enabled/cpu' if current_enabled else 'disabled'}).")
        return choice

    print("""
  Defacing removes facial features from head CT scans (requires nnUNetv2).

  Options:
    0 – No defacing
    1 – Enable defacing, CPU only
    2 – Enable defacing, CUDA (GPU)
""")
    default = "1" if current_enabled else "0"
    while True:
        raw = input(f"  Your choice [0/1/2] (current: {default}): ").strip()
        if raw == "":
            raw = default
        if raw == "0":
            return DefacingChoice.NONE
        if raw == "1":
            return DefacingChoice.CPU
        if raw == "2":
            return DefacingChoice.CUDA
        print("  Please enter 0, 1, or 2.")


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


def install_packages(choice: str) -> bool:
    _section("Step 1 — Package installation")

    if choice == DefacingChoice.NONE:
        _skip("Defacing disabled — skipping torch / nnUNetv2 installation.")
        return True

    # --- PyTorch ---
    torch_ver = _check_package("torch")
    if torch_ver:
        _ok(f"torch {torch_ver} already installed — skipping.")
    else:
        _info(
            f"torch not found — installing CPU-only build"
            if choice == DefacingChoice.CPU
            else f"torch not found — installing CUDA build"
        )
        torch_args = _TORCH_CPU if choice == DefacingChoice.CPU else _TORCH_CUDA
        if not _pip_install(torch_args, f"PyTorch ({choice.upper()})"):
            return False

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
    _section("Step 2 — Runtime requirements")

    ok = True

    vi = sys.version_info
    if vi >= (3, 12):
        _ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")
    else:
        _fail(f"Python >= 3.12 required (running {vi.major}.{vi.minor}.{vi.micro})")
        ok = False

    if choice == DefacingChoice.NONE:
        _skip("Defacing disabled — skipping torch / nnUNetv2 checks.")
        return ok

    try:
        import torch
        _ok(f"torch {torch.__version__}")
    except ImportError:
        _fail("torch not importable — run install.py again or install PyTorch manually.")
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
    _section("Step 3 — Directory layout")

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
    _section("Step 4 — nnUNet defacing model")

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
    _section("Step 5 — Persist defacing choice to settings.yaml")

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
    _section("Step 6 — Sanity checks")

    ok = True

    if choice != DefacingChoice.NONE:
        # Also look next to sys.executable (covers venvs not activated in PATH)
        venv_bin = Path(sys.executable).parent / "nnUNetv2_predict"
        bin_path = shutil.which("nnUNetv2_predict") or (str(venv_bin) if venv_bin.exists() else None)
        if bin_path:
            _ok(f"nnUNetv2_predict found at {bin_path}")
        else:
            _fail(
                "nnUNetv2_predict not found — "
                "ensure nnUNetv2 is installed in the active environment."
            )
            ok = False

    try:
        from pixieveil.config import Settings as _S
        _S.load()
        _ok("settings.yaml loads without errors")
    except Exception as exc:
        _fail(f"settings.yaml failed to load: {exc}")
        ok = False

    packages = {
        "pydicom":   "pydicom",
        "SimpleITK": "simpleitk",
        "nibabel":   "nibabel",
        "numpy":     "numpy",
    }
    for module, pkg in packages.items():
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
    args = parser.parse_args()

    print("PixieVeil setup")
    print(f"Python : {sys.version}")
    print(f"Prefix : {sys.prefix}")

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

    current_enabled = settings.defacing.get("enabled", False)

    # --- Step 0: ask user
    choice = ask_defacing_choice(args.non_interactive, current_enabled)

    results = [
        install_packages(choice),
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
