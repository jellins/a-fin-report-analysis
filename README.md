# a-share-fin-report-analysis

![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg) ![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)

> Turn any AI agent into a disciplined financial-report detective: a fixed-methodology, plug-in-data-source Skill that runs a seven-layer diagnostic on A-share listed companies. It red-flags fraud and blow-up risks, identifies genuinely good businesses, and outputs a four-tier quality rating (优质候选 / 可关注 / 高风险回避 / 直接排除) with a full evidence chain and source receipts. Methodology is locked; data sources are pluggable (built-in scripts fetch A-share/HK/US data out of the box, or bring your own CSV/JSON data). Not applicable to banks, insurers, brokers and other financial companies.

> 让任何 AI Agent 都成为纪律严明的财报侦探——A 股上市公司财报诊断与排雷 Skill

一套可被主流 AI Agent（Claude Code、WorkBuddy、Cursor 等）直接加载使用的财报诊断能力包：用**固定的分析框架**和**锁死的指标计算方式**，对 A 股上市公司执行七层系统化诊断，排除财务造假与业绩暴雷风险，识别真正的好生意。

## 为什么做这个 Skill

让 LLM 直接分析一家公司的财报，有三个系统性问题：

1. **分析发散**：每次分析的维度、指标、顺序都不一样，结论不可复现
2. **口径漂移**：同一家公司，这次用母公司报表、下次用合并报表；这次用净利润、下次用归母净利润——指标口径不一致导致结论互相矛盾
3. **数值幻觉**：取不到的数据靠"合理估计"补齐，看似专业的分析建立在虚构数字上

这个 Skill 的设计目标就是把这三个问题一次性解决：**方法论不可替换，数据源可以替换**。分析框架、指标公式、裁决规则全部固化在文件里，Agent 的角色被严格限定为「执行固定流程 + 按固定规则裁决」，而不是自由发挥做财务分析。

## 思想来源

方法论框架蒸馏自唐朝《手把手教你读财报（新准则升级版，2021）》——A 股价值投资领域公认最系统的财报入门与排雷方法论，其核心立场贯穿整个 Skill：

- **财报是排雷工具，不是选股神器**：先排除坏公司，再识别好生意
- **利润是算出来的估计值，现金才是真金白银**：一切利润必须经过现金流验证
- **顺着资金流向读财报**：钱从哪来 → 钱花到哪 → 钱赚得真不真
- **任何指标只有在自身 5 年趋势与同行对比中才有意义**

本 Skill 为**框架与思想的蒸馏重构，不含原书原文复述**。诊断清单经过「财务分析专家评审 + 独立 Agent 复审」双轮打磨，并按 2021 新准则口径做了关键修正（如：新准则下政府补助与资产处置收益已进入营业利润，传统的"营业利润占比"口径已失效）。**本项目与原书作者无任何关联，未获其授权或背书。**

## 架构设计：三层锁定

```
方法论层（不可变）   七层诊断框架 + 指标字典（公式/口径/裁决规则全部写死）
      │ 反推
契约层（固定格式）   数据需求清单（A–E 五组原始字段，数据源只需"填空"）
      │ 路由
数据源层（可插拔）   内置源（scripts/ 自研脚本：新浪/东财/巨潮/SEC EDGAR，开箱即用）
                    自定义源（用户声明 → 准入校验 → 优先采用）；手工自备（templates/ 兜底）
      │ 产出
回执层（强制）       每个数值必须携带 {值, 报告期, 来源, 原始字段, 获取时间}
```

- **指标字典**（`references/indicator-dictionary.md`）：48 项指标，每个有唯一 ID、定义、公式、口径、参照线、裁决规则与红灯分级；四档评级的聚合规则同样写死在字典里。Agent 只能查字典执行，禁止自创指标、禁止自由裁量聚合
- **数据回执**：无来源标注的数值禁止进入诊断结论；取不到的字段标注 `MISSING`，禁止估算编造——这是防幻觉的硬性约束
- **可插拔数据源**：用户未声明时走内置源（scripts/ 自研脚本，覆盖 A 股三表/治理数据/公告检索/港美股对照组）；用户声明自己的数据源（Excel/Wind/iFinD/内部 API 均可）时优先使用，但必须通过 C1–C5 准入校验（能力覆盖、口径合规、字段完整性、可溯源、样本抽验）

## 七层诊断框架

| 层 | 内容 | 回答的问题 |
|---|---|---|
| 第 0 层 | 前置闸口（行业适用性/风险警示/审计意见/合并报表/上市年限/数据完备性） | 这份财报值不值得读 |
| 第 1 层 | 安全性（现金覆盖有息负债、有息负债率、存贷双高） | 这家公司能不能活 |
| 第 2 层 | 资产质量（应收/存货/商誉/在建工程等 16 项） | 家底真不真 |
| 第 3 层 | 盈利质量（毛利率/三费率/主业成色/少数股东损益） | 赚的是不是真钱 |
| 第 4 层 | 现金验证（营收含金量/利润含金量/现金流肖像 8 类） | 利润有没有人买单 |
| 第 5 层 | 管理层与治理（质押/减持/换所/关联交易/或有事项） | 人靠不靠谱 |
| 第 6 层 | 综合判定（ROE 杜邦拆解/周转率/成长性/自洽性总检） | 好生意长什么样 |

输出为四档质地评级：**优质候选 / 可关注 / 高风险回避 / 直接排除**——由指标字典「评级聚合规则」机械裁决（红灯分级制，结论可复现），附完整指标证据链与来源标注。

