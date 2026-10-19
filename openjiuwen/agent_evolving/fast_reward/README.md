# Fast Reward

`openjiuwen.agent_evolving.fast_reward` 给 Agent 交付的作品打分，输出**一份标准 JSON**（元信息、总分和总评、各一级维度、全部二级子项），给用户看的 Markdown 回复也由这份 JSON 渲染。

它只管评分：**不读轨迹，不给修改建议**。归因到步骤、修改建议由独立的 `fast_attribution` 包完成，它读本包的 JSON（加上轨迹）。两个包互不 import。


## 接口说明（给合代码的同事）

### 输入

```python
result = await reward(task, artifacts, config, txt_response="", user_note="", process_signal="", rubric_path=None,
                      previous_result=None)
```

| 参数 | 类型 | 必需 | 含义 |
|---|---|---|---|
| `task` | str | 是 | 任务描述（用户的原话）。只用它写评分标准 |
| `artifacts` | list[str] | 是 | 交付物的路径列表，文件或文件夹都行。reward 自己把它们复制到一个临时目录里评，评完删掉，不会改原文件。答案只在回复里、没有文件时传 `[]` |
| `config` | dict | 是 | 模型、时限等，**每一项见下文"配置"一节（有几处容易踩坑）**；`load_config()` 读默认配置 |
| `txt_response` | str | 否 | Agent 的最终文字回复。答案在回复里时评它 |
| `user_note` | str | 否 | 用户对作品的意见 |
| `process_signal` | str | 否 | 过程信号：Agent 工作时遇到的环境报错，以及多轮对话里用户的反馈 |
| `rubric_path` | str / Path | 否 | 默认不传：每次调用按任务重新出题。传了：第一次写入这个文件，之后沿用同一套标准（分数才可比）。要换一套，换个路径，或先用 `archive_rubric(path)` 把旧文件存档 |
| `previous_result` | dict | 否 | 同一任务上一次的结果 JSON，只在重新出题时用：新标准保留它的基础项，已经做到的主动挑战项不再出，新的主动挑战项往更高处出，标准跟着作品一起往上走。沿用已保存的标准时不起作用 |

`user_note` 和 `process_signal` 都会写进评分标准：一处具体的意见推广成整部作品都要满足的子项，放进它涉及的那个维度，按基础项算。报错只在作品本身还可能出现时才写成子项（"运行时不出现这类错误"）。结果里分别标 `from_user_note` 和 `from_process_signal`。

不接收轨迹。任务、最终回复、用户意见和过程信号从会话里取，是调用方的事（JiuwenSwarm 用 `fast_attribution.locate_task_output`）。

### 输出

一个 dict（`schema_version: "fast_reward/3"`），字段见下文"输出：标准 JSON"：
- `meta`：评了什么、用的模型和工具、提醒；
- `total`：`score`（0–100）和 `summary`（总评文本）；
- `categories[]`：一级维度，含 `category_id`、`category_name`、`rubric`、`comment`、`score`、`issues`；
- `sub_category_items[]`：全部二级子项，含 `sub_category_id`、`category_id`、`category_name`、`rubric`、`comment`、`score`、`status`、`type`、`gain`、`from_user_note`、`from_process_signal`。

`render(result)` 把它渲染成 Markdown。一份结果只描述这一次评测，不带历史分数。

### 功能（从输入到输出做了什么）

1. **出题**：只看任务（和匹配到的评测知识），写出几个一级维度，每个维度有基础项和主动挑战项；有 `user_note` 或 `process_signal` 时，把它们推广成子项放进对应维度。
2. **取证**：在交付物的临时副本里读文件、跑 Python、用离线浏览器实际操作网页并截图。
3. **判定**：独立的判定调用逐条判 met / partial / unmet / unknown，写理由和维度评价；画面维度只看截图判。
4. **计分**：代码按固定规则算子项、维度、总分；模型不报分。
5. **总评**：根据各维度分数和评价写两三句总评。

### 实现逻辑

**一次 reward 是一个 DeepAgent 加一段代码流程。** 只有取证是真正的 ReAct，其余都是代码按顺序发出的普通模型调用。

```
reward() 开始
  ├─ 代码：建临时目录、写评分标准、放入用户意见和过程信号（模型调用，JSON 输出）
  ├─ DeepAgent（agent-core harness，本次创建）：取证
  │     工具只有两个：run_checks（一批检查并发执行）和 read_evidence（按需读已存的结果）
  │     每轮看上一轮的短摘要，再决定补查什么；证据够了或到轮数 / 时间上限就停
  │     完整结果保存在临时目录；每批最后两张截图进入 Agent 上下文，每个维度最新的 16 张截图交给判定
  │  （用完销毁）
  ├─ 代码：各维度判定（并行的模型调用），截图给看图判定
  ├─ 代码：计分
  └─ 代码：总评 → 组装 JSON → 删掉临时目录
```

