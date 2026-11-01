"""在不安装 MaiBot 依赖的前提下，从 DAFEIYU_HOST_SOURCE_ROOT 加载真实的 Host 源文件。

只有被列为 real 的 Host 模块是真实代码；其余 ``src.*`` 依赖都是惰性桩（属性访问返回 MagicMock）。
没有真实网络、模型或数据库调用。
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


def host_root() -> Path:
    root = os.environ.get("DAFEIYU_HOST_SOURCE_ROOT")
    if not root or not Path(root).is_dir():
        # 没有 Host 源码时明确跳过 Host 相关测试（unittest 会在结果里列出 skipped），不假装通过。
        raise unittest.SkipTest("DAFEIYU_HOST_SOURCE_ROOT is not set to a Host src directory")
    return Path(root)


def _fake_repair_json(text, *args, **kwargs):
    """模拟 json_repair 对被截断 JSON 的修复（补全引号/括号），使“可修复≠完整”的场景可测。"""
    import json
    import re

    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)(?:\s*```)?", text.strip(), flags=re.DOTALL | re.IGNORECASE)
    if fenced:  # 真实的 json_repair 同样会剥掉代码围栏
        text = fenced.group(1)
    for suffix in ("", "}", '"}', "]", "}]", '"}]', '"]'):
        try:
            json.loads(text + suffix)
            return text + suffix
        except ValueError:
            continue
    return '""'


class _StubModule(types.ModuleType):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__path__ = []  # 允许作为包

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        value = mock.MagicMock(name=f"{self.__name__}.{name}")
        setattr(self, name, value)
        return value


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, real: dict[str, Path]) -> None:
        self.real = real

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.real:
            return importlib.util.spec_from_file_location(fullname, self.real[fullname])
        if fullname == "src" or fullname.startswith("src."):
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        if fullname == "json_repair":
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        module = _StubModule(spec.name)
        if spec.name == "json_repair":
            module.repair_json = _fake_repair_json
        return module

    def exec_module(self, module):
        pass


def load(target: str, real_extra: tuple[str, ...] = ()) -> types.ModuleType:
    """加载 ``src/<target>.py``（点号路径，例如 ``services.memory_flow_service``）。"""

    root = host_root()
    names = {"src.common.token_optimizations": "common/token_optimizations.py"}
    for rel in (target, *real_extra):
        names[f"src.{rel}"] = f"{rel.replace('.', '/')}.py"
    real = {name: root / rel for name, rel in names.items()}
    finder = _Finder(real)
    with mock.patch.dict(sys.modules):
        for name in [n for n in sys.modules if n == "src" or n.startswith("src.")]:
            del sys.modules[name]
        sys.meta_path.insert(0, finder)
        try:
            module = importlib.import_module(f"src.{target}")
        finally:
            sys.meta_path.remove(finder)
    return module
