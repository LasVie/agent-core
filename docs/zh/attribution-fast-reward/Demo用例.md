# 交付 Demo 用例：个人助手 + 贪吃蛇

模型统一用 **qwen3.7-plus**。两个任务各有 Agent Team 和 Single Agent 两种跑法。

| 用例 | 模式 | 任务原文 | 已有会话（同事机器 `.runtime`） |
|---|---|---|---|
| A1 个人助手 | Agent Team | `reward-engine-runs/simple-tasks/personal/task.md` | `web_1a152586c02_c1c9b47c964f`（三个窗口的部分选择）<br>`web_1a1522ec519_20a9ac4fee00`（窗口不含用户请求）<br>`web_1a15253971f_07545a1ac50d`（全部轨迹） |
| A2 个人助手 | Single Agent | 同上 | 暂无，需新跑一次 |
| B1 贪吃蛇 | Agent Team | `reward-engine-runs/simple-tasks/snake/task.md` | `web_1a15258ef3d_d285a45532e2`（全部轨迹） |
| B2 贪吃蛇 | Single Agent | 同上 | `web_1a1527eec02_6f9529e78210`（全部轨迹） |

## 改动前的结果（已存的诊断记录）

| 会话 | 改动前 | 原因 |
|---|---|---|
| A1 `web_1a1522ec519…` | 95 分，归因 0 条 | 窗口晚于用户请求，Task 被解析成写入成员的子任务「把定稿写入 schedule.md 并回读校验」 |
| A1 `web_1a152586c02…` | 94 分，归因 0 条 | 三个窗口都晚于用户请求 41 秒以上，Task 只能从成员工作里反推；Reward 全 perfect |
| A1 `web_1a15253971f…` | 95 分，归因 0 条 | Task 正确，但 Reward 只核对「有没有」，5 项全 perfect；外婆 16:55 到（她 5 点后才有空）没被发现 |
| B1 `web_1a15258ef3d…` | 95 分，归因 0 条 | Reward 全 perfect，过程信号被替换掉。<br>对照：同一会话更早一次没走 Reward 的诊断，靠过程信号归因出 3 条根因（方向队列初版缺陷、写文件前未读、读后被改），还给了产物改进 |
| B2 `web_1a1527eec02…` | 诊断失败：「诊断后没有可归因的信号」 | Reward 全 perfect、无规则信号，流程中止，TTSE 也没运行 |

## 离线核对 Task 来源（不调模型，几秒钟）

在同事的任务根目录（有 `.runtime` 的那一层），切到新分支后运行：

```bash
.venv/bin/python ../jiuwenswarm-attribution-yiran/attr-fix-handoff/demo/replay_task_source.py web_1a152586c02_c1c9b47c964f
```

它读取该会话最近一次诊断的选择范围，按 JiuwenSwarm 的方式切片，然后打印 Task 现在依据的用户消息。在这边用同事机器上的数据跑过：

- `web_1a152586c02…`：4 条用户消息（原始需求、问路径、「没有路径我怎么知道去哪里看文件」、「用 agent team 重新做一份」）；
- `web_1a1522ec519…`：原始需求 1 条；
- 两个贪吃蛇会话：原始需求 1 条。

## 端到端复跑（前端）

1. 同事的 agent-core 切到 `feature/attr-task-source-fast-reward`，运行 `./start-local.sh` 重启，前端刷新。
2. 打开上表的会话 → 轨迹分析 → 诊断，选 qwen3.7-plus，范围与改动前相同（部分选择的也照原样框）。
3. 每个用例看下面几点：

| 看什么 | A1 个人助手（Team） | B1 贪吃蛇（Team） | B2 贪吃蛇（Single） |
|---|---|---|---|
| 任务解析 | 任务描述是用户原需求（含 5 点后、10 分钟、三件事），不是子任务 | 用户原需求 | 用户原需求 |
| 诊断分析 | 出现 `reward · … · Fast Reward NN`；有非 perfect 的「[要求]」或「[挑战]」项 | 同左；另有保留下来的过程信号（失败的工具调用、用户追加的消息） | 同左 |
| 归因分析 | 归因 ≥1 条，指到具体节点；有产物改进意见 | 归因 ≥1 条；有产物改进意见 | 诊断不再中止 |
| Skill 建议 | 若问题出在 leader 派发或成员做法，可能出现（归因 agent 自行判断，不保证） | 同左 | — |
| TTSE | — | — | 走完归因后，JiuwenSwarm 追加 TTSE 经验根因（若检测到） |

