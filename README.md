# Reddit 药物安全性与不良反应分析

本仓库提供一条面向 Reddit 自报文本、可自定义目标药物、使用 DeepSeek 普通 API 的药物警戒分析管线。项目借鉴了论文《Self-Reported Side Effects of Semaglutide and Tirzepatide in Online Communities》的研究问题，但当前实现已经独立重写，不包含原论文的四阶段方法脚本。

项目研究背景、当前状态、与原研究的差异、Study A 数据范围以及后续 HTML 汇报素材统一维护在 [`PROJECT_REPORT_SOURCE.md`](PROJECT_REPORT_SOURCE.md)。

本项目的结果用于发现 Reddit 用户关心的潜在不良反应信号，不能用于证明药物因果关系，也不能解读为临床发生率。

## 当前代码

| 文件 | 作用 |
|---|---|
| `download_reddit_data.py` | 下载 Reddit posts，并从药物相关 posts 定向下载评论 |
| `pharmacovigilance_pipeline.py` | 清理输入、调用模型 API、识别实际用药组合、抽取症状并生成统计和图表 |

当前已实现：

- 从 CSV、JSONL、NDJSON 或完整的 `targeted_comments` 目录读取 Reddit 文本。
- 自动合并召回 posts 与定向 comments，保留讨论串关系并为 comment 生成只用于消歧的上下文。
- 清理空文本、删除内容、缺失用户和重复记录，并将 Reddit 用户名转换为带盐 HMAC-SHA256 伪名。
- 通过 DeepSeek 普通 Chat Completions API 进行并发抽取。
- 使用根目录 `.env` 中的 `model_url`、`api_key` 和 `model_name`。
- 识别作者实际使用的目标药物，并按同期用药集合拆分为单药或联合用药 regimen。
- 对每个实际观察到的暴露组分别统计不良反应。
- 可选地将模型提出的 MedDRA PT 与本地授权词表 `pt.asc` 做精确匹配。
- 输出按“用户 + 暴露组 + PT”去重的频数，并为各单药组和联合用药组分别绘制前十症状图。
- 表格和图表同时输出中文、英文版；英文 MedDRA PT 作为统计主键，中文是帮助理解的辅助释义。
- `extractions.jsonl` 是可续跑 checkpoint；只跳过已完成记录，失败或中断记录会在下次重试。
- 用输入指纹、提示词版本、药物列表和模型配置校验 checkpoint，防止误用过期结果。
- 并发 worker 通过队列交结果，由单一写入器逐条 `flush + fsync`；输出目录跨进程锁阻止两个任务共用同一 checkpoint。

## 当前实现状态

当前代码已支持输入一种或多种目标药物。例如：

```powershell
--target-drugs dapagliflozin empagliflozin canagliflozin ertugliflozin
```

模型返回作者在每个时间段内实际使用的目标药物集合，代码再将集合规范化为稳定的 `exposure_group`。只使用其中一种时归入 `single`；同期使用两种或更多时归入 `combination`。不需要事先穷举所有组合，程序只保留数据中实际观察到的组。

先停 A 再用 B 会拆成两个单药 regimen，不会合并成 `A + B`。计划、假设、药物比较、一般讨论、他人用药和无法判断的提及不进入暴露统计。

提示词要求模型识别通用名、商品名、复方产品名、缩写、轻微拼写错误和字母倒置，并将其映射回命令行中的标准药名。映射后代码还会执行目标药物白名单过滤。这能提高容错性，但不能保证模型永远判断正确；含糊拼写应被放弃，而不是猜测。此外，如果爬取或预过滤阶段未收集到含错别字的原帖，后续模型无法弥补这类召回损失。

## 安装与配置

