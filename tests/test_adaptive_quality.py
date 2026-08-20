import unittest

import adaptive_quality as aq


def profile(*, width=1280, height=720, fps=30.0, codec="h264", bitrate=4_000_000):
    return aq.MediaProfile(
        width=width,
        height=height,
        fps=fps,
        codec=codec,
        bit_rate_bps=bitrate,
        duration_seconds=60.0,
        pixel_format="yuv420p",
        field_order="progressive",
    )


class AdaptiveQualityTests(unittest.TestCase):
    def test_bits_per_pixel_frame(self):
        p = profile(width=100, height=100, fps=25, bitrate=250_000)
        self.assertAlmostEqual(p.bits_per_pixel_frame, 1.0)

    def test_clean_gradient_has_neutral_blockiness(self):
        width = height = 32
        frame = bytes((x * 7 + y * 3) % 256 for y in range(height) for x in range(width))
        metrics = aq.visual_metrics_from_frames([frame], width, height)
        self.assertGreater(metrics.edge_energy, 0)
        self.assertLess(metrics.blockiness, 1.35)

    def test_block_edges_raise_blockiness(self):
        width = height = 32
        pixels = []
        for y in range(height):
            for x in range(width):
                block = ((x // 8) + (y // 8)) % 2
                pixels.append(30 if block == 0 else 220)
        metrics = aq.visual_metrics_from_frames([bytes(pixels)], width, height)
        self.assertGreater(metrics.blockiness, 2.0)

    def test_low_quality_compressed_prefers_iris(self):
        p = profile(width=640, height=360, fps=30, bitrate=280_000)
        v = aq.VisualMetrics(8, 0.025, 0.02, 1.55, 0.08)
        decision = aq.select_model(p, v)
        self.assertEqual(decision.family, "iris-lq")
        self.assertEqual(decision.model_id, "iris-3")

    def test_medium_quality_prefers_artemis_mq(self):
        p = profile(width=1280, height=720, fps=30, bitrate=2_500_000)
        v = aq.VisualMetrics(8, 0.045, 0.025, 1.08, 0.10)
        decision = aq.select_model(p, v)
        self.assertIn(decision.family, {"artemis-mq", "proteus"})

    def test_good_1080p_prefers_proteus_or_hq(self):
        p = profile(width=1920, height=1080, fps=30, bitrate=14_000_000)
        v = aq.VisualMetrics(8, 0.065, 0.04, 1.02, 0.12)
        decision = aq.select_model(p, v)
        self.assertIn(decision.family, {"proteus", "artemis-hq"})

    def test_faces_hint_biases_iris(self):
        p = profile(width=1280, height=720, fps=30, bitrate=2_000_000)
        v = aq.VisualMetrics(8, 0.04, 0.02, 1.12, 0.04)
        decision = aq.select_model(p, v, content="faces")
        self.assertTrue(decision.family.startswith("iris-"))

    def test_animation_hint_uses_aliasing_model(self):
        decision = aq.select_model(profile(), None, content="animation")
        self.assertEqual(decision.family, "artemis-aa")
        self.assertEqual(decision.model_id, "aaa-10")

    def test_installed_model_resolution(self):
        decision = aq.select_model(
            profile(width=640, height=360, bitrate=250_000),
            aq.VisualMetrics(8, 0.02, 0.02, 1.6, 0.05),
            available_models={"iris-2", "prob-4"},
        )
        self.assertEqual(decision.model_id, "iris-2")
        self.assertEqual(decision.available_model_count, 2)

    def test_missing_family_falls_back_to_installed_proteus(self):
        model, installed = aq.resolve_model_id("artemis-hq", {"prob-4"})
        self.assertEqual(model, "prob-4")
        self.assertTrue(installed)


if __name__ == "__main__":
    unittest.main()