- **并行**：一轮里的多个检查在 `run_checks` 内部并发执行，模型同时发出的多个工具调用也由 agent-core 并发执行；判定按维度并行。
- **上下文**：Agent 看到任务、各维度子项、交付物的文件清单（每个文件几行），以及文档和数据文件的摘录。**作品是网页（游戏、页面、应用）时，它的源码（html、js、css）谁都不看**：不给摘录，`read` 也读不了，判定也看不到；像用户一样用它来判，探针要用的变量名用探针自己找（`Object.keys(window)`、页面上的按钮）。交付物本身是代码（脚本、库，或写在回复里的代码）时才细读代码。读文件只返回"第 a–b 行 / 共 N 行"，不提示"从哪一行接着读"。浏览器检查只回给 Agent 每一步成没成功、探针读数和报错（最多 1500 字符），不回显它自己发出的动作和脚本；还有每批最后两张截图（模型支持图片时）。需要更多细节时用 `read_evidence` 读已存的完整结果。
- **防止原地打转**：取证 Agent 有时会一遍遍提交同一个检查，或者发空请求，拿不到任何新证据，白白耗时间。代码按"本轮有没有跑出新检查"来计数：
  - 提交的检查以前跑过，一模一样的不再跑，直接回复"已经作为 c3 跑过，用它的结果"；
  - 一次 `run_checks` 里只要有一个新检查，计数清零；一个新检查都没有（全是重复的，或者列表是空的、格式不对），计数加 1；
  - 计数到 2：提醒 Agent "最近没有新证据，该停了"；计数到 4：代码直接结束取证，用已有证据进入判定。
  - 按需读已存结果（`read_evidence`）是正常工作，不计数，读多少次都不会被打断。
- **出错分两种**：会决定分数的环节（出题、取证、各维度判定）失败就抛 `EvidenceError`，写明卡在哪一步和原因，不给没有依据的分数；取证到轮数或时间上限不算出错，用已有证据继续判定。不决定分数的环节（挑选评测知识、把用户意见和过程信号写进标准、写总评、看图试探、默认截图）失败时，这一步跳过，结果的 `warnings` 和回复开头写明哪一步没完成；总评失败时由代码按分数写一句。弱模型返回的 JSON 按宽松规则读：没有文字的子项丢掉，不认识的级别按基础项算，重复的维度编号自动加后缀。已保存的评分标准损坏会报错，免得悄悄换一套标准。模型漏写检查的 `tool` 时，按字段补上（有 `page` / `actions` 是浏览器，`code` 是 Python，`path` 是读文件）。

## 快速开始

依赖：`openai`、`PyYAML` 是 agent-core 的基础依赖，装了 agent-core 就有。评网页（游戏、绘本页面等）还要浏览器，它是可选依赖，需要另装：

```bash
pip install 'openjiuwen[reward-browser]'   # playwright + Pillow
playwright install chromium
```

作品里有网页却没装 playwright 时直接报错（不会退回只看源码）。只装了 playwright 没装 Chromium 时，浏览器检查会逐个失败，取证 Agent 拿不到运行证据。

```bash
python -m openjiuwen.agent_evolving.fast_reward --task task.md --artifact /abs/index.html --out reward_result.json
python -m openjiuwen.agent_evolving.fast_reward --task task.md --artifact report.md table.xlsx \
    --txt-response reply.md --user-note "第 2 页多一只手" --process-signal errors.txt \
    --rubric rubric.json --config config.json --out reward_result.json
```

```python
import asyncio
from openjiuwen.agent_evolving.fast_reward import load_config, reward, render

config = load_config()                                       # 或直接给一个 dict
result = asyncio.run(reward(task_text, ["report.md", "table.xlsx"], config, txt_response=reply_text))
print(render(result))                                         # Markdown 回复
```

## 输入

