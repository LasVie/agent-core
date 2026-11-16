# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The disk looks mostly free in these tests.

Below 20% free disk the harness deletes old checkpoints at every round end, so
on a full test machine histories would shrink at random. Tests of that rule
replace the disk usage themselves.
"""
from __future__ import annotations

import shutil

import pytest

_real_disk_usage = shutil.disk_usage


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    def roomy(path):
        usage = _real_disk_usage(path)
        return usage._replace(free=usage.total)

    monkeypatch.setattr(shutil, "disk_usage", roomy)
