#!/usr/bin/env python3
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_kill_notification_classifier_heuristic():
    from pubg_kill_notification_classifier import heuristic_predict

    crop = np.zeros((24, 180, 3), dtype=np.uint8)
    crop[:, :, 0] = 200  # blue bar
    label, conf = heuristic_predict(crop)
    assert label in ("kill", "uncertain", "map_blue", "hud_fp")
    assert conf >= 0.0


def test_kill_notification_dataset_save(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBG_KILL_NOTIFICATION_DATASET", str(tmp_path))
    from pubg_kill_notification_dataset import load_manifest, save_crop

    crop = np.zeros((20, 100, 3), dtype=np.uint8)
    vod = tmp_path / "v.mp4"
    vod.write_bytes(b"x" * 64)
    key = save_crop(crop, video_path=vod, start_sec=10.0, box=[1, 2, 50, 20], score=0.5)
    assert key is not None
    assert len(load_manifest()) == 1


def test_audio_dedup_signature():
    from vod_event_dedup import _signature_distance

    a = (0.5, 0.1, 0.0, 0.0, 0.0)
    b = (0.5, 0.1, 0.0, 0.0, 0.0)
    assert _signature_distance(a, b) == 0.0