| 输入 | 命令行参数 | 必需 | 说明 |
|---|---|---|---|
| 任务描述 | `--task` | 是 | 文本或文件。写评分标准时只看它 |
| 交付物 | `--artifact` | 否 | 一个或多个文件、目录。没有文件时（答案在回复里）省略，按最终回复评 |
| 最终回复 | `--txt-response` | 否 | Agent 交付时给用户的文字回复（文本或文件）。答案在回复里的任务就评它 |
| 用户意见 | `--user-note` | 否 | 文本或文件。写进评分标准（见上文） |
| 过程信号 | `--process-signal` | 否 | 环境报错和多轮对话里的用户反馈，文本或文件。写进评分标准（见上文） |
| 打分标准文件 | `--rubric` | 否 | 保存评分标准（含用户意见、过程信号加进去的子项），之后几轮沿用同一套；加 `--new-rubric` 先把旧文件存档再重新出题。只存标准，不存分数 |
| 上一次的结果 | `--previous-result` | 否 | 同一任务上一次的 `reward_result.json`，重新出题时据此抬高主动挑战项（见上文 `previous_result`） |

## 输出：标准 JSON

```json
{
  "schema_version": "fast_reward/3",
  "meta": {"evaluated_at": "2026-10-16T08:00:00+00:00", "task": "…", "deliverable": ["index.html"],
           "final_reply": true, "inputs": [], "user_note": "…", "process_signal": "…",
           "models": {"producer": "…", "judge": "…", "art_judge": "…"}, "tools": {"available": [], "used": []},
           "guides": ["game"], "scoring": "计分规则一句话", "warnings": []},
  "total": {"score": 72, "summary": "总评两三句"},
  "categories": [{"category_id": "visual", "category_name": "画面质量", "rubric": "这个维度怎样算好",
                  "comment": "该维度评价", "score": 60, "issues": ["头和身体分离"]}],
  "sub_category_items": [{"sub_category_id": "visual#2", "category_id": "visual", "category_name": "画面质量",
                          "rubric": "子项要求", "comment": "判定理由", "score": 50, "status": "partial",
                          "type": "proactive_challenge", "gain": 6.0, "from_user_note": false,
                          "from_process_signal": false}]
}
```

| 字段 | 含义 |
|---|---|
| `total.score` / `total.summary` | 总分（各维度平均，0–100）和总评（模型根据各维度分数和评价写，不含建议） |
| `categories[]` | 一级维度：`category_id`、`category_name`（评什么，2–6 个词）、`rubric`（这个维度怎样算好）、`comment`（该维度评价，判定模型写；没写时用子项理由代替）、`score`、`issues`（只有画面维度有：看图判定发现的严重画面问题，文字列表；它们会把相关的主动挑战项压到最多"部分达成"） |
| `sub_category_items[]` | 全部二级子项：`sub_category_id`（`维度id#序号`，归因模块用它指认子项）、所属 `category_id` / `category_name`、`rubric`（子项要求）、`comment`（判定理由）、`status` 和 `score`（见下表）、`type`（`"floor"` 基础项或 `"proactive_challenge"` 主动挑战项，见"计分"一节；只在 JSON 里，Markdown 回复不分开显示）、`gain`（这一项做到后总分能涨多少，供归因排序）、`from_user_note` / `from_process_signal`（这一项由用户意见 / 过程信号生成） |

`status`（子项达成情况）：

| status | 含义 | score |
|---|---|---|
| `met` | 证据显示完全做到 | 100 |
| `partial` | 做到一部分，或有严重画面问题牵涉它 | 50 |
| `unmet` | 证据显示没做到 | 0 |
| `unknown` | 没有证据，判断不了（不是没做到）。基础项无证据时不计入；主动挑战项无证据记 0。整个维度没有一条子项有证据时（什么都没看到），维度 score 为 null，不计入总分 | `null` |
| `meta` | 何时评的、评了什么（交付物、是否有最终回复、用户意见、过程信号）、用的模型和工具、评测知识、计分规则、提醒 |

`score` 由 `status` 按上表换算，是给人看的百分制分数；程序要区分"没做到"和"没查到"时看 `status`。`null` 表示没有证据，不是 0 分。一份结果只描述这一次评测，不带上一轮分数或历史：要比较几轮，调用方保存每次的结果自己比。JSON 用 `type` 区分基础项与主动挑战项，Markdown 按维度合并显示。`--evidence`（代码里配置 `"include_evidence": true`）会多一个 `evidence` 字段，放每个检查的工具、代码、输出。

**Markdown 回复**（`render(result)`）：总分 → 总评 → 提醒 → 输入 → 各维度（分数、规则、评价、没达成的子项和理由、画面问题）→ 一句计分规则。

### 判定规则：怎样避免误判

判定模型给每个子项一个 `status`。下面四条规则决定它怎么给，目的都是让分数只反映作品本身。

**1. 什么算证据**

