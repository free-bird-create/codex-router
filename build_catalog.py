"""根据 models.toml 生成 ChatGPT 桌面端（Codex）用的模型目录 codex-models.json。

做法：原样保留服务商给的 GPT 目录，再为 models.toml 里的每个模型追加一个条目。
新条目以目录里的第一个模型为模板深拷贝，只覆盖少数字段，所以 Codex 需要的其他字段一个不缺。
"""

import argparse
import copy
import json
import os

import router

CONFIG_DIR = os.path.expanduser("~/.config/codex-router")

# 非 OpenAI 模型统一关掉 OpenAI 专有的能力，避免 Codex 发出对方不认识的参数或工具
NON_OPENAI_OVERRIDES = {
    "tool_mode": None,                     # code_mode 会发出自定义工具 exec，DeepSeek 只认 apply_patch
    "supports_search_tool": False,         # 托管的 web_search 工具只有 OpenAI 有
    "use_responses_lite": False,
    "prefer_websockets": False,
    "support_verbosity": False,
    "supports_reasoning_summaries": False,
    "supports_reasoning_summary_parameter": False,
    "supports_image_detail_original": False,
    "experimental_supported_tools": [],
    "service_tiers": [],
    "additional_speed_tiers": [],
    "default_service_tier": None,
    "upgrade": None,
    "availability_nux": None,
}


def build_entry(template, slug, model, priority):
    """以模板为底，生成一个模型条目。"""
    entry = copy.deepcopy(template)
    entry.update(NON_OPENAI_OVERRIDES)

    levels = {lv["effort"]: lv["description"] for lv in template["supported_reasoning_levels"]}
    efforts = model["efforts"]
    entry.update(
        slug=slug,
        display_name=model.get("display_name", slug),
        description=model.get("description", f"{slug} via codex-router ({model['provider']})"),
        context_window=model["context_window"],
        max_context_window=model["context_window"],
        input_modalities=model.get("input_modalities", ["text"]),
        supported_reasoning_levels=[{"effort": e, "description": levels.get(e, e)} for e in efforts],
        default_reasoning_level="medium" if "medium" in efforts else efforts[0],
        multi_agent_reasoning_effort=efforts[-1],
        priority=priority,
    )

    # 模板的系统提示词写着“based on GPT-6”，换成真实的模型名
    text = json.dumps(entry, ensure_ascii=False)
    text = text.replace("an agent based on GPT-6", f"an agent based on {entry['display_name']}")
    return json.loads(text)


def build_catalog(config, base):
    template = base["models"][0]
    extra = [build_entry(template, slug, model, priority=100 + i)
             for i, (slug, model) in enumerate(config["models"].items())]
    existing = {m["slug"] for m in base["models"]}
    clashes = existing & {m["slug"] for m in extra}
    if clashes:
        raise ValueError(f"models already in base catalog: {sorted(clashes)}")
    return {**base, "models": base["models"] + extra}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.join(CONFIG_DIR, "models.toml"))
    parser.add_argument("--base", default=os.path.join(CONFIG_DIR, "base-catalog.json"))
    parser.add_argument("--out", default=os.path.expanduser("~/.codex/codex-models.json"))
    args = parser.parse_args()

    with open(args.base, encoding="utf-8") as f:
        base = json.load(f)
    catalog = build_catalog(router.load_config(args.config), base)

    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.out)  # 原子替换：写到一半出错也不会留下坏文件
    print(f"wrote {len(catalog['models'])} models to {args.out}")


if __name__ == "__main__":
    main()