建议使用 Python 3.11。

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
```

在项目根目录的 `.env` 中填写：

```dotenv
model_url=https://api.deepseek.com
api_key=your_deepseek_api_key
model_name=deepseek-flash
```

`.env` 不应提交到 Git。

## 输入数据

必需字段：

| 字段 | 说明 |
|---|---|
| `user_id` | 用户唯一标识；建议在传入前做带盐哈希 |
| `message` | 需分析的文本；如果没有该列，可使用 `body` |

可选字段：

```text
record_id / post_id / id
title
subreddit
date
```

`pharmacovigilance_pipeline.py` 不会自动下载 Reddit 数据。独立的 `download_reddit_data.py` 负责从 Arctic Shift API 下载原始帖子和评论。完成定向下载后，可直接把 `data/reddit/targeted_comments` 作为 `--input`；脚本会根据 manifest 读取原 post，合并评论，生成 `post:<id>` / `comment:<id>` 稳定记录 ID，保留 `thread_id`、`parent_id`、`source_type`、社区和时间，并对用户名做带盐伪名化。

## Reddit 数据下载

下载器默认覆盖 8 个目标社区，时间窗口为 UTC `[2020-09-22, 2026-09-22)`，同时下载 posts 和 comments。它按年切分任务，逐页写入 JSONL，并在终端显示当前任务、时间进度、记录数、速度和时间游标。

先只查看任务计划，不发出网络请求：

```powershell
python download_reddit_data.py --dry-run
```

确认后开始下载：

```powershell
python download_reddit_data.py
```

可在任意时候按 `Ctrl+C` 中断。以相同参数重新执行时，已完成的切片会跳过，未完成切片从同目录的 `.state.json` 记录继续。输出默认写入 `data/reddit/raw/<subreddit>/<posts|comments>/`，该目录已被 Git 忽略。

自定义社区或时间范围的示例：

```powershell
python download_reddit_data.py `
  --subreddits diabetes Heartfailure kidneydisease `
  --start-date 2020-09-22 `
  --end-date 2026-09-22
```

Arctic Shift 是免费服务，下载器默认在成功请求之间等待 0.8 秒，并对 429、5xx、超时、远端断开、连接重置、TLS 中断和响应截断做带可见倒计时的持续退避重试。Arctic Shift 有时会将内部超时返回为 `422 Timeout. Maybe slow down a bit`；程序仅对正文明确为 timeout/slow down 的 422 按临时错误处理，其他 422 仍立即报错。定向评论在时间窗尾部还会通过倒序查询核验最新记录，避免对已空的尾页无限重试。不建议为了提速而把 `--request-delay` 设得过低。如果需要限制单页重试次数，可显式传入 `--max-retries N`。

### 按药物相关帖子下载评论（推荐）

如果 posts 已下载，不建议再下载整个社区的全量 comments。目标评论模式会先扫描本地 posts，用通用名、商品名、复方名和错拼召回候选帖子，再根据 post ID 下载每个候选帖子的完整评论。召回时先执行精确匹配；长度不超过 6 个字符的预设名称只做精确匹配，长度至少 7 个字符的名称还会使用 Damerau-Levenshtein 编辑距离计算字符相似度，相似度达到 80% 即作为模糊命中。该距离把增字、漏字、错字和相邻字母颠倒都视为编辑操作。

先做本地 dry run：

```powershell
python download_reddit_data.py `
  --targeted-comments-from-posts data/reddit/raw_10years `
  --output-dir data/reddit/targeted_comments `
  --subreddits diabetes diabetes_t2 type2diabetes diabetesuk Heartfailure kidneydisease ChronicKidneyDisease IgANephropathy `
  --start-date 2016-09-22 `
  --end-date 2026-09-22 `
  --dry-run
```

去掉 `--dry-run` 后开始目标评论下载。结果保存为：

```text
data/reddit/targeted_comments/<subreddit>/comments/comments_for_<post_id>.jsonl
```

#### Study A：召回 posts 并下载对应 comments

Study A 使用 Dapagliflozin、Empagliflozin、Canagliflozin 和 Ertugliflozin 的内置别名表，扫描已经下载的十年 posts。下面的命令只召回具体药物名称或其模糊匹配，不加入只出现 `SGLT2`/`gliflozin` 类别词的帖子。

开始前建议先查看召回数量；dry run 不写 manifest，也不下载评论：