| 子项类型 | 例子 | 达成（`met`）需要 | 只有源码或自述时 |
|---|---|---|---|
| 行为 | 得分、碰撞、胜负、翻页、表单保存 | 检查实际运行到那个场景并观察到结果（探针读数、状态变化、截图）；调用作品自己的函数设置场景也算 | 最多 `partial`，理由写"源码里有，运行中没观察到"；网页作品不看源码，运行中没观察到就是 `unknown` |
| 事实、数字 | 统计值、计数、引用 | 检查实际重算或查对过（例如用交付物里的原始数据重算） | 交付物自己说对不算；没有可核对的数据判 `unknown` |
| 画面 | 造型、配色、构图 | 截图上看得到 | "代码里画了渐变"只说明意图，不算 |
| 文件本身 | 结构、常量、文字 | 读源码或文件即可 | — |

**2. 检查失败不算作品失败**

以下情况判 `unknown`（没查到），不判 `unmet`（没做到）：
- 页面没加载出来（HTTP 错误、白屏）；
- 检查自己的操作不对（例如盲按把游戏一开局就玩死）；
- 截图没拍到那个状态。没拍到不能推出"作品没有这个状态"。

评测环境的限制（只有一个浏览器、没有真机、运行时间短）也不算作品缺陷，标准里不写需要这些条件才能判的子项。

**3. 同一个问题只扣一次**

- 每个维度只判自己的子项，评价也只谈本维度。画面问题只在画面维度扣分，不拉低玩法、内容、数据这些维度。
- 一条子项只查一件事。

**4. 画面维度单独判**

- 游戏、网页、图表、海报这类要给人看的作品，都有一个画面维度。出题模型漏写时，代码自动补上（`pipeline.ensure_visual`）。id 或名字明确指外观的维度（视觉、画面、美术、外观、造型、画风、观感、美观、visual、looks、art……）也按画面维度判；只在目标描述中提到外观、或名字同时包含交互、流程、手感、数据时，不自动转成画面维度。"界面""图表"这类词也不算。反过来，出题模型把一个维度标成画面维度，而任务不做给人看的东西、维度名也不讲外观时（例如纯文字回答的"表达清晰"），代码去掉这个标记，按普通维度判。画面维度只放"看起来怎样"的子项；找不到意见对应的维度时，代码只回退到非画面维度，没有兼容维度则明确报错。
- 画面由一次只看截图、不看源码的调用来判（`ART_JUDGE_PROMPT`）。它先列出用户一眼能看到的问题，再判子项。有严重问题时，相关的主动挑战项最多算 `partial`。严重问题写进该维度的 `issues`。
- 评判尺度是"同题材、同画风的已发布作品"，不设固定的尺寸或比例。
- 截图从哪来：取证 Agent 按作品实际导航逐页取证。取证没拍时，代码按默认流程补拍（`pipeline.default_screenshots`）：打开页面、尝试开始操作、取特写，再尝试翻页和滚屏；这些默认操作不保证覆盖所有页面，未看到的内容仍应判为无证据。作品只有图片时，直接用交付的图片，按页码排序取前 8 张（`checks.image_views`）。
- 可以单独给画面判定配一个看图更强的模型（`art_judge`，见"配置"一节）。




**用户意见和过程信号写进评分标准**：有 `user_note` 或 `process_signal` 时，另用一次调用（`FEEDBACK_PROMPT`）看着现有维度，把它们推广成对整部作品都适用的子项（例如某一页文字难读 → "每页文字都清晰可读"），放进它涉及的那个维度，不凭空补数量或门槛；不新增维度，找不到合适的就放进最接近的非画面维度（通常是"质量"维度）。环境报错只在作品本身还可能出现时才写成子项。这些子项是基础项，结果里标 `from_user_note` 或 `from_process_signal`，模型跳过语义重复，代码再排除逐字重复。它们存进打分标准文件，之后每轮都查，所以标准会越来越严（`pipeline.feedback_claims` / `merge_feedback`）。每次最多 6 条。

## 计分（代码算，百分制）

每个维度的子项分两类，出题模型写标准时标好：
- **基础项**（`level: "floor"`）：任务明确要求的。做不到就是不合格。用户意见、过程信号生成的子项也算基础项。
- **主动挑战项**（proactive challenge，`level: "challenge"`）：任务没要求、同类作品里做得最好的会主动做到的。必须有超出基础项的独立提升，不能把同一要求换个说法再计分，也不能替任务规定未要求的实现方式。拿高分靠它。

