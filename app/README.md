# app/ — 单文件背词应用（GRE Vocabulary Garden）

这个目录是构建产物（默认 `dist/En-735.html` 这类名字）的**源码**。产物由 `tools/build.py` 把下面几个文件
内联成一个离线 HTML，页面运行时只读取内嵌的 `VOCABULARY_DATA` / `LIST_DATA`。

| 文件 | 作用 |
| --- | --- |
| `template.html` | HTML 骨架 + 四个占位符（`/*__APP_CSS__*/`、`/*__VOCABULARY_DATA__*/`、`/*__LIST_DATA__*/`、`/*__APP_JS__*/`）。改结构从这里改。 |
| `app.css` | 全部样式（含深色模式、移动端单列布局）。只使用 CSS 变量，没有外部字体/CDN。 |
| `app.js` | 全部逻辑：数据规范化、同义关系图、词库浏览、list、flashcard session、Export、结果页。 |
| `../tools/build.py` | 构建脚本：读 `data/words.json` + `data/lists.json` + 上面三个文件 → `dist/<语言简称>-<词数>.html`。 |
| `../tests/test_build.py` | 构建、自动 list、容错、UI 语言的自动化测试。 |

## 日常使用

```powershell
# 1) 重新构建（词库/list/代码有任何变化都跑这一步）
#    默认产物名：[目标语言简称]-[本次 build 的词库词数].html → 目前是 dist\En-735.html
python tools\build.py

# 自定义产物文件名（放在 dist\ 下，不含 .html 会自动补；同名直接覆盖）
python tools\build.py --name my-tool        # → dist\my-tool.html
python tools\build.py --lang Fr             # → dist\Fr-735.html
python tools\build.py --out out/custom.html # 完整路径（优先于 --name）

# 只校验数据，不写文件（会打印“would write: …”的目标路径）
python tools\build.py --check

# 改自动 list 的大小（默认 30，0 = 不自动切分）
python tools\build.py --chunk-size 50
```

打开产物：直接双击 `dist\En-735.html`（词数变了文件名也会变），或把它拷到手机上用浏览器打开。

### 界面语言

交互界面全部是英文；只有“词条内容”（`chinese` 释义、例句的中文翻译、`personal_notes`）
保留中文。新增 UI 文案时不要再引入中文，`tests/test_build.py::UiLanguageTest` 会检查。

## list 机制

`data/lists.json` 只是**配置 + 自定义 list**，`all` 与 `part-NN` 由 build 自动生成：

```json
{
  "schema_version": 1,
  "chunk_size": 30,
  "lists": []
}
```

* `all`：`data/words.json` 里的全部单词。
* `part-01`、`part-02` …：按 `words.json` 顺序每 `chunk_size` 个词一个 list，
  最后不足一段的单独成为一个 list（例如 735 词 → `part-01` … `part-24` 各 30 词，
  `part-25` 15 词）。
* `lists` 数组里可以放自己的 list：

```json
{ "id": "week-1", "name": "Week 1", "description": "本周重点", "word_ids": ["abate", "abdicate"] }
```

手动 list 的 `id` 不能是 `all` 或 `part-NN`（这些 id 保留给自动生成的 list，会被忽略并给出 warning）。
增删单词后不需要手动改 list：重新 build 即以 `words.json` 为准重建。

## 给 agent 的更新词库操作手册

> 唯一数据源是 `data/words.json`（词汇）与 `data/lists.json`（list 配置）。
> **绝对不要直接改构建产物（`dist\*.html`）**，它是渲染结果。

### 0. 通用规则

1. 不改动 `data/words.schema.json` 的字段设计（本阶段不重新设计 schema）。
2. 所有写入都通过仓库自带脚本完成，不要手写 JSON patch。
3. 每次改完必须依次执行：`curate_gre.py` → `validate.py --write` → `build.py` → 跑测试。
4. `personal_notes` 属于学习者本人，只能追加，不能改写或删除。
5. 词条内容（`chinese`、`english_definitions`、`synonyms`、`examples` …）必须保持原样，
   除非本次任务明确要求修改该词。

### 1. 新增单词（用户给出几个单词）

#### 1.0 agent 必须先做的事：把用户输入规范化并补全词语信息

用户给出的“新词”通常**不是**现成的词典条目，可能是下面任意一种（甚至带笔误）：

