"""router.py 纯函数的单元测试。运行：python3 -m unittest -v"""

import os
import tempfile
import unittest

import router

EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.example.toml")


def write_toml(text):
    """把一段 TOML 写进临时文件，返回路径（由调用方负责删除）。"""
    f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    f.write(text)
    f.close()
    return f.name


class LoadConfigTest(unittest.TestCase):
    def test_example_is_valid(self):
        config = router.load_config(EXAMPLE)
        self.assertEqual(config["default_provider"], "egpt")

    def assert_invalid(self, text):
        path = write_toml(text)
        self.addCleanup(os.remove, path)
        with self.assertRaises(ValueError):
            router.load_config(path)

    def test_unknown_default_provider(self):
        self.assert_invalid('default_provider = "nope"\n'
                            '[providers.a]\nbase_url = "x"\nkey_env = "K"\n')

    def test_model_with_unknown_provider(self):
        self.assert_invalid('default_provider = "a"\n'
                            '[providers.a]\nbase_url = "x"\nkey_env = "K"\n'
                            '[models.m]\nprovider = "b"\nefforts = ["low"]\n')

    def test_unknown_effort_name(self):
        self.assert_invalid('default_provider = "a"\n'
                            '[providers.a]\nbase_url = "x"\nkey_env = "K"\n'
                            '[models.m]\nprovider = "a"\nefforts = ["turbo"]\n')


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.config = router.load_config(EXAMPLE)

    def test_listed_model(self):
        name, provider, efforts = router.resolve(self.config, "glm-5.4")
        self.assertEqual(name, "glm")
        self.assertEqual(provider["key_env"], "GLM_API_KEY")
        self.assertEqual(efforts, ["low", "medium", "high"])

    def test_unlisted_model_falls_back_to_default(self):
        name, _, efforts = router.resolve(self.config, "gpt-6.2-astra")
        self.assertEqual(name, "egpt")
        self.assertIsNone(efforts)


class ClampEffortTest(unittest.TestCase):
    ALLOWED = ["low", "medium", "high"]

    def test_supported_level_unchanged(self):
        self.assertEqual(router.clamp_effort("medium", self.ALLOWED), "medium")

    def test_too_high_goes_down(self):
        for level in ("xhigh", "max", "ultra"):
            self.assertEqual(router.clamp_effort(level, self.ALLOWED), "high")

    def test_too_low_goes_up(self):
        self.assertEqual(router.clamp_effort("minimal", self.ALLOWED), "low")

    def test_gap_picks_lower_neighbour(self):
        self.assertEqual(router.clamp_effort("medium", ["low", "high"]), "low")

    def test_unknown_level_uses_highest(self):
        self.assertEqual(router.clamp_effort("turbo", self.ALLOWED), "high")


class PrepareBodyTest(unittest.TestCase):
    def test_clamps_and_keeps_original(self):
        body = {"model": "glm-5.4", "reasoning": {"effort": "xhigh", "summary": "auto"}}
        new = router.prepare_body(body, ["low", "medium", "high"])
        self.assertEqual(new["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(body["reasoning"]["effort"], "xhigh")  # 原对象没被改

    def test_default_provider_passes_through(self):
        body = {"reasoning": {"effort": "ultra"}}
        self.assertIs(router.prepare_body(body, None), body)

    def test_no_reasoning_field(self):
        body = {"model": "glm-5.4"}
        self.assertEqual(router.prepare_body(body, ["low"]), body)


class PrepareHeadersTest(unittest.TestCase):
    def test_replaces_auth_and_drops_unsafe_headers(self):
        incoming = {
            "Authorization": "Bearer client-key",
            "Host": "127.0.0.1:4000",
            "Content-Length": "123",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
            "User-Agent": "codex",
            "Content-Type": "application/json",
        }
        headers = router.prepare_headers(incoming, "upstream-key")
        self.assertEqual(headers, {
            "User-Agent": "codex",
            "Content-Type": "application/json",
            "Authorization": "Bearer upstream-key",
        })


if __name__ == "__main__":
    unittest.main()
