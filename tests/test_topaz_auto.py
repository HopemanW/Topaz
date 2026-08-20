import unittest
from pathlib import Path

import topaz_auto as ta


class TopazAutoTests(unittest.TestCase):
    def test_parse_resolution(self):
        self.assertEqual(ta.parse_resolution("1920x1080"), (1920, 1080))

    def test_parse_fractional_fps(self):
        self.assertAlmostEqual(ta.parse_fps("24000/1001"), 23.976023976, places=6)

    def test_filter_contains_topaz_stages(self):
        chain = ta.topaz_filter(
            1920, 1080, 60, "prob-4", "chf-3", 0, 1.0, 1, "enhance-first"
        )
        self.assertIn("tvai_up=model=prob-4", chain)
        self.assertIn("tvai_fi=model=chf-3:fps=60", chain)
        self.assertIn("pad=1920:1080", chain)
        self.assertLess(chain.index("tvai_up"), chain.index("tvai_fi"))

    def test_interpolate_first(self):
        chain = ta.topaz_filter(
            1920, 1080, 60, "prob-4", "chf-3", 0, 1.0, 1, "interpolate-first"
        )
        self.assertLess(chain.index("tvai_fi"), chain.index("tvai_up"))

    def test_nvenc_command(self):
        command = ta.build_command(
            "ffmpeg", Path("input.mkv"), Path("output.mp4"),
            width=1920, height=1080, fps=60,
            up_model="prob-4", fi_model="chf-3",
            device=0, vram=1.0, instances=1, order="enhance-first",
            encoder="hevc_nvenc", quality=18, overwrite=True,
        )
        joined = " ".join(command)
        self.assertIn("hevc_nvenc", command)
        self.assertIn("tvai_up=model=prob-4", joined)
        self.assertIn("tvai_fi=model=chf-3:fps=60", joined)
        self.assertEqual(command[-1], "output.mp4")


if __name__ == "__main__":
    unittest.main()