* 裸词：`obdurate` / `obdurate, obstinate`
* 词 + 备注（用空格、tab、`|`、`：` 分隔）：`obdurate 顽固的，固执的`、`obdurate | 顽固`
* 短语、变形或口语写法：`beholden to sb.`、`obdurate（形容词）`、`obdurat`（笔误）

**不要把用户输入原样写进数据文件。** agent（或 agent 派出的 subagent）的职责是把它变成符合
`data/words.schema.json` 的规范条目，建议按下面六步做：

1. **拆词与规范化**：一行一个词，拆成 `term | 中文笔记`；统一大小写与标点、去掉括号里的词性说明；
   学习者原本的中文措辞要保留原意，不要自行改写。
2. **确认歧义**：拼写可疑、或一词多义导致词性/释义无法确定时，**先查一遍词典程序**；仍有疑问就
   问用户，不要靠猜。明显的笔误要在动手前告知用户。
3. **生成 / 补全词语信息**（两种方式都允许，按情况选）：
   * **借助程序与词典 API**：`python tools\add_words.py` 会把 `inbox.txt` 里的新词增量合并进
     `data/words.json`（内部调用 FreeDictionaryAPI / Wiktionary / Datamuse / Tatoeba，可选 LLM；
     可用 `--dry-run`、`--skip-api`、`--skip-llm`）；也可以单独调
     `python diction_lookup\dictionary_lookup.py <word>` 拿 `pos`、英文释义、中文翻译、同义词、
     例句（含来源与许可证）。
   * **派 subagent 或自行生成**：让 subagent 收集 `pos` / `english_definitions` / `chinese` /
     `synonyms` / `example`，或由当前 agent 结合已有信息直接写好。硬性要求：
     `pos` 只能取 schema 枚举值；`english_definitions` 是 `{pos, definition}` 列表；
     `synonyms` 3–8 个、宁缺毋滥；`example` 是 `{sentence, translation}`（翻译可留空）；
     内容必须与该词的真实含义一致。
4. **写进正确的位置**：
   * 新词 → 追加到 `inbox.txt`（`term | 中文笔记`），再按下面 a)–e) 的流程合并；
   * 已有词的内容修正 → 写进 `data/curation/*.json`（见 §3）。
5. **不要编造**：拿不准的字段宁可不写。必须留下的不确定信息用 `review_flags` 标记
   （例如 `ambiguous_term`、`api_mismatch`），`validate.py` 会把它们汇总进 review queue；
   来源与许可证字段（`sources`、例句作者）不得删除。
6. **收尾**：`curate_gre.py` → `validate.py --write` → `build.py` → 跑测试，并把本次新增/修改的
   词、字段来源、仍存疑的点一并回报给用户。

下面 a)–e) 是这条流程对应的命令行版本。

```powershell
# a) 把用户给的词写进源笔记，一行一个；格式： term | 中文笔记
#    追加到 inbox.txt 末尾（不要覆盖已有内容）
#    例： obdurate | 顽固的，固执的

# b) 把新词并进词库。二选一：
#    b1) 增量合并（推荐，只动 inbox 里的词，不碰其他条目）
python tools\add_words.py --dry-run          # 先看会新增/更新哪些词
python tools\add_words.py                    # 需要 OPENAI_API_KEY + --model/OPENAI_MODEL
python tools\add_words.py --skip-llm         # 只用词典 API，不需要 OpenAI
python tools\add_words.py --skip-api --skip-llm   # 完全离线：只做结构化，并给词打 api_mismatch 待补
#    b2) 整库重建（会按 verbalword.md 重算全部词条，任何手动改动都会被覆盖）
python tools\import_notes.py --api-mode dictionary

# c) hand-curation（可选，但推荐：Wiktionary 的同义词常常是错的）
#    在 data/curation/*.json 里为每个词补一条 { "words": { "<term>": { ... } } }
python tools\curate_gre.py

# d) 校验 + 刷新 review queue
python tools\validate.py --write

# e) 构建 + 测试
python tools\build.py
python -m unittest discover -s tests
```

注意：`import_notes.py` 会**整库重建** `words.json`，所以任何手改过的 `words.json`
内容都可能被覆盖；只想加几个词时用 `add_words.py`。要修改已有词条的内容，请写进
`data/curation/*.json`（见 §3，以及 `tools\curate_gre.py` 顶部注释）。

### 2. 删除单词（安全、正确地移除）

