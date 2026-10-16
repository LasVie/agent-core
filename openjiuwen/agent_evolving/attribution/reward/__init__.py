# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reward building for the attribution pipeline (attribution.reward.v1).

This submodule is the attribution Rewward deep agent only; it is unrelated to
``agent_evolving/evaluator`` metric evaluation.
"""

from openjiuwen.agent_evolving.attribution.reward.reward_agent import (
    RewardBuilder,
    RewardSink,
    RewardSubmitRail,
    SubmitRewardTool,
    build_reward_agent,
    load_reward_prompt,
)
from openjiuwen.agent_evolving.attribution.reward.reward_schema import (
    SCHEMA_VERSION,
    RewardResult,
    RewardSchemaError,
    parse_reward_payload,
    reward_to_signals,
    validate_reward_payload,
)

__all__ = [
    "SCHEMA_VERSION",
    "RewardBuilder",
    "RewardResult",
    "RewardSchemaError",
    "RewardSink",
    "RewardSubmitRail",
    "SubmitRewardTool",
    "build_reward_agent",
    "load_reward_prompt",
    "parse_reward_payload",
    "reward_to_signals",
    "validate_reward_payload",
]
