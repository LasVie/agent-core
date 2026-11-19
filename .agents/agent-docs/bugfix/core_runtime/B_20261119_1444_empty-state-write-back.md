---
id: B_20261119_1444_empty-state-write-back
title: 写回空状态不生效，vcs 回退后留下之后才写的状态
type: B
author: Song Haoran
author_email: song.haoran@h-partners.com
modules:
  - openjiuwen/core/session
  - openjiuwen/harness/recoverable
files:
  - openjiuwen/core/session/state/base.py
  - openjiuwen/harness/recoverable/harness.py
  - tests/unit_tests/core/session/vcs/test_rewind_empty_state.py
designs: []
design_note: core session 的缺陷修复，无独立设计；在实施 D-20261113-1506-recoverable-harness 时发现，过程见 F_20261113_1706_recoverable-harness。
---

# 写回空状态不生效修复记录

本文的"写回"指把一份保存下来的状态放回 Session，做法是调用状态对象的 `set_state(state)`。
vcs 的 `rewind`（回到某个历史点）和各种 checkpointer 的恢复都通过它写回。

## 现象与影响

例子：会话一轮开始时，全局状态里只有对话上下文。vcs 存快照时会把上下文单独存走，所以这个历史点存下的全局状态是 `{}`。
这一轮里权限 rail 停下来等用户确认 `deploy`，全局状态里多了 `__react_agent_interruption__`。
之后回到一轮开始的那个历史点：上下文回去了，但 `__react_agent_interruption__` 还在。

- **预期：** 回到历史点后，状态与该点存下的完全一致，之后写的键都没了。
- **实际：** 目标点的状态为空时，之后写的键全部留下；目标点的状态不为空时正常。
- **后果（④ 中实测）：** 回退后宿主发来对旧确认的批准，被丢弃的 `deploy` 照样执行；发来新消息，新消息被当成对旧确认的回答。
  ④ 曾在写回后自行删掉多出的键绕开这个问题（见 F_20261113_1706_recoverable-harness 的 2026-11-17 修订）。
- **范围：** 所有基于 `InMemoryStateLike` 的状态。Agent 的全局状态和 Agent 状态两部分都受影响；工作流的各部分状态同理。

## 关联设计与当前契约

无独立设计。修复后的契约写在 [S_01 运行时规约](../../specs/core_runtime/S_01_runtime-data-compatibility.md) 第 1 节：
`set_state(None)` 表示没有要写回的内容、保持原状；`set_state({})` 表示写回一个空状态。
④ 依赖这一点保证"写回后状态与目标完全一致"（S_20 不变量 6）。

## 根因与修复决策

根因在 `InMemoryStateLike.set_state`：

```python
if state:              # {} 也被当成"没有内容"
    self._state = state
```

Agent 状态（`agent_state.py` 的 `StateCollection.set_state`）把快照拆成全局和 Agent 两部分分别写回。快照里缺这一部分时传入 `None`，这一部分是空的时传入 `{}`。
原来的判断把两种情况都当成"不写回"。

修复：改为 `if state is not None`。缺这一部分时照旧不动；这一部分是空的时，写回为空。
工作流状态（`workflow_state.py` 的 `CommitState.set_state`）对全局状态本来就是按 `is not None` 判断的，改后两处判断方式一致。
④ 写回后删除多余状态键的 `_drop_later_state` 随之删除。

考虑过、没有采用的做法：

- **只改 vcs 的 `_apply`：** 写回空状态时绕过 `set_state`，直接清空内部字典。需要碰状态对象的私有字段，而且 `set_state` 的问题仍然存在。
- **不改 core，保留 ④ 的绕法：** ④ 的结果是对的，但别的地方再用 vcs 的 `rewind` 或 `fork` 时，还会遇到同样的问题。
  ④ 的绕法也只处理了全局状态，没有处理 Agent 状态。

行为变化只出现在一种情况：写回的某一部分是 `{}`，而当前这一部分不为空。checkpointer 恢复时，会话一般是新建的，状态本来就为空，
所以结果不变。这是推断，依据是下面的回归测试修改前后结果一致；Redis checkpointer 没有实测。

## 回归证据与适用范围

候选：分支 `feature/recoverable-harness`，基于 45bb79c 修改。环境：macOS，仓库 `.venv`（Python 3.11）。

| 验证 | 做法 | 结果 |
| --- | --- | --- |
| 复现 | 修改前，用真实 AgentSession、ContextEngine 和 kv 后端，在状态为空和不为空时各存一个历史点，写入 `later` 后回到该点 | 状态为空时 `later` 留下，测试失败；不为空时通过 |
| 只靠 core 修复是否够用 | 用临时 pytest 插件替换 `set_state`，同时让 ④ 的 `_drop_later_state` 什么都不做，跑 `tests/unit_tests/harness/recoverable` | 不修 core：3 个失败（`test_paused.py`）；修 core：71 个全部通过 |
| 对其他调用方的影响 | 同一插件，修改前后各跑一次 `tests/unit_tests/core/{session,single_agent,workflow,agent,multi_agent,runner}` | 修改前后都是 1295 passed、2 skipped，失败列表都为空 |
| 新增用例 | `tests/unit_tests/core/session/vcs/test_rewind_empty_state.py`：`set_state({})` 写回为空、缺失部分保持原状；回到状态为空和不为空的历史点 | 3 个通过 |
| 用例有效性 | 临时把 `set_state` 改回原来的判断，跑新增用例和 `test_paused.py` | 5 个失败（新增 2 个、`test_paused.py` 3 个），改回后全部通过 |
| 整体回归 | `tests/unit_tests/core/{session,single_agent,workflow,agent,multi_agent,runner}`、`tests/unit_tests/harness`、`tests/unit_tests/harness_providers`、`tests/unit_tests/cli`、`tests/cli/integration/test_recoverable_cli.py`；修复后跑一次，再用临时插件把 `set_state` 改回原来的判断跑一次，比对两次的失败列表 | 修复后 10059 passed、11 failed、7 个文件收集失败；改回后多出 5 个失败，正是上一行的 5 个，其余失败两次完全相同，与本修复无关。7 个收集失败和部分失败报的是本机缺少依赖（`opentelemetry.exporter`、`opentelemetry.proto`、`openai_codex`、`openjiuwen.rsi`），另有几个是 codex、自动 harness 等用例自身的断言失败，没有逐个查 |

没有验证的：Redis checkpointer（`tests/unit_tests/extensions/checkpointer` 需要本地 Redis，本机没有）；
上面两次都失败或收集失败的用例。
重验条件：`set_state` 的调用方新增"传入 `{}` 表示不写回"的用法，或 checkpointer 改为在已有状态的会话上恢复。

## 防错模式与资料同步

"`None` 表示没有，`{}` 表示空"：写回或合并状态时，不要用真假判断代替 `is None`，否则空集合会被当成"没有"。
已同步：S_01 第 1 节写明 `set_state` 的语义；S_20 不变量 6 与"与其它 spec 的关系"改为依赖 core 的写回；
F_20261113_1706_recoverable-harness 删去对应遗留并加修订记录。

## 遗留与后续

`InMemoryCommitState.set_updates` 有同样的 `if updates:` 判断。它写回的是还没提交的更新，回退不经过它，这次没有改。