一句话：**基础项全做到、主动挑战项做到一半约 75 分；全做到才 100；基础项有一条没做到，这个维度最多 25 分。**

| 规则 | 说明 |
|---|---|
| 子项取值 | met = 1，partial = 0.5，unmet = 0 |
| 维度分 | 基础项平均 × 50 + 主动挑战项平均 × 50。基础项全做到、主动挑战项做到一半，约 75；主动挑战项全部达成才是 100。只有一类子项的维度按那一类平均 × 100 |
| 基础项没做到 | 任一基础项 unmet，该维度最高 25 |
| 没有证据 | 基础项 unknown 不计入；主动挑战项 unknown 记 0，不管判定模型能不能看图。整个维度所有子项都 unknown 时，维度为 null，不计入总分；看图判定看不了图时，画面维度也是 null，回复开头会提示 |
| 总分 | 有分数的维度平均（等权）；score 为 null 的维度不计入 |
| 子项 `gain` | 假如这一项达成，按上面的规则重算，总分能涨多少 |

## 评测知识（metric 文档）

开始评测前，先扫描一遍领域知识文档，挑出和这次任务相关的，交给出题和取证 Agent 使用。只作参考，不是固定模板：
- **扫描位置**：包里的 `metrics/`（自带 `picture_book.md` 绘本、`game.md` 游戏、`personal_assistant.md` 个人助手三份评测知识）；你自己的 `~/.fast_reward/metrics/`（或 `$FAST_REWARD_HOME/metrics/`），放进去就生效；配置 `metric_guides` 列出的文件或文件夹。
- **怎么挑**：文档第一行写 `keywords: 绘本, picture book, …` 的，任务里出现任一关键词就选用（自带的三份都有关键词，不额外调用模型）；没写关键词的，用一次轻量的模型调用，只看任务原文和每份文档的标题加开头，挑出相关的（`guides.for_task`）。
- **怎么用**：选中的文档拼在任务后面交给出题模型，按任务挑适用的维度，可以写单页、相邻两页或整本的子项，不替任务编死内容；取证 Agent 也能看到它（比如逐页翻看、对比各页主角）。判定理由会写明涉及哪几页。回复的"输入"一行列出用了哪些文档，JSON 里是 `guides`。
- **评测知识只写通用原则，像宪法一样**：写"好是什么"（例如"操作即时、结果公平""图文互补"），不写具体规定（尺寸、占画面多少、数量门槛、某种画风或情节）。具体、可检查的子项由出题模型按任务现写；把品味写成固定数字，会让判定拿错误的尺子量所有作品。自带的两份（游戏、绘本）都按这个原则写。
- 新增一类任务的评测知识：写一份 md，第一行写关键词，放进上面任一位置即可。写的时候注意：
  - 选中的文档合计最多取前 8000 个字符，一份控制在 7000 字符以内；同一类任务只放一份，两份都命中会重复并被截断。
  - 证据只能是读文件、跑脚本、浏览器操作后的截图和状态探针；子项按达成 / 部分达成 / 未达成判定，要能在一次不到一分钟的浏览器会话里验证。录屏、音频、多次会话、0–4 分锚点、注入负例校验评测器、人工盲测这类 Reward Engine 的做法写进去，只会变成"无证据"的子项。
  - 不放具体规定和示例数字；需要举例时只举原则层面的，否则会被套到别的任务上。

## 取证工具

取证 Agent 用 `run_checks` 一次提交一批检查，每个检查是下表里的一种，并注明它为哪些维度取证（一个检查可以同时服务几个维度，比如一次打开页面的截图同时给"玩法"和"画面"用）。

| 工具 | 用途 | 默认 | 边界 |
|---|---|---|---|
| `read` | 引用文件内容，或按正则搜索关键词 | 开 | 路径不能超出临时目录。作品是网页（游戏、页面、应用）时，它的源码（.html、.js、.css）不读，取证和判定都看不到：像用户一样用它来判；探针要用的变量名用探针自己找（`Object.keys(window)`、页面上的按钮）。交付物本身是代码（脚本、库，或写在回复里的代码）时才细读代码。读文件夹时返回其中的文件列表 |
| `python` | 写小脚本测量：重算汇总、核对行数、解析表格、校验格式 | 开 | 只用标准库，限时 60 秒；有防护，见下文 |
| `browser` | 离线浏览器里点击、按键、等待、读运行时数值、截图 | 有网页时开（没装 playwright 直接报错） | 默认从本地虚拟地址加载；检查写 `"via": "file"` 时用 `file://` 直接打开，和用户双击文件一样（子项涉及"直接打开"时用）。动作 `drag`（按住拖动，可转动 3D 视角、拖动棋子）和 `wheel`（在元素上滚滚轮，可缩放 3D 视角）用来测鼠标交互；动作 `closeup` 截取某个元素（或它的一部分）的 2 倍清晰特写，用来看清主角、图表等细节。文件名可以是中文。页面本身加载失败时这次检查记为失败，不作为作品证据。`localStorage` 可用。外网请求一律拦截，只有一种例外：jsdelivr、unpkg、cdnjs 上写明确切版本号的库文件（如 `three@0.160.0`）。它每次内容都一样，由评测代码下载一次、缓存在 `~/.fast_reward/cdn/` 后交给页面；没写版本号（如 `@latest`）的照样拦截。累计等待最多 60 秒 |

