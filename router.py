"""codex-router: 按模型名把 Codex 的 Responses 请求转发到不同服务商。仅用标准库。"""

import tomllib


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