**只用** `tools\remove_words.py`，不要手动删 JSON 里的对象——一个词被删掉后，
其他词条的 `synonyms` / `derivatives` / `confusables`、`data/curation/*.json`、
`verbalword.md`、enrichment cache 里都还留着它：

```powershell
# a) 先看计划（dry run，默认不写任何文件）
python tools\remove_words.py obdurate

# b) 计划没问题再执行
python tools\remove_words.py obdurate --apply

# c) 需要顺带清理缓存 / 导入日志 / 源笔记（避免下次 import_notes.py 又把它加回来）
python tools\remove_words.py obdurate --apply --purge-cache --purge-raw --purge-source-notes

# d) 重建 + 测试
python tools\build.py
python -m unittest discover -s tests
```

工具会做这几件事（dry run 也会逐条打印）：

| 步骤 | 说明 |
| --- | --- |
| 定位目标 | 按 `id` / `term` / `lemma` 匹配，找不到就报错退出（`--ignore-missing` 可跳过） |
| 删除词条 | 从 `data/words.json` 移除该对象 |
| 清理悬挂引用 | 其他词条指向被删词的 synonym / derivative / confusable 会被一并移除（`--keep-refs` 保留） |
| 清理 curation | 删除 `data/curation/*.json` 中对应的词条，否则 `curate_gre.py` 会因为“未知词”直接退出 1（`--keep-curation` 保留） |
| 清理自定义 list | 从 `data/lists.json` 的自定义 list 里去掉该 id（自动 list 由 build 重建，无需处理） |
| 刷新 review queue | 调用 `validate.py`，重算 `review_flags` 与 `data/review_queue.json` |
| 报告源笔记 | 列出 `verbalword.md` / `inbox.txt` 中还提到该词的行——不清掉的话 `import_notes.py` 会把它加回来 |
| 备份 | 写盘前把 `data/words.json`（以及被改动的源笔记）复制到 `data/backups/`（`--no-backup` 关闭） |
| 安全阀 | 一次删除超过全库一半时拒绝执行，除非 `--force` |

对 agent 的建议：先跑 dry run 并把输出贴给用户确认，再执行 `--apply`。

### 3. 修改单词内容

写进 `data/curation/<批次>.json`，然后 `python tools\curate_gre.py`：

```json
{
  "words": {
    "obdurate": {
      "pos": ["adjective"],
      "chinese": ["顽固的，固执的"],
      "english_definitions": [{ "pos": "adjective", "definition": "Stubbornly refusing to change." }],
      "synonyms": ["stubborn", "obstinate"],
      "example": { "sentence": "...", "translation": "..." },
      "clear_flags": ["api_mismatch"]
    }
  }
}
```

### 4. 调整 list

* 改每段词数：编辑 `data/lists.json` 的 `chunk_size`，或 `python tools\build.py --chunk-size 50`。
* 加一个自己的 list：往 `data/lists.json` 的 `lists` 数组里加对象（见上文），然后重新 build。
* 不需要手动维护 `all` / `part-NN`，它们每次 build 都按 `words.json` 重新生成。

## 行为约定（改代码时不要破坏）

* session 进度（当前词、轮次、临时 score、卡片正反面、中文是否显示）只存在内存里，
  **不写** localStorage；刷新即归零。
* **持久化的数据只有两项**：
  * 丢弃标记 `gre-vocab:discarded:v1`（单词 id 数组）
  * 最近一次完整 session 的 score `gre-vocab:scores:v1`（`{word_id: 0..10}`，缺失 = 未练习）
* 计分：评分只改当前词的掌握状态与本次 session 的 score，**不会自动跳词**；
  同一轮内对同一个词反复改评分不重复累计，只记被选过的最高档（unknown 1 / fuzzy 2 / known 0），
  每轮结束时结算一次，score 上限 10；`known` 的词不再进入下一轮，直到全部 `known` 才结束 session。
* 丢弃标记**不改变**练习流程：被标记的词照常出现在 session 里（自动跳过是明确禁止的）。
* 交互界面文案为英文；词条内容允许中文。
* 页面不请求任何外部资源，所有数据来自内嵌的 `VOCABULARY_DATA` / `LIST_DATA`。
* 空字段不渲染空标题；脏数据不能让页面崩溃（`normalizeWord` 有 try/catch，读 localStorage 有 try/catch）。
* 同页面重绘（例如在 Lists 页选择 list、在 Export 页清空标记）必须保持当前滚动位置：
  `render()` 里会先记下 `window.scrollY`，写回 `innerHTML` 后再恢复（否则文档高度瞬间归零，
  浏览器会把滚动条夹到顶部）；只有切换路由或进入 session 才回到顶部。