- **检查只测量，不下结论**：输出的是事实和数值，判定由另一次模型调用完成。
- **检查本身出错**：Python 检查报 Traceback、浏览器起不来时，报错原样交回取证 Agent，它在下一轮自己改写这个检查。
- **没有 shell 工具**：检查不能直接运行命令。
- **`python` 检查的防护**：脚本在作品的临时副本里运行（脚本文件本身放在副本之外，所以列目录时只看到作品），60 秒超时。运行前先装一个 Python 审计钩子（`checks.GUARD`），装上后在这个进程里拆不掉，它拒绝：
  - 启动其他程序（`subprocess`、`os.system`、`exec`/`spawn`/`fork`）；
  - 网络连接（`socket` 的连接、绑定、发送）；
  - 加载本地代码（`ctypes`）；
  - 写入、删除、改名作品临时副本（和脚本自己的临时文件夹）以外的文件；
  - 读取或列出这两处和 Python 安装目录以外的文件（比如读不到 `~/.ssh`、你的其他项目，也读不到其他临时文件）。

  被拒的操作在检查结果里显示为 `PermissionError`。它能挡住误操作和明显的越权，**但不是操作系统级的沙箱**：Python 审计钩子防不住刻意构造的绕过。在自己机器上评自己 Agent 的作品可以接受；评不可信的作品或上线时，应把 reward 放进容器或沙箱运行（agent-core 有 `core/sys_operation/sandbox`）。

## 配置

`config` 是一个 dict。命令行按这个顺序找配置文件（JSON）：`--config` 指定的文件 → `$FAST_REWARD_CONFIG` → `~/.fast_reward/config.json`（目录可用 `$FAST_REWARD_HOME` 改）→ 内置 `DEFAULT_CONFIG`（和 `config.example.json` 一样）。代码里调用时直接传 dict，可以用 `load_config(path)` 读文件并解析模型。

### 一份完整的例子

我们实验里三个角色（出题、判定、取证）都用 deepseek-v4-flash，开思考、低强度（和 JiuwenSwarm 的 `/reward` 一致）。`art_judge`、`evidence_model` 不写就分别跟随 `judge`、`producer`；想让画面单独由一个看图更强的模型判，再加 `art_judge`。

```json
{
  "producer": {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1", "api_key": "env:DEEPSEEK_API_KEY",
               "timeout": 360, "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}},
  "judge": {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1", "api_key": "env:DEEPSEEK_API_KEY",
            "timeout": 360, "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}},
  "evidence_rounds": 12,
  "evidence_seconds": 300,
  "criteria_budget": 3,
  "metric_guides": ["/path/to/my_guides/"]
}
```

### 每一项

| 键 | 类型 | 默认 | 作用 | 容易踩的坑 |
|---|---|---|---|---|
| `producer` | 模型配置 | `{"model": "session", "timeout": 360}` | 写评分标准、放入用户意见和过程信号 | 必填。`"session"` 只在 JiuwenSwarm 环境里能用，见下文 |
| `judge` | 模型配置 | 同 `producer` | 逐条判定子项、写维度评价和总评 | 和做作品的模型是同一个时，判定可能偏宽松；要稳定的奖励信号就固定成独立模型 |
| `art_judge` | 模型配置 | 不配就用 `judge` | 只看截图判画面维度 | 必须能看图，否则画面子项全是"无证据" |
| `evidence_model` | 模型配置，或 agent-core 的 `ModelPoolEntry` | 同 `producer` | 取证 DeepAgent 用的模型 | **必须支持工具调用（function calling）**，不支持就会报 `EvidenceError: ran no check`。能看图时 Agent 会看到截图 |
| `evidence_rounds` | int | 12 | 取证 Agent 最多几轮模型调用 | 只是防死循环的保险；到上限会停下继续判定，不报错 |
| `evidence_seconds` | int | 300 | 取证最多多少秒 | 设了 `deadline_seconds` 时还会被它截短：取证必须给判定和总评留时间 |
| `deadline_seconds` | int | 不设（无总时限） | 整次运行的总时限，可选；出题、意见整理、判定与总评共享该截止时间 | 调用方外层有超时时才需要设，要比外层小。JiuwenSwarm 传 720，剩下的时间给归因 |
| `criteria_budget` | int | 3 | 出题时交付物维度的上限（另加一个"质量"维度和需要时的"画面"维度） | 调大会多花判定时间 |
| `include_evidence` | bool | false | 结果 JSON 是否带每个检查的完整输出。默认不用，只在排查判定时打开 | 打开后 JSON 会很大；命令行用 `--evidence` |
| `metric_guides` | 路径列表 | 无 | 额外的评测知识文件或文件夹 | 只写通用原则，见"评测知识"一节 |

