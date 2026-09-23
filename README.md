# Reddit 药物安全性分析

本项目从指定 Reddit 社区下载帖子，按药名召回候选帖子，再只下载这些帖子的评论。随后对帖子和评论做本地清洗，调用 DeepSeek 判断作者是否实际使用目标药物、是否报告症状，最后按单药和同期联合用药分别生成统计表与中英文图表。

目标药物由命令行指定，不要求全部药物同时使用。换药先后使用不算联用；药名提及也不等于本人用药。结果是特定社区的自报信号，**不能当作不良反应发生率或因果证据**。

两个主文件：`download_reddit_data.py` 负责下载与前置召回；`pharmacovigilance_pipeline.py` 负责清洗、模型抽取、统计和绘图。下面以 Study B（Evolocumab、Alirocumab、Inclisiran）为完整示例；其他研究需一致地替换药物、别名、社区、日期及独立输出目录。

## 环境配置

建议 Python 3.11。在项目根目录执行（也可以使用已有的 conda 环境）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在项目根目录的 `.env` 中填写实际可用的模型服务配置：

```dotenv
model_url=https://你的模型服务地址
api_key=你的密钥
model_name=服务商提供的模型名
```

只有模型分析步骤需要 `.env`；下载、召回预览和 `--prepare-only` 不调用 DeepSeek。`.env`、原始数据、分析输出、本地报告和测试文件均被 Git 忽略，不要提交用户名、原文或密钥。

## 从数据到表图：逐步运行

以下命令均在项目根目录执行。日期使用 UTC 半开区间：`--start-date` 当天包含，`--end-date` 当天不包含。Study B 示例覆盖 `[2016-09-23, 2026-09-23)`，社区为 `Cholesterol`、`repatha`、`HeartAttack`、`HeartDisease`、`PeterAttia`。

### 1. 预览帖子下载计划

只打印社区和年度切片，不下载：

```powershell
python download_reddit_data.py `
  --output-dir data/reddit/study_b/raw_10years `
  --subreddits Cholesterol repatha HeartAttack HeartDisease PeterAttia `
  --kinds posts `
  --start-date 2016-09-23 `
  --end-date 2026-09-23 `
  --community-workers 5 `
  --dry-run
```

### 2. 下载帖子

不同社区并行，同一社区的年度切片顺序下载；每个切片有独立的 `.state.json` 断点文件。**此步不下载全社区评论。**

```powershell
python download_reddit_data.py `
  --output-dir data/reddit/study_b/raw_10years `
  --subreddits Cholesterol repatha HeartAttack HeartDisease PeterAttia `
  --kinds posts `
  --start-date 2016-09-23 `
  --end-date 2026-09-23 `
  --community-workers 5
```

按一次 `Ctrl+C` 可停止；原命令重跑会跳过已完成切片并续传未完成切片。先核对状态文件均为 `complete`，再进入下一步。

### 3. 预览药名召回

扫描本地帖子的标题和正文，使用 `study_b_drug_aliases.json` 中的三种通用名与商品名召回候选帖子；较长名称还允许 80% 字符相似度的错拼匹配。`--dry-run` **不写候选名单、不下载评论**。

```powershell
python download_reddit_data.py `
  --targeted-comments-from-posts data/reddit/study_b/raw_10years `
  --output-dir data/reddit/study_b/targeted_comments `
  --subreddits Cholesterol repatha HeartAttack HeartDisease PeterAttia `
  --start-date 2016-09-23 `
  --end-date 2026-09-23 `
  --drug-alias-file study_b_drug_aliases.json `
  --dry-run
```

核对召回帖数、各社区/药物分布和疑似错拼。Study B 不加 `--include-class-only`。召回只是候选筛选：**只在评论里提到药物、而原帖完全未命中的讨论串不会进入本次评论下载**。

### 4. 保存召回名单并下载候选帖评论

这条命令先写 `recalled_posts.jsonl`、`recall_summary.json`，**随后立即**按 post ID 并行下载对应的完整评论线程：

```powershell
python download_reddit_data.py `
  --targeted-comments-from-posts data/reddit/study_b/raw_10years `
  --output-dir data/reddit/study_b/targeted_comments `
  --subreddits Cholesterol repatha HeartAttack HeartDisease PeterAttia `
  --start-date 2016-09-23 `
  --end-date 2026-09-23 `
  --drug-alias-file study_b_drug_aliases.json `
  --targeted-comment-workers 8
```

中断后原命令续传会复用候选名单，跳过已完成线程。核对所有线程状态、评论 JSONL 行数与 state `count`、唯一评论 ID；`recall_summary.json` 的帖子 `num_comments` 合计只是预估，不是实际下载量。若修改日期、社区或别名，请使用**新的输出目录**，不要混用旧 manifest。

### 5. 只做本地清洗

合并召回帖与评论、清理空/删除/重复文本、伪名化用户；**不请求模型**：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/study_b/targeted_comments `
  --output-dir output/study_b_analysis `
  --target-drugs evolocumab alirocumab inclisiran `
  --prepare-only
```

查看 `output/study_b_analysis/cleaning/cleaning_summary.json` 和 `cleaned_posts.csv`，确认清洗前后数量及 post/comment 构成。用户伪名盐保存在同一输出目录的 `state/.user_hash_salt`，不要公开。

### 6. 用独立目录做 100 条模型测试

**从这一步开始调用 DeepSeek 并产生费用。** 先小样本检查 JSON 抽取、单药/换药/联用判断及表图：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/study_b/targeted_comments `
  --output-dir output/study_b_test100 `
  --target-drugs evolocumab alirocumab inclisiran `
  --limit 100 `
  --concurrency 10 `
  --min-chart-users 1
