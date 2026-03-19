"""Retrain entrypoint — trains all models and exits.

Used by EventBridge-triggered ECS task every 4 hours.
Same Docker image as bot, different command.
"""

import logging
import sys
import time

sys.path.insert(0, "src")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    logger.info("retrain_started")
    start = time.time()

    from polybot.ml.trainer import train_all

    results = train_all()

    duration = round(time.time() - start, 1)
    deployed = sum(1 for r in results if r.deployed)

    for r in results:
        status = "DEPLOYED" if r.deployed else f"SKIPPED: {r.error}"
        logger.info(f"  {r.pair}: brier={r.val_brier:.4f} auc={r.val_auc:.4f} {status}")

    logger.info(f"retrain_complete duration={duration}s deployed={deployed}/{len(results)}")


if __name__ == "__main__":
    main()
