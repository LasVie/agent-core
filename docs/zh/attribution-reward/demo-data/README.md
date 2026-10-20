# Demo 会话：周六规划（多轮、Team、Skill 重做）

`web_1a152586c02_saturday.tar.gz` 是会话 `web_1a152586c02_c1c9b47c964f`（标题「agent-team-星期六规划」，qwen3.7-plus）的完整打包，约 1.8MB。这个会话轨迹不会再变，适合当固定的 demo case：

- 先是单 Agent 做了一版，用户追问 schedule.md 的路径、抱怨「没有路径我怎么知道去哪里看文件」；
- 再「用 agent team 重新做一份」：leader + schedule-planner + message-writer，交付 `schedule.md`、`wechat-message.md`；
- 最后「用 skill + agentteam 模式重做」：新建 Skill `schedule-planner`，交付 `schedule-v2.md`、`wechat-message-v2.md`。

框选范围的各种情况（全选、只框中间、框到部分交付物、交付物被换名重做）都能在这一个会话里演示。

## 包里有什么

| 内容 | 在 `.runtime` 下的位置 |
|---|---|
| 轨迹库（诊断读的就是它） | `.trace/sessions/a5/a515…sqlite3` |
| 会话：聊天记录、元数据、打包时已有的全部诊断结果 | `agent/sessions/web_1a152586c02_c1c9b47c964f/` |
| Team 工作区，含 4 份交付文件 | `.agent_teams/jiuwen_team_web_1a152586c02_c1c9b47c964f/` |
| 两个成员的目录 | `.agent_teams/jiuwen_team_members/jiuwen_team_web_1a152586c02_c1c9b47c964f#*` |
| 文件操作记录、会话运行状态 | `.agent_history/file_ops_*`、`config/runtime_state/web_1a152586c02_c1c9b47c964f.*` |
| 会话里新建的 Skill | `agent/workspace/skills/schedule-planner/` |

没有 API key、模型密钥之类的内容（打包后检查过）。

## 导入到自己的 JiuwenSwarm

只用 Python 标准库。先停掉后端，然后：

```bash
python3 import_demo.py web_1a152586c02_saturday.tar.gz --runtime <你的任务根目录>/.runtime
```

脚本会：

1. 把上面每一块拷到新 `.runtime` 下的同一位置；
2. 轨迹、聊天记录、诊断结果里写的是原机器的绝对路径（`/Users/lytton/work/reinforement_learning/jiuwenswarm-attribution-guangliang`），统一替换成新任务根目录（`--runtime` 的上一级），这样 Reward 能在新位置读到交付文件；轨迹库里的内容是 zlib 压缩的，会逐条解压、替换、再压缩；
3. 重建 Team 工作区里指向成员目录的两个软链接；
4. 会话已存在时不覆盖，要覆盖加 `--force`。

然后重启后端（`./start-local.sh`），打开 `/chat/web_1a152586c02_c1c9b47c964f`。

已验证：导入到另一个目录后，用 JiuwenSwarm 的 `_prepare_nodes` 读轨迹，全选和部分框选两种情况下，文件候选的路径都落在新目录下、文件都在。前端会话列表能否直接列出这个会话，打包时没有条件验证，第一次导入后请看一眼。

已知限制：
- 聊天记录里文件卡片的下载链接（`download_token`）带原机器的签名，换机器后点不开；不影响诊断和归因。
- 需要 agent-core 是 `feature/attr-task-source-fast-reward` 分支（见上一级的 `改动说明.md`）。

## 建议演示的几种框选（时间为北京时间）

| 框选 | 预期 |
|---|---|
| 全部轨迹 | 评最后交付的那一轮（15:56:09 起）：`schedule-v2.md`、`wechat-message-v2.md` 和 15:57:39 的最终回复；早期的 `schedule.md`、`wechat-message.md` 不评。过程信号里有用户的路径抱怨；交给 Reward 的任务带「当前有效的需求清单」，「给出绝对路径」标为用户纠正过 |
| 只框中间：leader 12:08–12:43、schedule-planner 12:43:40–12:44:53、message-writer 12:43:35–12:44:06 | 「消息 0 · 文件 0」：框里的 `schedule.md` 后来被 `schedule-v2.md` 取代；不跑 Reward，只看过程，不出产物改进 |
| 只框中间：leader 12:42:55–15:56:12、两个成员 12:43–12:45 | 同上；12:45 的第一轮团队完成回复已被 15:57 的重做取代，不算交付物 |
| 只框成员在最后一轮的部分：message-writer 15:57:00–15:57:22 | 只看过程：最后一轮里没有框到 leader，不评交付物 |
| 只框到最后一轮的开头：leader 12:43–15:56:18 | 只看过程：leader 在 15:56:15 只建了 Skill 目录，交付的那一步不在框选里 |
| 框到 leader 交付的那一步：leader 15:56:30–15:57:40（含 15:57:33 发文件、15:57:39 最终回复） | 评最终交付：v2 完成回复、`schedule-v2.md`、`wechat-message-v2.md`（以任务解析报的文件为准） |

打包时已有的诊断结果也在包里，导入后在前端的诊断记录里能直接看到，不必重跑。

## 重新打包

在会话所在的机器上：

```bash
python3 pack_demo.py --runtime <任务根目录>/.runtime --session web_1a152586c02_c1c9b47c964f \
    --task-root <轨迹里写的绝对任务根目录> --skill schedule-planner \
    --title "agent-team-星期六规划" --out web_1a152586c02_saturday.tar.gz
```