```

`--limit 100` 取清洗后的**前** 100 条，不是按药物分层抽样；某种药物没有出现不能说明模型不支持它。测试目录与全量目录分开，不能将小样本结果当成最终统计。

### 7. 全量模型分析与续传

确认 API 余额、模型输出及小样本口径后，去掉 `--limit`，在正式目录处理全部清洗记录：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/study_b/targeted_comments `
  --output-dir output/study_b_analysis `
  --target-drugs evolocumab alirocumab inclisiran `
  --concurrency 150 `
  --min-chart-users 5
```

中断后**重跑同一命令**：模型 checkpoint 会跳过已成功记录、重试失败项；本地清洗仍会重做。不要用 `--restart` 续跑，它会丢弃模型 checkpoint。HTTP 402 `Insufficient Balance` 需要先补足余额，降低并发不能解决余额不足。只有成功记录完整后，表图才可视为完整结果。`--meddra-pt` 可选，用于用本地授权 `pt.asc` 精确检查候选 PT；未提供时 PT 为模型建议、未经过词表校验。

### 8. 只从既有结果重算两张中文附录表（可选）

不重新清洗、不调用 API，也不重画柱状图：

```powershell
python pharmacovigilance_pipeline.py `
  --tables-only `
  --output-dir output/study_b_analysis `
  --target-drugs evolocumab alirocumab inclisiran
```

## 结果文件

| 路径（相对项目根目录） | 内容 |
|---|---|
| `data/reddit/study_b/raw_10years/<社区>/posts/*.jsonl` | 原始帖子；同目录 `.state.json` 为下载断点 |
| `data/reddit/study_b/targeted_comments/recalled_posts.jsonl` | 逐帖候选名单和药名命中明细 |
| `data/reddit/study_b/targeted_comments/recall_summary.json` | 原帖分母、召回率、社区/药物分布及预计评论数 |
| `data/reddit/study_b/targeted_comments/<社区>/comments/comments_for_<post_id>.jsonl` | 该候选帖的评论；同目录 `.state.json` 为断点 |
| `output/study_b_analysis/cleaning/cleaning_summary.json`、`cleaned_posts.csv` | 清洗前后数量与逐条模型输入 |
| `output/study_b_analysis/state/extractions.jsonl`、`checkpoint_metadata.json` | 逐条模型结果与续跑参数指纹；同目录私密伪名盐不要公开 |
| `output/study_b_analysis/records/post_extractions_{en,zh}.csv` | 逐记录的模型判断 |
| `output/study_b_analysis/records/exposure_records_{en,zh}.csv` | 单药/同期联合用药阶段明细 |
| `output/study_b_analysis/records/adverse_events_{en,zh}.csv` | 各用药阶段的症状事件明细 |
| `output/study_b_analysis/tables/exposure_group_summary_{en,zh}.csv` | 各暴露组的记录数、用户数与症状报告用户数 |
| `output/study_b_analysis/tables/pt_frequency_by_group_{en,zh}.csv` | 按用户去重的症状术语频数与百分比 |
| `output/study_b_analysis/tables/appendix_table_1_symptom_pairs_zh.csv` | 中文症状两两共现表 |
| `output/study_b_analysis/tables/appendix_table_2_exclusive_single_drug_zh.csv` | 中文严格单药对照表 |
| `output/study_b_analysis/charts/en/`、`charts/zh/` | 各达到最低用户数的单药/联用组前十症状柱状图 |

统计按“用户＋暴露组＋症状术语”去重；表中百分比的分母应以对应列说明为准，不是临床发生率。中文症状名是阅读辅助释义，不冒充授权中文 MedDRA 术语。组合组没有柱状图，可能只是低于 `--min-chart-users` 门槛，仍应查看 CSV。

## 给后续 Agent 的指导

1. 先读本 README 与两个主脚本；如本机有研究记录 `PROJECT_REPORT_SOURCE.md`，再结合它核对当前进度。该记录及本地 `tests/` 不属于公开仓库，**不要推送**；不存在时不要假定历史数字适用于新机器。
2. 先检查数据、状态文件和 checkpoint，再向用户给出**当前应运行的一条命令**及预期输出；长时间下载或付费模型任务由用户自行观察进度，除非用户明确要求代跑。不要一次执行整条管线。
3. 每项研究保持药物列表、别名文件、社区/日期和目录一致。召回命中不是本人用药；模型只有确认作者实际使用时才计入，先后换药不能算同期联合。只统计数据中实际出现的组。
4. 下载和模型都可续跑：原参数重跑，已完成项跳过；改变召回范围或模型 checkpoint 关键配置时换新输出目录，不要静默混用，不要随意使用 `--restart`。
5. 汇报前核对原始帖、召回帖、实得评论、清洗后记录、模型成功/失败数、分组用户数和成本；有失败项时现有表图只能标为阶段性结果。不得把 Reddit 自报解释为临床确诊、不良反应真实发生率或因果关系。
6. 修改代码后至少做语法检查、相关逻辑测试和小样本导出验证；明确告诉用户哪些检查做过、是否真实请求过 API。不要提交 `.env`、原始数据、模型输出、测试文件或本地报告。
