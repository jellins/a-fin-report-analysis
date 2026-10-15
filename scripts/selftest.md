# 数据源自检（selftest）

> 用途：安装后验证内置数据源在本机网络环境下可用；接口故障时按本文档定位处置。
> 流程自检（虚构数据端到端走查）请用 `examples/example-report-001.md`——两者职责不同（FR-22-2）。

## 0. 环境检查

```bash
python3 --version          # 要求 ≥ 3.8
python3 -c "import requests"   # 无报错即依赖就绪；缺失则 pip3 install requests
```

## 1. 冒烟测试（TC-DS-01，必做）

```bash
python3 scripts/fetch_a_share.py 600519
```

预期：`exit code = 0`；stdout 为 JSON 回执，结构要点：

- `items` 非空，`status` 以 `OK` 为主；`period` 覆盖最近 ≥5 个年报期（`20XX-12-31`）
- 每个 item 含五要素：`field_id / value / period / source / raw_field`（另含 `retrieved_at / unit / currency / status / note`）
- `meta.field_coverage` 形如 `{"FZB": "xx/175", ...}`；`errors` 为空

> 若 `errors` 非空或退出码非 0，按第 3 节失败语义处置。
> 注意：茅台无商誉/短期借款/长期借款等科目，对应字段 `status=MISSING`、`note=科目未披露`——这是**数据真相**，不是故障。

## 2. 全量用例索引（TC-DS-01~10）

| TC | 命令 / 操作 | 通过判据 |
|---|---|---|
| TC-DS-01 | `python3 scripts/fetch_a_share.py 600519` | 见第 1 节 |
| TC-DS-02 | 同上，观察输出 | `conflicts` 数组为空（两源一致）或含完整双值记录（field_id/period/双源值/rel_diff）；不允许只有单值无任何记录 |
| TC-DS-03 | `python3 scripts/fetch_a_share.py <次新股代码>` | 如实返回已有期 + `meta.warnings` 期数不足提示，exit 0；未披露科目 `status=MISSING, value=null` |
| TC-DS-04 | `python3 scripts/fetch_a_share.py 999999`（不存在代码）及断网执行 | 数据不存在：exit 0 + warnings 注明；断网：exit≠0 + stderr 降级提示 + `errors` 非空（禁止空 items 冒充） |
| TC-DS-05 | `python3 scripts/fetch_governance.py 600519` | D13 分红（unit=CNY/10股）、股东户数、高管变动、质押粗筛、D14 推导值均输出，note 口径标注齐全 |
| TC-DS-06 | `python3 scripts/fetch_announce.py 601318 --preset yearly --download` | 公告清单非空；PDF 落盘 `./announcements/601318/` 且通过 %PDF 头校验 |
| TC-DS-07 | `python3 scripts/fetch_hk.py 00700.HK` | ≥5 期年报；`meta.accounting_standards` 与 item `currency` 透传 |
| TC-DS-08 | `python3 scripts/fetch_us.py AAPL.O`（无 SEC_CONTACT）；`SEC_CONTACT=<邮箱> python3 scripts/fetch_us.py AAPL.O` | 前者：stderr 明确提示跳过 + warnings 注明，exit 0；后者：B01/B16/C02 item note 出现「EDGAR 交叉一致」或 conflicts 双值记录 |
| TC-DS-09 | 连续 5 次请求观察耗时（或抓日志） | 同域相邻请求间隔 ≥1.0s；静态检查：`grep -n "requests\.\(get\|post\)" scripts/*.py` 仅允许命中 `ds_common.py` 的 `_request` 封装 |
| TC-DS-10 | `python3 scripts/fetch_a_share.py 920001`；`python3 scripts/fetch_a_share.py 832982` | 920 号段：返回数据且 warnings 含「有限支持」；老号段：exit 3 且 stderr 提示自备数据 |

## 3. 失败语义速查（处置以退出码为准）

| 退出码 | 语义 | 处置 |
|---|---|---|
| 0 | 成功（允许 items 内含 MISSING/CONFLICT） | 正常使用 |
| 3 | 参数错误（代码格式非法、不支持的号段） | 核对代码格式；北交所老号段（43/83/87/8 开头）请按 `templates/` 自备数据 |
| 4 | 接口故障（连接/超时/5xx 重试耗尽、返回结构漂移） | 查看 `errors[].action_hint`；稍后重试；持续失败 → 改用自定义源或手工自备 |
| 5 | 风控（HTTP 403） | 见第 4 节被封对策 |

## 4. 东财/新浪被封对策

接口为面向公众的免费页面接口，住宅 IP 高频访问可能触发风控（403 或连接中断）：

1. **降频**：脚本内置同域 ≥1s + 随机抖动限流，勿并发调用多脚本
2. **换网络**：切换手机热点或出口 IP 后重试
3. **稍后重试**：风控通常为临时性，间隔 30 分钟以上再试
4. **降级**：公司资料已内置主备双源（push2 → CompanySurvey）；三表仍失败 → 按 `templates/` 模板手工自备数据走自定义源准入（spec 第 3 节）

## 5. 结果记录模板

```
日期 / 网络环境：
TC-DS-01：exit=__，coverage=__，errors=__
其他 TC（按需）：
结论：□ 可用  □ 部分可用（说明）  □ 不可用（已切换自备数据）
```
