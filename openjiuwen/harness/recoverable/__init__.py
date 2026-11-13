# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Recoverable Harness: step checkpoints and confirmed rollback for DeepAgent.

Design: ``.agents/agent-docs/designs/active/D-20261113-1506-recoverable-harness``.
"""

from openjiuwen.harness.recoverable.harness import (
    DEFAULT_STORAGE_DIR,
    ROLLBACK_NOTE_SOURCE_KIND,
    RecoverableHarness,
)
from openjiuwen.harness.recoverable.models import (
    Checkpoint,
    CheckpointKind,
    Initiator,
    RollbackApproval,
    RollbackPlan,
    RollbackResult,
)
from openjiuwen.harness.recoverable.rail import CheckpointRail

__all__ = [
    "Checkpoint",
    "CheckpointKind",
    "CheckpointRail",
    "DEFAULT_STORAGE_DIR",
    "Initiator",
    "ROLLBACK_NOTE_SOURCE_KIND",
    "RecoverableHarness",
    "RollbackApproval",
    "RollbackPlan",
    "RollbackResult",
]
