"""pytest 适配层（V4-A，2026-09-17）。

按 ``tests/layers.py`` 给每个测试文件自动打 marker，使
``pytest -m fast`` / ``pytest -m "slow or integration"`` 可用。

注意：本文件只被 pytest 加载，**不影响** ``unittest discover``（当前项目无
pytest 时的主路径）。
"""
from __future__ import annotations

import pytest

from tests.layers import layer_of


def pytest_collection_modifyitems(config, items):
    for item in items:
        marker = getattr(pytest.mark, layer_of(item.path.name), None)
        if marker is not None:
            item.add_marker(marker)