```powershell
python download_reddit_data.py `
  --targeted-comments-from-posts data/reddit/raw_10years `
  --output-dir data/reddit/targeted_comments `
  --subreddits diabetes diabetes_t2 type2diabetes diabetesuk Heartfailure kidneydisease ChronicKidneyDisease IgANephropathy `
  --start-date 2016-09-22 `
  --end-date 2026-09-22 `
  --dry-run
```

确认数量后，使用下面的正式命令：

```powershell
python download_reddit_data.py `
  --targeted-comments-from-posts data/reddit/raw_10years `
  --output-dir data/reddit/targeted_comments `
  --subreddits diabetes diabetes_t2 type2diabetes diabetesuk Heartfailure kidneydisease ChronicKidneyDisease IgANephropathy `
  --start-date 2016-09-22 `
  --end-date 2026-09-22 `
  --targeted-comment-workers 8
```

dry run 和正式运行在扫描本地 posts 时都会显示实时进度条，包括按文件字节计算的总百分比、已扫描记录数、当前召回数和正在读取的文件。正式运行随后会先写入 `data/reddit/targeted_comments/recalled_posts.jsonl` 和 `recall_summary.json`，再按其中的 post ID 通过 `link_id` 下载每条帖子的完整评论线程。

定向评论下载使用有界线程池；`--targeted-comment-workers` 可设为 1–32，默认和推荐起始值为 8。每个 post ID 只会提交一次，每个 worker 独占该 post 的 JSONL 和状态文件，不会并发写同一文件。续传时会读取该帖子已保存的全部 comment ID，对历史页重叠和同页重复同时去重。输出目录还有跨进程排他锁；如果误开第二个针对同一 `--output-dir` 的下载命令，第二个会立即报错退出。总进度会显示完成线程数、活跃 worker、已保存评论数和本次页数。worker 数越高越容易触发数据源限流；建议先用 8，稳定后再尝试 12 或 16，不建议直接使用 32。

评论输出和状态文件按 post 分开保存；网络中断或手动按一次 `Ctrl+C` 后，程序会停止待执行任务，并通知正在运行的 worker 在安全页边界退出。重新执行完全相同的正式命令即可续跑。不要增加 `--include-class-only`，除非研究方案明确决定纳入只提到药物类别、没有具体药名的帖子。

正式续跑默认直接读取输出目录中已有的 `recalled_posts.jsonl` 和 `recall_summary.json`，不会重新扫描十年 posts。程序会核对源目录、日期、社区、别名、模糊匹配规则、class-only 设置和测试限额；参数不一致时拒绝复用并提示处理方式。只有药物召回规则或研究范围发生变化、确实需要重新生成候选集时，才在正式命令末尾增加 `--refresh-recall`。`--dry-run` 为了重新计算预览数字，仍会执行本地扫描。

`recall_summary.json` 是后续报告的固定数据源，记录扫描文件数、范围内原始 post 数、召回总数、召回率、各社区和各目标药物的召回数、精确/模糊命中数量、具体命中词频、多药物提及帖子数以及候选帖子报告的评论总数。这里的数字仅代表药名候选召回，不能解释为本人实际用药、副作用人数或临床发生率。

每个 post 都有独立状态文件，可以按 `Ctrl+C` 停止后以原命令继续。`recalled_posts.jsonl` 记录逐帖候选明细；`recall_summary.json` 保存可直接用于汇报的汇总统计；`drug_match_details` 还会记录原文命中词、对应别名、精确或模糊命中、编辑距离和相似度，便于检查模糊召回产生的噪声。默认只召回出现具体药名的帖子；增加 `--include-class-only` 后，也会包含只提到 `SGLT2` 或 `gliflozin` 的帖子。类别词不会参与模糊匹配。

未来分析其他药物时，可通过 `--drug-alias-file aliases.json` 替换默认 SGLT2 别名表。JSON 格式为：

```json
{
  "canonical_drug_name": ["canonical_drug_name", "brand_name", "common_typo"]
}
```

## 当前运行方式

