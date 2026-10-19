# Golf Knowledge Base / 高尔夫知识库

这是一个来源可追溯、可增量更新、支持中英双语检索的本地高尔夫知识库。首版聚焦四个可安全落地的领域：

- `rules`：R&A / USGA 规则、澄清与本地规则
- `handicap`：World Handicap System / USGA 差点规则
- `equipment`：器材规则与符合规则的高尔夫球清单
- `biomechanics`：开放获取的挥杆运动学、地面反作用力与压力中心综述

系统不会把所有网页都复制进向量库。每条知识都保留来源、版本、发布日期/抓取时间、许可证说明、权威等级和页码。商业数据、厂商内容与社区观点只登记链接，不默认抓取或再分发。

## 目录

```text
01_rules/                     官方规则 PDF
02_handicap/                  差点规则 PDF
03_equipment/                 器材规则与合规清单 PDF
04_biomechanics_open_access/  CC BY 4.0 论文 PDF
05_taxonomy/                  中英双语概念与关系
06_schema/                    JSON-LD 领域本体
07_docs/                      架构说明与研究报告
08_tools/                     下载、构建、检索工具
generated_corpus/             chunks.jsonl 与 golf_kb.sqlite
SOURCE_MANIFEST.csv           可下载与实时来源清单
```

## 一键构建

推荐使用 Codex 自带的 Python（已经包含 `pypdf`）：

```bash
cd golf_kb_primary_source_pack
/Users/kaijunlin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 08_tools/refresh.py
```

普通 Python 也可以：

```bash
python3 -m pip install -r requirements.txt
python3 08_tools/refresh.py
```

`refresh.py` 会下载官方 PDF、校验文件头与 SHA-256，然后生成：

- `generated_corpus/chunks.jsonl`：可导入任意 RAG / 向量数据库的标准语料
- `generated_corpus/golf_kb.sqlite`：本地 SQLite FTS5 全文知识库
- `08_tools/download_report.json`：可审计的下载结果
- `generated_corpus/build_report.json`：页数、分块数与失败详情

如果暂时不下载 PDF，也可以先构建研究报告和术语库：

```bash
python3 08_tools/build_corpus.py
```

## 检索

```bash
python3 08_tools/search_kb.py "罚杆区如何处理" --limit 5
python3 08_tools/search_kb.py "ground reaction force clubhead speed" --category biomechanics
python3 08_tools/search_kb.py "动态杆面倾角"
```

中文术语会通过 `05_taxonomy/concepts.csv` 自动扩展到英文规范词。例如“攻角”会扩展为 `attack angle`，再检索英文原始文档。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

## 使用边界

- 规则问题以 R&A / USGA 最新版本为最高优先级；本库不是法律或裁判意见。
- `Conforming Golf Balls` 是动态月度清单，生产使用前应刷新。
- 官方可下载不等于可自由再分发。版权与许可说明保存在每个 chunk 的元数据中。
- 两篇 PMC 论文为 CC BY 4.0；再使用时需保留作者、题名、来源和许可证署名。
- OSM、Wikidata、PGA TOUR、Data Golf、TrackMan 等当前作为实时/授权来源登记，未在首版镜像其内容。

当前源版本与核验日期：2026-09-20。
