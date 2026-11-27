"""codex-router: 按模型名把 Codex 的 Responses 请求转发到不同服务商。仅用标准库。"""

import argparse
import http.client
import json
import os
import sys
import threading
import time
import tomllib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ── 第 1 段：配置 ─────────────────────────────────────────

# 思考强度从低到高的顺序，clamp_effort 靠它比较“高低”
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def load_config(path):
    """读取 models.toml 并检查结构，返回配置 dict；不合法时抛出 ValueError。"""
    with open(path, "rb") as f:
        config = tomllib.load(f)

    providers = config.get("providers", {})
    models = config.get("models", {})

    default = config.get("default_provider")
    if default not in providers:
        raise ValueError(f"default_provider {default!r} is not defined in [providers]")

    for name, provider in providers.items():
        for key in ("base_url", "key_env"):
            if key not in provider:
                raise ValueError(f"provider {name!r} is missing {key!r}")

    for name, model in models.items():
        if model.get("provider") not in providers:
            raise ValueError(f"model {name!r} uses unknown provider {model.get('provider')!r}")
        efforts = model.get("efforts", [])
        unknown = [e for e in efforts if e not in EFFORT_ORDER]
        if not efforts or unknown:
            raise ValueError(f"model {name!r} has invalid efforts {efforts!r}")

    config.setdefault("models", {})
    return config


# ── 第 2 段：纯函数（不碰网络，全部可单测）──────────────────

def resolve(config, model):
    """返回 (服务商名, 服务商配置, 允许的思考强度)。

    表里没有的模型交给 default_provider，此时思考强度为 None，表示原样转发。
    """
    entry = config["models"].get(model)
    if entry is None:
        name = config["default_provider"]
        return name, config["providers"][name], None
    name = entry["provider"]
    return name, config["providers"][name], entry["efforts"]


def clamp_effort(requested, allowed):
    """把请求的思考强度对齐到模型支持的档位。

    规则：取 allowed 里“不超过 requested 的最高档”；如果都比它高，就取最低档。
    例：allowed = [low, medium, high] 时，xhigh → high，minimal → low。
    """
    if requested in allowed:
        return requested
    ranked = sorted(allowed, key=EFFORT_ORDER.index)
    if requested not in EFFORT_ORDER:
        return ranked[-1]
    level = EFFORT_ORDER.index(requested)
    lower = [e for e in ranked if EFFORT_ORDER.index(e) <= level]
    return lower[-1] if lower else ranked[0]


def prepare_body(body, allowed):
    """返回对齐思考强度后的新请求体，不修改传入的 body。"""
    effort = body.get("reasoning", {}).get("effort")
    if allowed is None or effort is None:
        return body
    reasoning = {**body["reasoning"], "effort": clamp_effort(effort, allowed)}
    return {**body, "reasoning": reasoning}


# 不能原样转发给上游的请求头（统一用小写比较）
DROPPED_HEADERS = {
    "authorization",     # 换成上游自己的 Key
    "host",              # 由 http.client 按上游地址重新生成
    "content-length",    # 请求体可能被改过，由 http.client 重新计算
    "accept-encoding",   # 要求上游不压缩，方便逐块透传和看日志
    # 逐跳头（hop-by-hop）：只对“当前这一段连接”有效，代理不应转发
    "connection", "keep-alive", "proxy-connection", "te",
    "trailer", "transfer-encoding", "upgrade",
}


def prepare_headers(incoming, api_key):
    """过滤掉不该转发的请求头，并注入上游的 Authorization。"""
    headers = {k: v for k, v in incoming.items() if k.lower() not in DROPPED_HEADERS}
    headers["Authorization"] = f"Bearer {api_key}"
    return headers


