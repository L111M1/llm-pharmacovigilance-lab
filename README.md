# Reddit 药物安全性分析

从指定 Reddit 社区中，按药物名、商品名及常见错拼**直接搜索帖子和评论**，只保留本地复核命中的记录。本地清理后，DeepSeek 判断作者实际使用的目标药物和描述的症状，按单药及同期联用生成中英文结果和中文附录表。Reddit 自报数据不能解释为临床不良反应发生率或因果证据。

正式采集入口为 `download_reddit_data.py`，搜索与断点逻辑在 `keyword_recall.py`，清洗、模型抽取及统计在 `pharmacovigilance_pipeline.py`。**已删除**下载社区全部帖文、先召回帖子再下载整条评论线程的入口；历史数据仍可由分析脚本读取，已有模型结果无需重跑。

## 环境

建议 Python 3.11，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

只有模型调用需要在项目根目录 `.env` 中填写 `model_url`、`api_key`、`model_name`。不要提交密钥、原始数据或分析输出。

## 召回规则与处理流程

1. 每种药物只需配置**正确**通用名和商品名。Study A 可用内置别名；Study B 用 `study_b_drug_aliases.json`。药名长度小于 7 字母只精确检索；较长词自动生成一次字符遗漏、相邻字母颠倒、元音/键盘邻键替换的有限候选，且 `1 − 编辑距离 / 两词最大长度 ≥ 0.80`。跨药物歧义候选不用于自动归类。错拼集合是受控近似，不保证覆盖所有写法。
2. 帖子通过标题和正文搜索两类词：药物词，以及副作用相关英文表达（如 `side effect(s)`、`side-effect(s)`、`adverse effect(s)`、`adverse reaction(s)`、`adverse event(s)`、`drug reaction`、`symptom(s)`、`intolerance`），并给相关英文单词生成有限的一次编辑错拼。**药物词或副作用词命中任一即可成为帖子候选**；副作用词单独命中的帖子通常会在后续药物判定中被排除，但保留它们可减少仅在正文隐晦提药时的漏召回。帖子搜索接口不承诺 OR 语义，因此每个词单独查询。
3. 评论正文按药物词搜索；最多 8 个词合并为一次 `OR` 查询，**只保存命中的评论**，不下载所属讨论串的其他回复。评论不以宽泛的 `side effects` 单独召回，以避免下载海量无关评论。帖子与评论最终均在本地按词界和相似度复核，再按 Reddit ID 去重。搜索语法参考 [Arctic Shift API 文档](https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md)。
4. 查询按社区和关键词拆分，可多线程运行；每项先搜索完整日期区间，仅在 HTTP 422 时自动二分时间窗口，保存候选 JSONL 与状态文件。网络故障会重试。按 `Ctrl+C` 停止后，用**完全相同命令**继续。所有查询完成后，复核结果写入 `verified_posts.jsonl` / `verified_comments.jsonl`；统计在各自的 `manifest.json`。新增记录按完成顺序只追加到共享的 `direct_records.jsonl`，使既有模型 checkpoint 的输入前缀保持不变。更改日期、社区或别名时使用新的数据目录。
5. 分析脚本先读取历史帖子与线程数据，再按 `direct_records.jsonl` 顺序读新帖子/评论。`--append-only` 校验旧清洗记录指纹，旧成功和旧失败的模型结果都不会因增量输入而重跑；新失败记录可在续跑时重试。任何直搜任务未完成时，分析脚本会拒绝读取该目录。新评论缺少父评论/原帖上下文，模糊陈述可能被保守排除。

## Study B：Evolocumab、Alirocumab、Inclisiran

下列命令均在项目根目录执行。时间为 UTC 半开区间 `[2016-09-23, 2026-09-23)`；输出目录沿用已有 Study B 数据和模型结果，不覆盖历史文件。`--dry-run` 可先加在下载命令末尾预览任务数，不联网、不写文件。

