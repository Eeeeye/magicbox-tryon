import unittest

import numpy as np

from PIL import Image, ImageDraw

from inference.masking import GarmentRegionMissing, blending_mask, garment_mask, prepare_garment


class GarmentMaskTest(unittest.TestCase):
    def test_downward_hand_is_preserved_when_upper_sleeves_change(self):
        labels = np.zeros((256, 192), dtype=np.uint8)
        labels[35:135, 66:122] = 4
        labels[45:175, 122:140] = 14
        labels[15:36, 75:105] = 11
        mask = np.asarray(garment_mask(labels, "upper"))
        self.assertEqual(mask[80, 125], 255)
        self.assertEqual(mask[169, 130], 0)
        self.assertEqual(mask[28, 87], 0)
        self.assertEqual(mask[230, 12], 0)

    def test_lower_mask_keeps_overlapping_arm_and_shoes(self):
        labels = np.zeros((256, 192), dtype=np.uint8)
        labels[96:195, 60:135] = 6
        labels[165:213, 72:90] = 12
        labels[122:132, 70:81] = 15
        labels[213:230, 70:95] = 9
        mask = np.asarray(garment_mask(labels, "lower"))
        self.assertEqual(mask[180, 77], 255)
        self.assertEqual(mask[126, 76], 0)
        self.assertEqual(mask[218, 77], 0)

    def test_short_skirt_preserves_skin_below_original_shorts(self):
        labels = np.zeros((256, 192), dtype=np.uint8)
        labels[90:130, 55:130] = 6
        labels[131:220, 65:80] = 12
        synthesis = garment_mask(labels, "lower")
        self.assertEqual(np.asarray(synthesis)[201, 72], 255)
        mask = np.asarray(blending_mask(synthesis, labels, short_lower=True))
        self.assertEqual(mask[112, 80], 255)
        self.assertEqual(mask[201, 72], 0)

    def test_flat_lay_margins_and_sleeveless_outline(self):
        image = Image.new("RGB", (200, 300), "white")
        ImageDraw.Draw(image).polygon([(80, 75), (120, 75), (145, 200), (55, 200)], fill="purple")
        cropped, exposed_arms = prepare_garment(image)
        self.assertLess(cropped.height, 150)
        self.assertTrue(exposed_arms)

    def test_missing_garment_returns_input_error(self):
        for category in ("upper", "lower", "dress"):
            with self.subTest(category=category), self.assertRaises(GarmentRegionMissing):
                garment_mask(np.zeros((128, 128), dtype=np.uint8), category)
