"""Run one nnU-Net prediction in its own process.

``Defacer.run_nnunet_inference`` starts this as a child process (in its own
process group) instead of calling ``nnUNetPredictor`` in a worker thread: a
thread cannot be interrupted, so Ctrl-C used to wait for inference to
finish — minutes on a CPU. A child process (and nnU-Net's own pool workers
in its group) can be terminated on shutdown.

    python -m pixieveil.processing.nnunet_worker MODEL_FOLDER IN_DIR OUT_DIR \
        --device cpu [--mirroring]

Exit status 0 on success; anything else is a failure (details on stderr).
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("model_folder")
    parser.add_argument("in_dir")
    parser.add_argument("out_dir")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--mirroring", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s - nnunet_worker - %(message)s")

    import torch
    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

    device = torch.device(args.device)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*old nnU-Net plans format.*",
                                category=UserWarning)
        predictor = nnUNetPredictor(
            tile_step_size=0.5,
            use_gaussian=True,
            use_mirroring=args.mirroring,
            perform_everything_on_device=device.type == "cuda",
            device=device,
            verbose=False,
            allow_tqdm=False,
        )
        predictor.initialize_from_trained_model_folder(
            args.model_folder, use_folds=("all",), checkpoint_name="checkpoint_final.pth",
        )
    predictor.predict_from_files(args.in_dir, args.out_dir,
                                 save_probabilities=False, overwrite=True)
    logger.info("prediction written to %s", args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
