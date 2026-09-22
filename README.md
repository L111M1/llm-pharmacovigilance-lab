# Reddit 药物安全性与不良反应分析

本仓库提供一条面向 Reddit 自报文本、可自定义目标药物、使用 DeepSeek 普通 API 的药物警戒分析管线。项目借鉴了论文《Self-Reported Side Effects of Semaglutide and Tirzepatide in Online Communities》的研究问题，但当前实现已经独立重写，不包含原论文的四阶段方法脚本。

本项目的结果用于发现 Reddit 用户关心的潜在不良反应信号，不能用于证明药物因果关系，也不能解读为临床发生率。

## 当前代码

| 文件 | 作用 |
|---|---|
| `download_reddit_data.py` | 下载 Reddit posts，并从药物相关 posts 定向下载评论 |
| `deepseek_pharmacovigilance.py` | 清理输入、调用 DeepSeek、识别实际用药组合、抽取症状并生成统计和图表 |

当前已实现：

- 从 CSV、JSONL 或 NDJSON 读取已爬取的 Reddit 文本。
- 清理空文本、删除内容和重复记录。
- 通过 DeepSeek 普通 Chat Completions API 进行并发抽取。
- 使用根目录 `.env` 中的 `model_url`、`api_key` 和 `model_name`。
- 识别作者实际使用的目标药物，并按同期用药集合拆分为单药或联合用药 regimen。
- 对每个实际观察到的暴露组分别统计不良反应。
- 可选地将模型提出的 MedDRA PT 与本地授权词表 `pt.asc` 做精确匹配。
- 输出按“用户 + 暴露组 + PT”去重的频数，并为各单药组和联合用药组分别绘制前十症状图。
- 每次运行都覆盖 `extractions.jsonl`，不使用 checkpoint，不复用任何旧模型结果。

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

`deepseek_pharmacovigilance.py` 不会自动下载 Reddit 数据。独立的 `download_reddit_data.py` 负责从 Arctic Shift API 下载原始帖子和评论。下载结果还需经过本地药名预筛和字段转换，才能作为 DeepSeek 管线的输入。

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

Arctic Shift 是免费服务，下载器默认在成功请求之间等待 0.8 秒，并对 429、5xx、超时、远端断开、连接重置、TLS 中断和响应截断做带可见倒计时的持续退避重试。不建议为了提速而把 `--request-delay` 设得过低。如果需要限制单页重试次数，可显式传入 `--max-retries N`。

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

每个 post 都有独立状态文件，可以按 `Ctrl+C` 停止后以原命令继续。`recalled_posts.jsonl` 记录候选 post ID、社区、时间、匹配的标准药名和原始文件位置；`drug_match_details` 还会记录原文命中词、对应别名、精确或模糊命中、编辑距离和相似度，便于检查模糊召回产生的噪声。默认只召回出现具体药名的帖子；增加 `--include-class-only` 后，也会包含只提到 `SGLT2` 或 `gliflozin` 的帖子。类别词不会参与模糊匹配。

未来分析其他药物时，可通过 `--drug-alias-file aliases.json` 替换默认 SGLT2 别名表。JSON 格式为：

```json
{
  "canonical_drug_name": ["canonical_drug_name", "brand_name", "common_typo"]
}
```

## 当前运行方式

先用小样本测试：

```powershell
python deepseek_pharmacovigilance.py `
  --input data/reddit_posts.csv `
  --output-dir output/test_run `
  --target-drugs dapagliflozin empagliflozin canagliflozin ertugliflozin `
  --limit 100 `
  --concurrency 10 `
  --min-chart-users 1
```

确认输出无误后再扩大并发：

```powershell
python deepseek_pharmacovigilance.py `
  --input data/reddit_posts.csv `
  --output-dir output/sglt2_analysis `
  --target-drugs dapagliflozin empagliflozin canagliflozin ertugliflozin `
  --meddra-pt MedDRA_28_0_English/MedAscii/pt.asc `
  --concurrency 150 `
  --min-chart-users 5
```

`--meddra-pt` 是可选参数。未提供时，模型提出的 PT 会标记为 `not_checked`，不代表已经通过 MedDRA 词表校验。

## 当前输出

| 文件 | 说明 |
|---|---|
| `analysis_metadata.json` | 本次目标药物和纳入规则 |
| `cleaned_posts.csv` | 清理和去重后的输入 |
| `extractions.jsonl` | 本次运行逐条写入的模型结果；不是 checkpoint |
| `post_extractions.csv` | 每条成功记录的暴露组列表 |
| `exposure_records.csv` | 每条记录拆分后的 regimen 明细 |
| `adverse_events.csv` | 各 regimen 下的不良反应明细 |
| `exposure_group_summary.csv` | 各暴露组的记录数、暴露用户数和症状报告用户数 |
| `pt_frequency_by_group.csv` | 各暴露组按独立用户去重的 PT 频数及两种分母百分比 |
| `charts/single/*.png` | 各已观察单药组的前十症状图 |
| `charts/combinations/*.png` | 各已观察联合用药组的前十症状图 |

`percent_of_exposed_users` 的分母是该暴露组全部用户，`percent_of_event_reporters` 的分母是该组中至少报告一项不良反应的用户。两者都不是药物不良反应的临床发生率。

## Reddit 数据与合规

原研究使用 Pushshift 和 Arctic Shift 数据，时间范围为 2015 年 1 月至 2025 年 6 月，并从 9 个 GLP-1 或减重相关 subreddit 中过滤 semaglutide 和 tirzepatide 相关记录。原始 Reddit 数据未包含在本仓库中。

后续收集数据时应：

- 遵守 Reddit 及数据来源的最新使用条款。
- 仅保留研究所需字段，不发布原始用户名和原文。
- 在聚合同一用户内容前对用户名做带盐哈希。
- 保留 `record_id`、`thread_id`、`parent_id`、`subreddit` 和 `created_utc`，以便去重和恢复必要的讨论上下文。

## 给后续 Agent 的工作约定

如果你是继续维护该项目的 Agent，请先完整阅读本 README 和 `deepseek_pharmacovigilance.py`，再开始修改。

### 修改原则

1. 分析、统计和绘图逻辑主要位于 `deepseek_pharmacovigilance.py`；数据下载与前置召回逻辑位于 `download_reddit_data.py`。修改时保持两者输入输出约定一致。
2. 每次修改模型 JSON 结构时，必须同步修改系统提示词、`normalize_extraction()`、扁平化导出、统计函数、绘图函数和 README。
3. 当前开发阶段不要重新引入 checkpoint、断点跳过或旧结果复用。每次运行必须从当前输入重新计算。
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