先只执行全量本地清洗，不调用 DeepSeek：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/targeted_comments `
  --output-dir output/study_a `
  --target-drugs dapagliflozin empagliflozin canagliflozin ertugliflozin `
  --prepare-only
```

该命令生成 `cleaned_posts.csv` 和 `cleaning_summary.json`。首次运行还会在输出目录创建本地 `.user_hash_salt`；同一输出目录后续运行会复用它，以保持用户伪名稳定。不要公开该盐值文件。

再用独立输出目录做小样本模型测试：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/targeted_comments `
  --output-dir output/study_a_test100 `
  --target-drugs dapagliflozin empagliflozin canagliflozin ertugliflozin `
  --limit 100 `
  --concurrency 10 `
  --min-chart-users 1
```

确认输出无误后再扩大并发：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/targeted_comments `
  --output-dir output/study_a `
  --target-drugs dapagliflozin empagliflozin canagliflozin ertugliflozin `
  --meddra-pt MedDRA_28_0_English/MedAscii/pt.asc `
  --concurrency 150 `
  --min-chart-users 5
```

`--meddra-pt` 是可选参数。未提供时，模型提出的 PT 会标记为 `not_checked`，不代表已经通过 MedDRA 词表校验。

Study A 内置了达格列净、恩格列净、卡格列净和艾托格列净的中文图表名称。未来研究其他药物时，可准备 UTF-8 JSON 映射文件：

```json
{
  "semaglutide": "司美格鲁肽",
  "tirzepatide": "替尔泊肽"
}
```

并在命令中增加 `--drug-labels-zh drug_labels_zh.json`。模型返回的 `meddra_pt_zh` 是简体中文辅助释义，不能冒充授权中文 MedDRA 官方术语；英文 `meddra_pt` 仍是聚合、去重和可选 `pt.asc` 校验的主键。

按 `Ctrl+C` 中断后，以完全相同的输入、药物列表、模型和输出目录重新运行，程序会跳过 checkpoint 中已完成的记录，只请求未完成项。若输入、提示词、药物列表或模型配置改变，程序会拒绝复用旧 checkpoint；请换新 `--output-dir`，或在明确希望丢弃旧模型结果时使用 `--restart`。

## 当前输出

| 文件 | 说明 |
|---|---|
| `analysis_metadata.json` | 本次目标药物和纳入规则 |
| `cleaning_summary.json` | 清洗前后数量、分原因删除数、用户数和 post/comment 数 |
| `cleaned_posts.csv` | 清理和去重后的输入 |
| `checkpoint_metadata.json` | 用于拒绝不兼容续跑的输入/提示词/模型指纹 |
| `extractions.jsonl` | 按记录持久化的模型 checkpoint；已完成项续跑时跳过 |
| `post_extractions_en.csv` / `_zh.csv` | 每条成功记录的暴露组列表，英文/中文版 |
| `exposure_records_en.csv` / `_zh.csv` | 每条记录拆分后的 regimen 明细，英文/中文版 |
| `adverse_events_en.csv` / `_zh.csv` | 各 regimen 下的不良反应明细，中文版同时保留英文 PT |
| `exposure_group_summary_en.csv` / `_zh.csv` | 各暴露组的记录数、暴露用户数和症状报告用户数 |
| `pt_frequency_by_group_en.csv` / `_zh.csv` | 各暴露组按独立用户去重的 PT 频数及两种分母百分比 |
| `charts/en/<single|combinations>/*.png` | 英文版单药/联合用药前十症状图 |
| `charts/zh/<single|combinations>/*.png` | 中文版单药/联合用药前十症状图 |

`percent_of_exposed_users` 的分母是该暴露组全部用户，`percent_of_event_reporters` 的分母是该组中至少报告一项不良反应的用户。两者都不是药物不良反应的临床发生率。

## Reddit 数据与合规

原研究使用 Pushshift 和 Arctic Shift 数据，时间范围为 2015 年 1 月至 2025 年 6 月，并从 9 个 GLP-1 或减重相关 subreddit 中过滤 semaglutide 和 tirzepatide 相关记录。原始 Reddit 数据未包含在本仓库中。