### 模型配置怎么写

每个模型配置是一个 dict：

| 字段 | 必需 | 说明 |
|---|---|---|
| `model` | 是 | 模型名；写 `"session"` 表示跟随 JiuwenSwarm 当前会话的模型 |
| `base_url` | 是（`"session"` 时不用） | OpenAI 兼容接口地址 |
| `api_key` | 是（`"session"` 时不用） | **不要写明文**：写 `"env:变量名"`，运行时从环境变量读 |
| `timeout` | 否 | 单次调用的总配额（秒），含一次重试，默认 360 |
| `first_chunk_timeout` / `idle_timeout` | 否 | 流式返回多久没开始、中途多久没新数据就判为卡住，默认各 60 秒 |
| `extra_body` | 否 | 原样传给接口的额外参数，例如 DeepSeek 的 `{"thinking": {"type": "enabled"}, "reasoning_effort": "low"}`、Qwen 的 `{"enable_thinking": false}`。开思考会明显变慢 |
| `vision` | 否 | true / false：直接声明能不能看图，跳过看图试探 |
| `provider` | 否 | 取证 DeepAgent 的模型提供方，默认 `OpenAI`；普通 JSON 模型调用仍使用 OpenAI 兼容接口 |

- **`"session"`**：从 JiuwenSwarm 最近的会话记录读出模型名，再从 `~/.jiuwenswarm/config/config.yaml` 读地址和密钥；其中 `${变量名}`（可写 `${变量名:-默认值}`）先从环境变量取，再从 `~/.jiuwenswarm/config/.env` 取，都没有就报错。不在 JiuwenSwarm 环境里（没有会话记录或 config.yaml）会直接报错，要写完整配置。`{"model": "session", "timeout": 600}` 可以覆盖个别字段。
- **看图试探**：没写 `vision` 时，评网页或交付图片前，判定、看图判定、取证三个角色各自试探一次（同一个模型只试一次），结果按接口地址和模型名缓存在 `~/.fast_reward/vision.json`。换了模型能力（比如同名模型升级）要删掉这个缓存。答案不清楚或调用出错时再试一次；仍不清楚，本次不发截图、不缓存，回复开头提示，下次再试；配置里写 `"vision": true` 可跳过试探。看图判定看不了图时，画面维度整项不计分，不能用源码代判外观。
- **JiuwenSwarm 里**：`reward_engine.fast` 下的配置会合并进来；模型默认跟随对话的模型，`art_judge` 可以只写 `models.defaults` 里的模型名。

## 代码结构