def upstream_target(base_url, request_path):
    """把本地路径映射到上游：/v1/responses + https://x.com → (https, x.com, /responses)。

    本地统一以 /v1 开头；上游的版本前缀由 base_url 自己决定（DeepSeek 没有 /v1）。
    """
    url = urllib.parse.urlsplit(base_url)
    suffix = request_path.removeprefix("/v1")
    return url.scheme, url.netloc, url.path.rstrip("/") + suffix


# ── 第 3 段：转发 ────────────────────────────────────────

UPSTREAM_TIMEOUT = 600  # 秒；推理模型可能长时间不吐字
RESPONSE_SKIPPED_HEADERS = DROPPED_HEADERS - {"authorization", "accept-encoding"}


class Handler(BaseHTTPRequestHandler):
    """每个请求一个线程。HTTP/1.0：响应结束即关闭连接，流式输出天然以断开为结尾。"""

    server_version = "codex-router"

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"status": "ok"})
        else:
            self.send_error_json(404, f"unknown path {self.path}")

    def do_POST(self):
        started = time.monotonic()
        self.server.request_started()
        try:
            self.forward(started)
        finally:
            self.server.request_finished()

    def forward(self, started):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self.send_error_json(400, "request body is not valid JSON")

        model = body.get("model", "")
        name, provider, efforts = resolve(self.server.config, model)
        api_key = os.environ.get(provider["key_env"])
        if not api_key:
            return self.send_error_json(500, f"environment variable {provider['key_env']} is not set")

        scheme, host, path = upstream_target(provider["base_url"], self.path)
        payload = json.dumps(prepare_body(body, efforts)).encode()
        conn_class = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = conn_class(host, timeout=UPSTREAM_TIMEOUT)
        try:
            conn.request("POST", path, payload, prepare_headers(self.headers, api_key))
            upstream = conn.getresponse()
            self.relay(upstream)
            status = upstream.status
        except OSError as e:  # 连不上、超时、TLS 失败……
            self.send_error_json(502, f"upstream {name} unreachable: {e.__class__.__name__}")
            status = 502
        finally:
            conn.close()
        log(f"{model or '?'} -> {name} {status} {time.monotonic() - started:.1f}s")

    def relay(self, upstream):
        """把上游响应原样写回：状态码、响应头、正文逐块透传（SSE 不会被攒成一大块）。"""
        self.send_response_only(upstream.status, upstream.reason)
        for key, value in upstream.getheaders():
            if key.lower() not in RESPONSE_SKIPPED_HEADERS:
                self.send_header(key, value)
        self.end_headers()
        try:
            while chunk := upstream.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 用户在 ChatGPT 里点了停止，客户端先断开了

    def send_json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, status, message):
        """用 OpenAI 的错误格式返回，ChatGPT 界面能直接显示原因。"""
        log(f"error {status}: {message}")
        self.send_json(status, {"error": {"message": f"[codex-router] {message}",
                                          "type": "router_error"}})

    def log_message(self, format, *args):
        pass  # 关掉默认的访问日志：我们自己每个请求打一行，且绝不打印请求头和请求体


def log(message):
    print(message, file=sys.stderr, flush=True)


class RouterServer(ThreadingHTTPServer):
    """在标准服务器上加两样东西：配置，以及“进行中请求数”（第 4 段用它判断空闲）。"""

    daemon_threads = True

    def __init__(self, address, config, bind_and_activate=True):
        super().__init__(address, Handler, bind_and_activate)
        self.config = config
        self.active = 0
        self.last_active = time.monotonic()
        self.lock = threading.Lock()

    def request_started(self):
        with self.lock:
            self.active += 1

    def request_finished(self):
        with self.lock:
            self.active -= 1
            self.last_active = time.monotonic()


# ── 第 4 段：生命周期 ─────────────────────────────────────

DEFAULT_CONFIG = os.path.expanduser("~/.config/codex-router/models.toml")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=4000)
    args = parser.parse_args()

    server = RouterServer(("127.0.0.1", args.port), load_config(args.config))
    log(f"listening on 127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