## Session 交互（flashcard）

* 顶部控制行：`← Quit`、`←` / `→`（Previous / Next）、位置指示 `3 / 30`、`⊘ Discard`。
* 三个评分按钮 `Unknown` / `Fuzzy` / `Known` **从 session 一开始就可见**（没有 Show answer 按钮），
  点击只改状态与 score，**停留在当前词**；当前选中的档位有内描边高亮（`aria-pressed`）。
* 点击卡片（或桌面按 `Space`）在正面 / 反面之间翻转：正面只有单词与词性，反面是
  Chinese meaning / English definition / Synonyms / Example。翻面不改变 score 与 status。
* 反面里的 `Chinese meaning` 默认隐藏（灰色占位区域），点一下显示，再点一下隐藏；
  切换词或翻回正面后恢复默认隐藏。这个状态不写入任何存储。
* 快捷键：`Space` 翻面、`1/2/3` 评分、`←/→` 上一个 / 下一个、`D` 丢弃、`Esc` 退出。
* 可以自由 `←` 翻阅已经看过的词并重新改评分；把 `Known` 改成 `Fuzzy` / `Unknown` 的词
  会重新回到待练习集合（下一轮继续出现）。

## Score 的生命周期

1. 每次新 session 的临时 score 一律从 `0` 开始，不读 localStorage 里的旧值。
2. 评分时只更新“当前词本次 session 的状态 + 本轮最高档分值”，不做加法累加。
3. 每轮结束时把本轮的分值结算进 `session.score`（上限 10）。
4. **只有正常完成 session**（所有词都 `known`）才把最终 score 合并写入
   `gre-vocab:scores:v1`；中途 `退出` 不写入、不覆盖旧值。
5. 从未完成过完整 session 的词视为 `-1`（unpracticed），不参与统计。

## Export 标签页

与 Garden / Bank / Lists 同级的主标签页，集中管理长期状态：

* **Statistics**：极简 SVG donut，按 `0 / 1-3 / 4-6 / 7-9 / 10` 五档统计当前 build 的全部词汇；
  `-1`（未练习）不进入图表，只在图外显示 `Unpracticed: N words`。
* **Score-range copy**：五档各自一个 `Copy`，复制纯文本、一行一个词、不带分数（空档位按钮禁用）。
* **Discarded words**：丢弃词的 chip 列表 + `Copy discarded words` + `Clear marks`
  （Lists 页不再重复放这个管理界面）。

Result 页仍然只表示“刚刚完成的这一次 session”，不读取 localStorage 里的旧 score。

## 丢弃标记与导出

背词过程中遇到“不想再学”的词，可以就地标记丢弃，之后再一键导出：

* **标记位置**：session 顶部 `⊘ Discard` 按钮（快捷键 `D`，仅桌面）、词条详情页头部同一个按钮。
* **状态**：保存在浏览器 `localStorage`（键 `gre-vocab:discarded:v1`），刷新不丢；
  Bank 列表里被标记的词显示一个 `⊘`；localStorage 不可用时自动退化为“仅本次会话有效”并提示。
* **导出**：Export 页的 `Discarded words` 面板列出全部被标记的词，
  `Copy discarded words` 一键复制纯文本（一行一个词），`Clear marks` 清空标记。
* **后续处理**（可选）：把复制出来的词交给删除流程，例如
  `python tools\remove_words.py abate specious --apply`，再 `python tools\build.py` 重建。

## 调试入口

浏览器控制台里可用 `window.GRE_DEBUG`（只读，不参与页面逻辑）：
`GRE_DEBUG.state()`、`GRE_DEBUG.current()`、`GRE_DEBUG.startIds([...])`、`GRE_DEBUG.flip()`、
`GRE_DEBUG.showChinese()`、`GRE_DEBUG.next()`、`GRE_DEBUG.previous()`、
`GRE_DEBUG.rate('unknown'|'vague'|'known')`、`GRE_DEBUG.exit()`、`GRE_DEBUG.discarded()`、
`GRE_DEBUG.toggleDiscard(id)`、`GRE_DEBUG.clearDiscarded()`、`GRE_DEBUG.scores()`、
`GRE_DEBUG.stats()`、`GRE_DEBUG.bucketTerms(id)`。
