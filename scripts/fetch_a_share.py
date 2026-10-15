#!/usr/bin/env python3
"""A 股三表与公司资料取数（FR-14 / FR-15）。

主源：新浪财经三表（合并报表口径，年报 1231 序列）；校验源：东财 F10（B01/B17/C02 交叉）；
公司资料：东财 push2（行业/简称/上市日期/股本市值）。

用法：python3 scripts/fetch_a_share.py 600519 [--years 5] [--out receipt.json]

设计依据：docs/TDD-v1.0.0-datasource.md §3。合规声明：代码自研；接口为公开事实信息。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    ArgError, FetchError, add_conflict, add_error, add_item, add_missing,
    get_json, limited_get, run_cli, sina_report,
)

SCRIPT = os.path.basename(__file__)

# ---------------------------------------------------------------- 代码规范化（TDD §3.1）

def normalize_a_code(raw):
    """接受 600519 / SH600519 / 600519.SH 等形态 → 纯 6 位代码。

    解析失败抛 ArgError（exit 3），绝不静默返回空串。
    北交所老号段（43/83/87/8 开头）明确报错提示自备数据（FR-14-4）。
    """
    text = raw.strip().upper()
    m = re.fullmatch(r"(?:SH|SZ|BJ)?(\d{6})(?:\.(?:SH|SZ|BJ))?", text)
    if not m:
        raise ArgError(f"无法解析的 A 股代码: {raw!r}（示例：600519 / SH600519 / 600519.SH）")
    code = m.group(1)
    if code.startswith(("43", "83", "87")) or code.startswith("8"):
        raise ArgError(
            f"北交所老号段 {code} 数据源存在僵尸数据风险，内置源不支持；"
            "请改用 templates/ 手工自备数据（见 references/data-source-spec.md）")
    return code


def em_secid(code6):
    if code6.startswith("6"):
        return f"1.{code6}"
    if code6.startswith(("0", "3")):
        return f"0.{code6}"
    return None  # 北交所：push2 实测不稳定（TDD §0 M10），公司资料降级为 MISSING


def em_f10_code(code6):
    if code6.startswith("6"):
        return "SH" + code6
    if code6.startswith("920"):
        return "BJ" + code6
    return "SZ" + code6


# ---------------------------------------------------------------- 字段映射总表（TDD §3.3，实测科目名）

FIELD_MAP_FZB = {
    "A01": "货币资金", "A02": "交易性金融资产", "A03": "应收票据", "A04": "应收账款",
    "A05": "应收款项融资", "A06": "预付款项", "A07": "其他应收款(合计)", "A08": "存货",
    "A09": "合同资产", "A10": "流动资产合计", "A11": "长期股权投资", "A12": "其他非流动金融资产",
    "A13": "投资性房地产", "A14": "固定资产净额", "A15": "在建工程合计", "A16": "使用权资产",
    "A17": "无形资产", "A18": "开发支出", "A19": "商誉", "A20": "长期待摊费用",
    "A21": "递延所得税资产", "A22": "资产总计", "A23": "短期借款", "A24": "应付票据",
    "A25": "应付账款", "A26": "合同负债", "A27": "一年内到期的非流动负债", "A28": "流动负债合计",
    "A29": "长期借款", "A30": "应付债券", "A31": "租赁负债", "A32": "递延所得税负债",
    "A33": "负债合计", "A34": "归属于母公司股东权益合计", "A35": "少数股东权益",
}

FIELD_MAP_LRB = {
    "B01": "营业收入", "B02": "营业成本", "B03": "销售费用", "B04": "管理费用",
    "B05": "研发费用", "B06": "财务费用", "B06a": "利息费用", "B06b": "利息收入",
    "B07": "其他收益", "B08": "投资收益", "B09": "公允价值变动收益", "B10": "资产处置收益",
    "B11": "营业利润", "B12": "营业外收入", "B13": "营业外支出", "B14": "利润总额",
    "B15": "所得税费用", "B16": "净利润", "B17": "归属于母公司所有者的净利润",
    "B18": "少数股东损益", "B19": "其他综合收益", "B20": "综合收益总额",
}

FIELD_MAP_LLB = {
    "C01": "销售商品、提供劳务收到的现金", "C02": "经营活动产生的现金流量净额",
    "C03": "购建固定资产、无形资产和其他长期资产所支付的现金", "C04": "投资活动产生的现金流量净额",
    "C05": "分配股利、利润或偿付利息所支付的现金", "C06": "筹资活动产生的现金流量净额",
    "C07": "现金及现金等价物净增加额",
}

# 附注修饰信息三表不可得（TDD §3.3 映射规则），由 D 组公告路径补充
ATTACHMENT_ONLY = {
    "A03": "银行/商业承兑拆分见年报附注（D 组路径）",
    "A06": "账龄结构见年报附注（D 组路径）",
    "A13": "计量模式见年报附注（D 组路径）",
}

# ---------------------------------------------------------------- 东财 F10 交叉校验（TDD §3.4，列名 2026-10-15 冻结）

EM_F10_BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
EM_CHECKS = {
    "B01": ("lrbAjaxNew", "OPERATE_INCOME"),
    "B17": ("lrbAjaxNew", "PARENT_NETPROFIT"),
    "C02": ("xjllbAjaxNew", "NETCASH_OPERATE"),
}
CONFLICT_THRESHOLD = 0.005
EM_F10_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/"}


def em_f10_cross_check(env, code6, sina_values):
    """sina_values: {(field_id, period): value}。东财缺失仅 warnings 不阻塞。"""
    periods = sorted({p for (_, p) in sina_values}, reverse=True)
    if not periods:
        return
    dates = ",".join(periods)
    # 按 endpoint 聚合拉取（B01/B17 同属 lrbAjaxNew，避免重复请求）
    cache = {}  # ep -> {period: row}；拉取失败为 None
    for ep in sorted({ep for ep, _ in EM_CHECKS.values()}):
        try:
            d = get_json(EM_F10_BASE + ep, endpoint=f"em_f10_{ep}", params={
                "companyType": "4", "reportDateType": "0", "reportType": "1",
                "dates": dates, "code": em_f10_code(code6),
            }, headers=EM_F10_HEADERS)
        except FetchError as exc:
            add_error(env, exc.endpoint, f"校验源不可用: {exc}", exc.http_status,
                      "新浪主源数据照常有效；校验源缺失不阻塞")
            env["meta"]["warnings"].append(f"东财 F10 {ep} 校验源缺失，对应字段交叉校验未执行")
            cache[ep] = None
            continue
        rows = d.get("data") or []
        if not rows:
            env["meta"]["warnings"].append(f"东财 F10 {ep} 返回空，对应字段交叉校验未执行")
        cache[ep] = {str(r.get("REPORT_DATE", ""))[:10]: r for r in rows}
    for field_id, (ep, column) in EM_CHECKS.items():
        by_period = cache.get(ep)
        if not by_period:
            continue
        for (fid, period), sina_val in sina_values.items():
            if fid != field_id or sina_val is None:
                continue
            row = by_period.get(period)
            em_val = row.get(column) if row else None
            if em_val is None:
                continue  # 东财侧该期缺失：不阻塞（TDD §3.4）
            em_val = float(em_val)
            denom = max(abs(sina_val), abs(em_val))
            if denom and abs(sina_val - em_val) / denom > CONFLICT_THRESHOLD:
                add_conflict(env, field_id, period, "sina", sina_val, "em_f10", em_val)
                for item in env["items"]:
                    if item["field_id"] == field_id and item["period"] == period:
                        item["status"] = "CONFLICT"
                        item["note"] = "新浪/东财双源偏差 >0.5%，双值见 conflicts；不仲裁任选"


# ---------------------------------------------------------------- 公司资料（TDD §3.5，主备双源）

PUSH2_URL = "https://push2.eastmoney.com/api/qt/stock/get"
PUSH2_HEADERS = {"Referer": "https://quote.eastmoney.com/"}
# 备选源（2026-10-15 执行期修订：push2 存在住宅 IP 持续性风控先例，TC-DS-01 实测触发）
SURVEY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
SURVEY_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/"}


def _profile_items(env, source, name, industry, industry_field, listed,
                   total_shares=None, total_mv=None):
    if name:
        add_item(env, "G0-06", name, "", source, "证券简称",
                 unit="text", currency="", note="ST/*ST 状态由 Agent 按简称前缀裁决")
    else:
        add_missing(env, "G0-06", "", source, "证券简称", "字段未返回", unit="text", currency="")
    if industry:
        add_item(env, "G0-05", industry, "", source, industry_field,
                 unit="text", currency="", note="东财行业口径（FR-15-2）")
    else:
        add_missing(env, "G0-05", "", source, industry_field, "字段未返回", unit="text", currency="")
    if listed:
        add_item(env, "D09", listed, "", source, "上市日期", unit="date", currency="")
    else:
        add_missing(env, "D09", "", source, "上市日期", "字段未返回", unit="date", currency="")
    if total_shares:
        add_item(env, "INFO_TOTAL_SHARES", float(total_shares), "", source, "总股本",
                 unit="股", currency="")
    if total_mv:
        add_item(env, "INFO_TOTAL_MV", float(total_mv), "", source, "总市值")


def _profile_via_survey(env, code6):
    """备选源：CompanySurvey/PageAjax。覆盖三闸口字段；总股本/总市值缺失记 MISSING。"""
    d = get_json(SURVEY_URL, endpoint="em_survey", params={"code": em_f10_code(code6)},
                 headers=SURVEY_HEADERS)
    jbzl = (d.get("jbzl") or [{}])[0]
    fxxg = (d.get("fxxg") or [{}])[0]
    listed_raw = str(fxxg.get("LISTING_DATE") or "")[:10] or None
    _profile_items(env, "em_survey",
                   name=jbzl.get("SECURITY_NAME_ABBR"),
                   industry=jbzl.get("EM2016"),
                   industry_field="EM2016 东财行业（2016 版三级路径）",
                   listed=listed_raw)
    add_missing(env, "INFO_TOTAL_SHARES", "", "em_survey", "总股本",
                "备选源不含总股本；待 push2 恢复或手工补充", unit="股", currency="")
    add_missing(env, "INFO_TOTAL_MV", "", "em_survey", "总市值",
                "备选源不含总市值；待 push2 恢复或手工补充")
    env["meta"]["warnings"].append("公司资料来自备选源 CompanySurvey（push2 风控降级）")


def fetch_profile(env, code6):
    secid = em_secid(code6)
    if secid is None:
        env["meta"]["warnings"].append(
            "北交所标的：公司资料接口实测不稳定，D09/行业/简称以 MISSING 输出，请自备")
        for fid, raw in (("D09", "上市日期"), ("G0-05", "行业分类"), ("G0-06", "证券简称")):
            add_missing(env, fid, "", "em_push2", raw, "北交所标的公司资料接口不可用", unit="text", currency="")
        return
    try:
        d = get_json(PUSH2_URL, endpoint="em_push2", params={
            "secid": secid, "fields": "f57,f58,f84,f116,f127,f128,f189",
        }, headers=PUSH2_HEADERS)
        data = d.get("data")
        if not isinstance(data, dict):
            raise FetchError("em_push2", "返回结构漂移：data 缺失",
                             action_hint="接口结构可能已变更")
        listed = None
        raw189 = data.get("f189")
        if isinstance(raw189, (int, float)) and raw189:
            s = str(int(raw189))
            listed = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        _profile_items(env, "em_push2",
                       name=data.get("f58"), industry=data.get("f127"),
                       industry_field="f127 行业分类", listed=listed,
                       total_shares=data.get("f84"), total_mv=data.get("f116"))
        return
    except FetchError as exc:
        add_error(env, exc.endpoint, f"push2 不可用，切换备选源: {exc}", exc.http_status,
                  exc.action_hint)
    try:
        _profile_via_survey(env, code6)
    except FetchError as exc:
        add_error(env, exc.endpoint, f"备选源也不可用: {exc}", exc.http_status,
                  exc.action_hint or "三表数据照常有效；公司资料请手工补充")
        for fid, raw, unit in (("D09", "上市日期", "date"), ("G0-05", "行业分类", "text"),
                               ("G0-06", "证券简称", "text")):
            add_missing(env, fid, "", "em_survey", raw, "双源均不可用", unit=unit, currency="")


# ---------------------------------------------------------------- 主流程

def fill_statements(env, code6, years):
    sina_values = {}
    coverage = {}
    for source, fmap, group in (("fzb", FIELD_MAP_FZB, "A"),
                                ("lrb", FIELD_MAP_LRB, "B"),
                                ("llb", FIELD_MAP_LLB, "C")):
        table, warning = sina_report(code6, source, years=years)
        if warning and warning not in env["meta"]["warnings"]:
            env["meta"]["warnings"].append(warning)
        if not table:
            add_error(env, f"sina_{source}", "无任何年报期返回，标的可能不存在或接口异常",
                      action_hint="请核实代码是否正确；若为新股请改用 templates/ 手工自备")
        hit = 0
        for period, rec in table.items():
            for field_id, title in fmap.items():
                val = rec.get(title)
                if val is not None:
                    hit += 1
                    note = ATTACHMENT_ONLY.get(field_id, "")
                    add_item(env, field_id, val, period, f"sina_{source}", title, note=note)
                else:
                    note = ATTACHMENT_ONLY.get(field_id, "科目未披露")
                    add_missing(env, field_id, period, f"sina_{source}", title, note)
                sina_values[(field_id, period)] = val
        total = len(fmap) * len(table)
        coverage[group] = f"{hit}/{total}"
    env["meta"]["field_coverage"] = coverage
    return sina_values


def main(ticker, years, env):
    code6 = normalize_a_code(ticker)
    env["ticker"] = code6
    if code6.startswith("920"):
        env["meta"]["warnings"].append("北交所新号段（920）：有限支持，数据完整性请以公告核验")
    sina_values = fill_statements(env, code6, years)
    em_f10_cross_check(env, code6, sina_values)
    fetch_profile(env, code6)


if __name__ == "__main__":
    run_cli(SCRIPT, "A", main, "A 股代码（如 600519 / SH600519 / 600519.SH）")