后续收集数据时应：

- 遵守 Reddit 及数据来源的最新使用条款。
- 仅保留研究所需字段，不发布原始用户名和原文。
- 在聚合同一用户内容前对用户名做带盐哈希。
- 保留 `record_id`、`thread_id`、`parent_id`、`subreddit` 和 `created_utc`，以便去重和恢复必要的讨论上下文。

## 给后续 Agent 的工作约定

如果你是继续维护该项目的 Agent，请先完整阅读本 README、`PROJECT_REPORT_SOURCE.md` 和 `pharmacovigilance_pipeline.py`，再开始修改。凡是影响研究范围、数据状态、流程、统计口径或汇报叙事的改动，都必须同步更新 `PROJECT_REPORT_SOURCE.md` 的日期、状态和变更记录。

### 修改原则

1. 分析、统计和绘图逻辑主要位于 `pharmacovigilance_pipeline.py`；数据下载与前置召回逻辑位于 `download_reddit_data.py`。修改时保持两者输入输出约定一致。
2. 每次修改模型 JSON 结构时，必须同步修改系统提示词、`normalize_extraction()`、扁平化导出、统计函数、绘图函数和 README。
3. 模型 checkpoint 必须同时校验输入指纹、提示词版本、药物列表和模型配置；只跳过兼容 checkpoint 中的已完成项。修改任一上述条件时应使用新输出目录，不得静默混用旧结果。
4. 不要让模型返回判断证据或冗长解释；只返回后续统计必需的结构化字段。
5. “提到药物”不等于“作者本人使用药物”。计划、假设、咨询、比较和他人用药不应进入暴露统计。
6. 联合用药必须有同期使用语义；先停 A 再用 B 只能归入两个单药 regimen。
7. 统计默认按“用户 + 暴露组 + MedDRA PT”去重，不要让同一用户重复发帖放大频数。
8. 输入药物较多时，只为实际观察到且达到最低用户数的组合绘图，避免指数级生成空图。
9. 开发时先用 `--limit 100 --concurrency 10`；只有结构、统计和图表验证通过后，才建议使用 150 并发跑全量数据。
10. 完成修改后至少运行语法检查、模型返回规范化测试、统计去重测试和端到端小样本导出测试。

### 当前单药/组合管线的数据口径

1. 模型输出 `regimens` 列表，每个 regimen 只包含同期使用的目标药物和该阶段的不良反应。
2. 药物名顺序由 `--target-drugs` 的输入顺序固定，因此模型返回顺序不会造成重复暴露组。
3. 同一条记录中重复的 regimen 会合并，重复的不良反应会在规范化阶段去重。
4. PT 频数按“暴露组 + 用户 + 分析术语”去重，同一用户重复发帖不会重复增加同一组的同一 PT 频数。
5. `--min-chart-users` 控制绘图的最低暴露用户数，不影响 CSV 中的统计结果。

### 必须向用户解释的内容

Agent 在交付修改或指导运行时，必须用清楚的中文向用户说明：

- 本次修改了哪些文件和数据口径。
- 药物列表如何传入，“单药”和“联合用药”如何定义。
- 哪些文本会被排除，特别是换药、计划用药、他人用药和无法判断的记录。
- 实际运行命令、`.env` 要求、并发数和输出目录。
- 每个 CSV 和图表表示什么，统计分母是什么。
- 哪些测试已经通过，是否真实调用了 DeepSeek API。
- Reddit 自报数据存在自选偏差和报告偏差，结果只能用于生成待验证的信号，不能证明因果或真实发生率。

Agent 不应把尚未实现的功能描述为已完成，也不应在未做真实 API 调用时声称“整条管线已跑通”。

## 参考链接

- [原始代码仓库](https://github.com/sehgal-neil/glp1-side-effects-analysis)
- [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift)
- [Pushshift API](https://github.com/pushshift/api)
- [MedDRA MSSO](https://www.meddra.org/)
