"""router.py 纯函数的单元测试。运行：python3 -m unittest -v"""

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import build_catalog
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


class UpstreamTargetTest(unittest.TestCase):
    def test_base_url_with_v1(self):
        self.assertEqual(router.upstream_target("https://www.egpt.fun/v1", "/v1/responses"),
                         ("https", "www.egpt.fun", "/v1/responses"))

    def test_base_url_without_v1(self):
        self.assertEqual(router.upstream_target("https://api.deepseek.com", "/v1/responses"),
                         ("https", "api.deepseek.com", "/responses"))


class FakeUpstream(BaseHTTPRequestHandler):
    """假的上游服务商：记下收到的请求，然后分 3 块流式返回。"""

    def do_POST(self):
        self.server.seen = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for i in range(3):
            self.wfile.write(f"data: chunk{i}\n\n".encode())
            self.wfile.flush()

    def log_message(self, *args):
        pass


class ProxyTest(unittest.TestCase):
    """端到端：客户端 → router → 假上游，全程只在本机，不联网。"""

    def setUp(self):
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        up_port = self.upstream.server_address[1]
        path = write_toml(
            'default_provider = "fake"\n'
            f'[providers.fake]\nbase_url = "http://127.0.0.1:{up_port}/api"\nkey_env = "FAKE_KEY"\n'
            '[providers.down]\nbase_url = "http://127.0.0.1:1"\nkey_env = "FAKE_KEY"\n'
            '[models.small]\nprovider = "fake"\nefforts = ["low", "high"]\n'
            '[models.broken]\nprovider = "down"\nefforts = ["low"]\n')
        self.addCleanup(os.remove, path)
        self.router = router.RouterServer(("127.0.0.1", 0), router.load_config(path))
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        os.environ["FAKE_KEY"] = "upstream-secret"

    def tearDown(self):
        for server in (self.router, self.upstream):
            server.shutdown()
            server.server_close()
        os.environ.pop("FAKE_KEY", None)

    def post(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.router.server_address[1])
        conn.request("POST", "/v1/responses", json.dumps(body),
                     {"Authorization": "Bearer dummy", "Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, resp.read().decode()

    def test_forwards_and_streams(self):
        status, text = self.post({"model": "small", "reasoning": {"effort": "xhigh"}})
        self.assertEqual(status, 200)
        self.assertEqual(text, "data: chunk0\n\ndata: chunk1\n\ndata: chunk2\n\n")
        seen = self.upstream.seen
        self.assertEqual(seen["path"], "/api/responses")
        self.assertEqual(seen["auth"], "Bearer upstream-secret")
        self.assertEqual(seen["body"]["reasoning"]["effort"], "high")

    def test_missing_key_returns_openai_style_error(self):
        os.environ.pop("FAKE_KEY")
        status, text = self.post({"model": "small"})
        self.assertEqual(status, 500)
        self.assertIn("FAKE_KEY", json.loads(text)["error"]["message"])

    def test_unreachable_upstream_returns_502(self):
        status, _ = self.post({"model": "broken"})
        self.assertEqual(status, 502)

    def test_active_counter_returns_to_zero(self):
        self.post({"model": "small"})
        self.assertEqual(self.router.active, 0)


class BuildCatalogTest(unittest.TestCase):
    BASE = {"models": [{
        "slug": "gpt-x", "display_name": "GPT-X", "apply_patch_tool_type": "freeform",
        "base_instructions": "You are Codex, an agent based on GPT-6.",
        "supported_reasoning_levels": [{"effort": "low", "description": "fast"},
                                       {"effort": "xhigh", "description": "deep"}],
    }]}

    def setUp(self):
        self.catalog = build_catalog.build_catalog(router.load_config(EXAMPLE), self.BASE)
        self.glm = next(m for m in self.catalog["models"] if m["slug"] == "glm-5.4")

    def test_keeps_base_and_appends_models(self):
        slugs = [m["slug"] for m in self.catalog["models"]]
        self.assertEqual(slugs[0], "gpt-x")
        self.assertIn("deepseek-v4-pro", slugs)

    def test_entry_fields(self):
        self.assertEqual(self.glm["display_name"], "GLM-5.4")
        self.assertFalse(self.glm["supports_search_tool"])
        self.assertIsNone(self.glm["tool_mode"])
        self.assertEqual([lv["effort"] for lv in self.glm["supported_reasoning_levels"]],
                         ["low", "medium", "high"])
        self.assertEqual(self.glm["default_reasoning_level"], "medium")
        self.assertIn("based on GLM-5.4", self.glm["base_instructions"])

    def test_template_not_modified(self):
        self.assertEqual(self.BASE["models"][0]["apply_patch_tool_type"], "freeform")


if __name__ == "__main__":
    unittest.main()
