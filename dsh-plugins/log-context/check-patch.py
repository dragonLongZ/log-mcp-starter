"""校验 cordis.patch.yml 里的 !!js 表达式：能解析、能求值、有合理默认值。

Loader 用自己的 `!!js` 处理器，这里用同样的语义（表达式在插件上下文中求值，
可用 Node 内置模块）单独跑一遍，避免装进 DSH 之后才发现表达式语法错误。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

PATCH = Path(__file__).parent / "cordis.patch.yml"


class Loader(yaml.SafeLoader):
    """把 !!js 节点还原成 {__jsExpr: "..."}，与 Loader 内部表示一致。"""


def _js_node(loader, node):
    return {"__jsExpr": loader.construct_scalar(node)}


Loader.add_constructor("!js", _js_node)
Loader.add_constructor("tag:yaml.org,2002:js", _js_node)
Loader.add_multi_constructor("!", lambda loader, suffix, node: _js_node(loader, node))


def collect(node, path=""):
    """深度收集 document 里所有 !!js 表达式及其路径。"""
    found = []
    if isinstance(node, dict):
        if "__jsExpr" in node:
            found.append((path, node["__jsExpr"]))
            return found
        for key, value in node.items():
            found.extend(collect(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(collect(value, f"{path}[{index}]"))
    return found


def main() -> int:
    raw = PATCH.read_text(encoding="utf-8")
    document = yaml.load(raw, Loader=Loader)
    assert isinstance(document, list), "patch 顶层应是列表（可能被 parse 成 dict）"
    exprs = collect(document)
    if not exprs:
        print("没有找到 !!js 表达式")
        return 1

    # 用一个不存在的 settings 文件求值：必须回落到默认值，而不是抛错。
    script_lines = []
    for path, expr in exprs:
        script_lines.append(
            "try { console.log(%s + ' => ' + JSON.stringify((%s))) }"
            " catch (e) { console.log(%s + ' => THREW: ' + e.message) }"
            % (json.dumps(path), expr, json.dumps(path))
        )

    workdir = Path(tempfile.mkdtemp())
    entry = workdir / "eval.mjs"
    entry.write_text("\n".join(script_lines), encoding="utf-8")
    result = subprocess.run(["node", str(entry)], capture_output=True, text=True)
    print(result.stdout.strip() or result.stderr.strip())

    ok = "THREW" not in (result.stdout + result.stderr)
    # 再验证一次「有 settings.json 时优先」：写一份临时设置，断言能读到。
    settings_dir = workdir / "home" / "log-context"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"mcpUrl": "http://example.test:9000/mcp", "token": "tok-123"}),
        encoding="utf-8",
    )
    entry2 = workdir / "eval2.mjs"
    entry2.write_text("\n".join(script_lines), encoding="utf-8")
    result2 = subprocess.run(
        ["node", str(entry2)],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "DSH_HOME": str(workdir / "home")},
    )
    print("--- 有 settings.json 时 ---")
    print(result2.stdout.strip() or result2.stderr.strip())
    ok = ok and "example.test" in result2.stdout and "tok-123" in result2.stdout

    shutil.rmtree(workdir, ignore_errors=True)
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