## 能解决什么问题，带来什么价值

- **对个人投资者**：5 分钟完成一家公司财报的纪律化初筛，避开财务造假与暴雷陷阱，把"看不懂财报"变成"按清单执行"
- **对研究者**：诊断结论可复现——同一套数据输入，任何 Agent、任何时间执行，输出同一套评级与证据链
- **对团队**：自定义数据源准入机制让内部数据平台、付费终端（Wind/iFinD）无缝接入，方法论在团队内统一
- **对 Agent 生态**：以标准 Skill 格式发布，一份文件即装即用

## 安装与使用

### 目录结构

```
a-share-fin-report-analysis/
├── SKILL.md                              # 入口：触发条件、执行流程、关键纪律
├── references/
│   ├── indicator-dictionary.md           # 指标字典 + 评级聚合规则 + 数据需求清单
│   ├── data-source-spec.md               # 数据源规范与准入机制（含同行对照组遴选规范）
│   └── diagnostic-checklist.md           # 七层诊断清单（背景与经验说明）
├── templates/                            # 自定义数据源双轨模板（CSV / JSON Schema / 接入指引）
├── scripts/                              # 内置数据源（自研，python3+requests 即可运行）
│   ├── fetch_a_share.py                  # A 股三表 + 公司资料（新浪主源 + 东财交叉校验）
│   ├── fetch_governance.py               # 治理数据（分红/股东户数/高管变动/质押粗筛）
│   ├── fetch_announce.py                 # 巨潮公告检索 + 年报 PDF 下载
│   ├── fetch_hk.py / fetch_us.py         # 港美股对照组三表（+SEC EDGAR 官方校验）
│   └── selftest.md                       # 数据源自检步骤与被封对策
├── examples/
│   └── example-report-001.md             # 虚构示例报告（安装后自检基准）
├── CHANGELOG.md                          # 版本记录（Keep a Changelog）
├── CONTRIBUTING.md                       # 贡献指南
└── LICENSE
```

### 安装

环境要求：`python3 ≥ 3.8` + `pip3 install requests`（内置数据源脚本的唯一依赖；不使用内置源时可不装）。

将 `a-share-fin-report-analysis` 目录复制到你的 Agent 的 skills 目录：

| 平台 | 安装路径 |
|---|---|
| Claude Code | `~/.claude/skills/a-share-fin-report-analysis/` |
| WorkBuddy | `~/.workbuddy/skills/a-share-fin-report-analysis/` |
| 其他兼容 Agent Skills 协议的平台 | 按各平台 skills 目录约定放置 |

### 使用

无需显式调用，用自然语言描述需求即可触发：

- 「帮我分析一下贵州茅台的财务质地」
- 「用这份我自己整理的 Excel 数据诊断一下 XX 公司」（触发自定义数据源准入校验）
- 「对比宁德时代和比亚迪的财报，哪家质地更好」
- 「这家公司的商誉有没有暴雷风险」

### 安装后自检

- **流程自检**：以 `examples/example-report-001.md` 附录中的虚构数据为输入，让 Agent 执行完整诊断：应得到与该示例**同结构、同评级（高风险回避）**的输出。若结构或评级不一致，说明 Skill 未完整加载或执行环境未按流程执行。
- **数据源自检**：`python3 scripts/fetch_a_share.py 600519`（exit 0 且回执含 ≥5 个年报期即通过）；完整用例索引与被封对策见 `scripts/selftest.md`。

### 数据来源说明

本 Skill 内置自研数据脚本（`scripts/`），开箱即用、无需安装任何外部数据 Skill：A 股三表与公司资料（新浪主源 + 东财交叉校验）、治理数据（东财）、公告检索与年报 PDF 下载（巨潮）、港美股对照组三表（东财 + SEC EDGAR 官方校验）。合规分级与失败处置见 `references/data-source-spec.md`。

**合规自证**：全部代码自研，未复制 a-stock-data / global-stock-data 的代码；接口 URL/参数为公开事实信息，接口调研参考致谢见文末。本项目只分发代码与方法，不分发数据。

也可以完全不用内置源——按 `templates/` 双轨模板（CSV 长表或 JSON Schema）整理你自己的数据，经 C1–C5 准入校验后优先使用，详见 `references/data-source-spec.md` 与 `templates/README.md`。

## 合规与免责声明

- **不分发数据**：本项目只提供方法论、指标定义与数据源调用指引，不分发、不转售任何市场数据；数据由使用者按各数据源条款自行获取并承担合规责任
- **非投资建议**：本 Skill 输出的是企业质地判断（排雷 + 识好），不构成任何证券买卖建议；估值与买卖时点不在其范围内。历史数据不代表未来表现，任何依据本 Skill 输出作出的投资决策由使用者独立承担责任
- **思想致敬**：方法论框架蒸馏自唐朝《手把手教你读财报》，感谢作者对 A 股投资者教育的贡献；本项目不含原书原文内容，强烈建议购买原书系统学习
- **阈值说明**：清单中标注〔经验值〕的阈值为快速初筛的代理指标，非绝对红线；最终裁决必须结合行业特性与公司具体情况

## License

[Apache-2.0](LICENSE) © 2026 Lucid Liu (思辨致知)

## 致谢

- 唐朝（老唐）——财报排雷方法论的思想源头
- [simonlin1212](https://github.com/simonlin1212)——开源项目 a-stock-data / global-stock-data 的作者；本项目内置数据源的公开接口调研参考了这两个项目（仅接口规律参考，未使用其代码）