先搜索帖子：

```powershell
python download_reddit_data.py `
  --direct-posts `
  --output-dir data/reddit/study_b/targeted_comments `
  --subreddits Cholesterol repatha HeartAttack HeartDisease PeterAttia `
  --start-date 2016-09-23 --end-date 2026-09-23 `
  --drug-alias-file study_b_drug_aliases.json `
  --search-workers 2
```

再搜索评论：

```powershell
python download_reddit_data.py `
  --direct-comments `
  --output-dir data/reddit/study_b/targeted_comments `
  --subreddits Cholesterol repatha HeartAttack HeartDisease PeterAttia `
  --start-date 2016-09-23 --end-date 2026-09-23 `
  --drug-alias-file study_b_drug_aliases.json `
  --search-workers 2
```

本地清洗预览（不调用模型）：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/study_b/targeted_comments `
  --output-dir output/study_b_analysis `
  --target-drugs evolocumab alirocumab inclisiran `
  --append-only --prepare-only
```

确认新增记录和费用预算后，调用模型并更新表图：

```powershell
python pharmacovigilance_pipeline.py `
  --input data/reddit/study_b/targeted_comments `
  --output-dir output/study_b_analysis `
  --target-drugs evolocumab alirocumab inclisiran `
  --append-only --concurrency 150 --min-chart-users 5
```

只用已有模型结果重算中文附录表（不调用模型）：

```powershell
python pharmacovigilance_pipeline.py `
  --tables-only --output-dir output/study_b_analysis `
  --target-drugs evolocumab alirocumab inclisiran
```

Study A 下载同样使用 `--direct-posts`、`--direct-comments` 两步，输出目录改为 `data/reddit/targeted_comments`，社区改为 `diabetes diabetes_t2 type2diabetes diabetesuk Heartfailure kidneydisease ChronicKidneyDisease IgANephropathy`，日期改为 `2016-09-22` 到 `2026-09-22`，且**不传** Study B 的别名文件。分析时沿用 `output/study_a_analysis`、`--append-only`，目标药物为 `dapagliflozin empagliflozin canagliflozin ertugliflozin`。新研究若没有旧模型 checkpoint，使用独立目录并省略 `--append-only`。

## 结果位置

| 路径（以 Study B 为例） | 内容 |
|---|---|
| `data/reddit/study_b/targeted_comments/direct_posts/search/`、`direct_comments/search/` | 各查询的候选和续跑状态；并非最终命中数 |
| `direct_posts/verified_posts.jsonl`、`direct_comments/verified_comments.jsonl` | 本地复核、排除历史 ID 后的去重记录 |
| `direct_posts/manifest.json`、`direct_comments/manifest.json` | 候选、排除、有效及追加数量 |
| `data/reddit/study_b/targeted_comments/direct_records.jsonl` | 新帖子和新评论的只追加输入日志 |
| `output/study_b_analysis/cleaning/`、`state/` | 清洗数据、逐条模型结果与断点 |
| `output/study_b_analysis/records/`、`tables/`、`charts/en/`、`charts/zh/` | 明细、统计表、英文及中文图表 |

症状统计按用户、暴露组和术语去重；中文医学术语仅辅助阅读，不冒充授权中文 MedDRA。`--min-chart-users` 仅控制图中最少用户数，详情见命令行帮助。

## 给后续 Agent 的指导

先检查数据目录、两个 `manifest.json`、共享追加日志和模型 checkpoint，再提供用户所需的**下一条命令**。长时间下载和付费模型步骤默认由用户自己启动与观察，除非明确要求代跑。不要重新启用社区全量帖子下载或按帖子抓完整评论线程；不能把候选查询数当成真实召回数。沿用旧模型结果必须加 `--append-only`，不要用 `--restart` 续跑。汇报时分别说明候选、旧 ID 跳过、复核有效、清洗后、模型成功/失败与用户级统计，并提醒自报数据不能证明药物因果关系。