这些是预期，不是已经跑出的结果。这边没有模型密钥，端到端只能在同事机器上确认。

## 补充用例 A3：单 Agent 个人助手（泳镜问题）

会话 `web_1a1527f268f_95fc29ee91fd`。用前三个提交跑出的结果是：总分 80，3 条归因（换衣过渡、停车入场、回复多余说明），1 条产物改进。但计划把泳镜放在 10 点游泳课之后才买，这条被判成了满足。提交四修了这一点。

有两种验证方式：
- **前端**：重跑这个会话的诊断，看「诊断分析」里有没有关于泳镜买得太晚的不满足项，以及归因里有没有对应的一条。
- **命令行**（不经过前端，几分钟）：`attr-fix-handoff/demo/check_goggles.sh`。它用这个分支的 Fast Reward 重新评那次的 `schedule.md` 和回复，最后列出所有涉及泳镜的判定项和结论。默认用 deepseek-v4-flash；也可以传一个 Fast Reward 配置文件，换成 qwen3.7-plus。

提交五之后，「诊断分析」里会一行行显示 Fast Reward 的进度，形如：`▓▓▓▓▓░░░░░ 48% · 取证：第 3 项检查完成（browser） · 已用 1分52秒`。

## 补充用例 A4：只框中间几段（开发者看过程）

这个会话已打包成固定 demo：`demo-data/web_1a152586c02_saturday.tar.gz`，导入方法和建议的框选见 `demo-data/README.md`。

> 提交十之后的规则：框选里有 leader 在「最后交付的那一轮」里干活才评交付物，否则只看过程。下面表格里按提交八、九写的预期（只评框到的那一份、「本次评估范围」、「框选之外的后续进展」）已不适用，以 `demo-data/README.md` 为准。

会话 `web_1a152586c02_c1c9b47c964f`，照 `dg_b6560655…` 的框法选三个不重叠的窗口：leader 12:09–12:45、schedule-planner 12:44:42–12:45:08、message-writer 15:57:02–15:57:22。提交八之后应看到：

| 看什么 | 预期 |
|---|---|
| 任务解析 | 文件产物只剩 `wechat-message-v2.md`（它的最后一次写入在 message-writer 窗口内）；成员发给 leader 的消息不再算消息产物 |
| 诊断分析 | Fast Reward 只评这一份；任务后面附「本次评估范围」，`schedule.md`、`wechat-message.md` 标为不在范围内；过程信号只剩判为反馈的用户追加消息（预期是「没有路径我怎么知道去哪里看文件」，不含「用 agent team 重新做一份」） |
| 归因分析 | 只针对框到的内容；产物改进只关于评到的那一份 |

再试一种：只框 leader 到 12:45（第一轮完成为止）。「任务解析」的输入末尾会多一段「框选之外的后续进展」，模型应据此判断 `schedule.md` 和第一轮的完成回复已被后来的 v2 取代，不再报为交付物。

再试一种：三个窗口都不含任何交付文件的最后一次写入（例如只框 leader 12:09–12:30）。应看到不跑 Fast Reward、没有产物改进，只有过程信号的归因；连过程信号也没有时，诊断里是一条「未发现过程问题」，归因正常结束、0 条根因。

## 新跑一遍（可选）

用 qwen3.7-plus 新开会话，分别以 Agent Team 和 Single Agent 模式，把上面两份 `task.md` 的内容原样发给 agent，做完后用「全部轨迹」和「只框后半段」各诊断一次。两种框法得到的任务描述应该一致。

`simple-tasks/README.md` 列了每个任务事先埋的点，用来核对 Reward 抓到了哪些。别发给 agent。

## 回到改动前

`git checkout attr-new`；或保留新分支、启动前设 `ATTRIBUTION_REWARD=agent`（只关掉 Fast Reward，Task 来源和过程信号的修正仍在）。
