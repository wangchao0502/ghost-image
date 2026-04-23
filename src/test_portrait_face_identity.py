from __future__ import annotations

import unittest
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, (Path(__file__).resolve().parent).as_posix())
import portrait_filter_crop as pfc


class FaceIdentityTests(unittest.TestCase):
    def test_extract_face_embedding_returns_unit_norm(self) -> None:
        image = np.zeros((120, 120, 3), dtype=np.uint8)
        image[20:100, 20:100] = 180
        bbox = [20.0, 20.0, 100.0, 100.0]

        emb = pfc.extract_face_embedding(image, bbox, embedding_size=32)

        self.assertIsNotNone(emb)
        assert emb is not None
        self.assertEqual(32 * 32, emb.shape[0])
        self.assertAlmostEqual(1.0, float(np.linalg.norm(emb)), places=5)

    def test_extract_face_embedding_invalid_bbox_returns_none(self) -> None:
        image = np.zeros((80, 80, 3), dtype=np.uint8)
        self.assertIsNone(pfc.extract_face_embedding(image, [50, 50, 50, 50], embedding_size=32))
        self.assertIsNone(pfc.extract_face_embedding(image, [100, 100, 140, 140], embedding_size=32))

    def test_best_face_similarity_selects_best_face(self) -> None:
        image = np.zeros((128, 128, 3), dtype=np.uint8)
        # Candidate A: horizontal stripes
        image[16:64, 16:64] = 20
        image[16:64:2, 16:64] = 220
        # Candidate B: checkerboard
        image[64:112, 64:112] = 20
        image[64:112:2, 64:112:2] = 220

        face_boxes = [
            [16.0, 16.0, 64.0, 64.0, 1.0],
            [64.0, 64.0, 112.0, 112.0, 1.0],
        ]
        target = pfc.extract_face_embedding(image, [16.0, 16.0, 64.0, 64.0], embedding_size=32)
        assert target is not None

        score, best_bbox = pfc.best_face_similarity(
            image=image,
            face_boxes=face_boxes,
            target_embedding=target,
            embedding_size=32,
        )

        self.assertGreater(score, 0.95)
        self.assertEqual([16.0, 16.0, 64.0, 64.0], best_bbox)


if __name__ == "__main__":
    unittest.main()