| 文件 | 用途 |
|---|---|
| `__init__.py` | 对外接口：`reward`、`text_reward`、`write_criteria`、`render`、`load_config`、`archive_rubric`、`SCHEMA_VERSION` |
| `__main__.py` | 命令行：`pipeline.reward` → 写 JSON → 打印 Markdown |
| `evidence_agent.py` | 取证 DeepAgent：`gather()` 建一个 agent-core DeepAgent，挂 `run_checks` / `read_evidence` 两个工具，跑完销毁；`EvidenceRun` 存结果、按维度分证据；`EvidenceError` |
| `pipeline.py` | 主流程：`reward()`（建临时目录后调 `score_workspace()`：写标准 → 取证 Agent → 各维度判定 → 计分 → 总评 → 组装 JSON）、`text_reward()`、`write_criteria()`、`feedback_claims()` / `merge_feedback()`、`ensure_visual()`、`evaluate_terminal()`、`cap_by_critique()`、`default_screenshots()`、`load_rubric()` / `save_rubric()` / `archive_rubric()`、`overall_summary()` |
| `result.py` | 标准 JSON：`build()`；Markdown：`render()`、`progress()`、`bar()` |
| `scoring.py` | `score()`（子项状态 → 维度分）、`total()`、`to_result()`、`claim_gains()` |
| `prompts.py` | 写标准、取证 Agent（`EVIDENCE_AGENT_PROMPT`）、判定、看图判定、推广用户意见、总评 |
| `checks.py` | 取证工具：`run_check()`、`run_experiment()`（离线浏览器）、`image_views()`、`available_tools()` |
| `guides.py` / `metrics/` | 评测知识的挑选和自带文档（`picture_book.md`、`game.md`、`personal_assistant.md`） |
| `workspace.py` | 临时目录：`make_workspace()`、`copy_work_folder()`、`linked_files()`、`workspace_files()` |
| `files.py` | 把文件读成文本 |
| `models.py` | 流式 JSON 客户端、`"session"` 模型解析、看图试探 |
| `session.py` | JiuwenSwarm 会话目录（找会话模型、写进度文件用；不读轨迹） |
| `progress.py` | 网页进度卡片读的进度文件 |
| `config.py` | 配置 |
| `rewardpack.py` | RewardPack 批量评测 |

测试：`tests/unit_tests/agent_evolving/fast_reward/`。模型调用用脚本替身，临时目录、Python 检查和离线浏览器真跑。


## 和 JiuwenSwarm 的配合

入口是 `/reward`，工具在 JiuwenSwarm 的 `agents/harness/common/tools/reward_tool.py`。`mode=fast` 时它先调本包评分，再把结果 JSON 交给 `fast_attribution` 归因、给建议；回复 = 本包的 Markdown + 归因模块的 Markdown。任务（用户原话）、最终回复、本轮写的文件，由工具用 `fast_attribution.locate_task_output` 从会话记录里取出；用户在 `/reward` 后面写的意见作为 `user_note` 传进来（`process_signal` 目前还没接）。本包自己不读会话记录。取证 Agent 用当前对话的模型（`evidence_model` 传会话的 `ModelPoolEntry`）。

- 评分标准存在会话目录的 `fast_reward_rubric.json`，之后每轮沿用（用户意见和过程信号加进去的子项留下）。`/reward 重新出题` 时，`reward_tool` 先调 `archive_rubric` 把旧标准改名存档为 `.1.json`、`.2.json`……，再把会话里最近一次的结果作为 `previous_result` 传进来，本包就会在旧标准之上重新出一套。配置 `reward_engine.fast.reuse_rubric: false` 可关闭沿用。
- `reward_engine.fast` 里可以写本包的配置项；`art_judge` 写模型列表里的模型名即可。
- 结果写到 `<会话>/reward_production/<run_id>/reward_result.json`（本包）和 `attribution.json`（归因）。
- 进度写到 `<会话>/fast_reward/<开始毫秒>.json`，网页进度卡片读取。

## 批量评测：RewardPack

```bash
python -m openjiuwen.agent_evolving.fast_reward.rewardpack --pack /path/to/rewardpack_top10 --out results/ --limit 4
```

- 每个候选用它当时能看到和产出的东西来评：任务原文、交付物文本、最终回复，以及任务的输入材料（WorkBuddy 的 workspace 输入、Claw-Eval 的 fixtures）。**不看标准答案，也不看官方评分器。**
- 同一任务同一轮的 A、B 两个候选共用一套标准，标准缓存在 `results/criteria/`。
- 结果逐条追加到 `results/results.jsonl`，汇总在 `results/summary.json`。
- `--rounds all` 跑全部 3 轮，`--concurrency` 控制并发数。

## 局限

- 取证受轮数和时间上限约束（默认 12 轮、300 秒；时间是主要约束，轮数只是保险），复杂交互里仍可能有子项判为无证据。浏览器检查每次从重新加载的页面开始，要到某个状态需要重复前面的操作。
- `python` 检查只能用标准库，不能运行其他程序、不能联网；需要登录或联网才能验证的东西不适用。
- 网页副本复制显式 HTML/CSS 资源引用，包含 CSS 的图片和嵌套样式；不会推断 JavaScript 动态拼出的文件路径。依赖这类路径的作品需检查副本是否完整，不能把副本缺资源当成原作品缺陷。资源超过复制预算会明确报错。
- 主动挑战项和 `quality` 带主观判断，同一个作品两次评测可能相差几分。比较作品时最好多跑几次。
