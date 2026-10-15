#!/usr/bin/env python3
"""港股对照组三表取数（FR-18）；同时承载港美股共享解析逻辑（供 fetch_us.py 复用）。

主源：东财港股 F10 三表（长表 ITEM_NAME/AMOUNT，年报按 REPORT_TYPE=="年报" 过滤）。
对照组字段映射：A/B/C 组按实测科目对照（2026-10-15 冻结，见 TDD §6.1 注记）；
缺口记 MISSING 不阻塞（国际准则科目体系差异属预期）。

用法：python3 scripts/fetch_hk.py 00700.HK [--years 5] [--out receipt.json]

设计依据：docs/TDD-v1.0.0-datasource.md §6.1。合规声明：代码自研；接口为公开事实信息。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    ArgError, FetchError, add_error, add_item, add_missing, get_json, run_cli,
)

SCRIPT = os.path.basename(__file__)
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"Referer": "https://data.eastmoney.com/"}

CURRENCY_MAP = {"人民币": "CNY", "美元": "USD", "港币": "HKD"}

# ---------------------------------------------------------------- 港股字段映射（2026-10-15 冻结）
# 值形态：[候选科目名链] 取首个非空；("sum"|"sub", [成分科目]) 为推导（note 注明推导式）；
# None 表示该准则口径下不可得（MISSING，不阻塞）。

FIELD_MAP_HK_FZB = {
    "A01": ["现金及等价物"],
    "A02": ["指定以公允价值记账之金融资产(流动)"],
    "A03": None,  # 港股无应收票据分类
    "A04": ["应收帐款"],
    "A05": None,
    "A06": ["预付款按金及其他应收款"],  # 合并口径
    "A07": None,  # 已并入预付款按金及其他应收款
    "A08": ["存货"],
    "A09": None,
    "A10": ["流动资产合计"],
    "A11": ("sum", ["联营公司权益", "合营公司权益"]),
    "A12": ["其他金融资产(非流动)"],
    "A13": ["投资物业"],
    "A14": ["物业厂房及设备"],
    "A15": ["在建工程"],
    "A16": None,  # 使用权资产未单列
    "A17": ["无形资产"],
    "A18": None,
    "A19": None,  # 商誉未单列（已知缺口，须查年报附注）
    "A20": None,
    "A21": ["递延税项资产"],
    "A22": ["总资产"],
    "A23": ["短期贷款"],
    "A24": ["应付票据"],
    "A25": ["应付帐款"],
    "A26": ["递延收入(流动)"],  # 近似合同负债口径
    "A27": None,
    "A28": ["流动负债合计"],
    "A29": ["长期贷款"],
    "A30": None,  # 应付债券未见单列
    "A31": ("sum", ["融资租赁负债(流动)", "融资租赁负债(非流动)"]),
    "A32": ["递延税项负债"],
    "A33": ["总负债"],
    "A34": ["股东权益"],  # 归母口径（总权益=股东权益+少数股东权益）
    "A35": ["少数股东权益"],
}

FIELD_MAP_HK_LRB = {
    "B01": ["营业额", "营运收入"],
    "B02": ("sub", ["营业额", "毛利"]),  # 营业成本=营业额−毛利（推导）
    "B03": ["销售及分销费用"],
    "B04": ["行政开支"],
    "B05": None,  # 港股研发费用未单列（见年报）
    "B06": ["融资成本"],  # 近似财务费用口径
    "B06a": None,  # 融资成本主要含利息，但非纯利息费用
    "B06b": ["利息收入"],
    "B07": ["其他收益"],
    "B08": None,  # 应占联营公司溢利≠投资收益
    "B09": None,
    "B10": None,
    "B11": ["经营溢利"],
    "B12": None,  # 国际准则无营业外收支分类
    "B13": None,
    "B14": ["除税前溢利"],
    "B15": ["税项"],
    "B16": ["除税后溢利"],
    "B17": ["股东应占溢利"],
    "B18": ["少数股东损益"],
    "B19": ["其他全面收益"],
    "B20": ["全面收益总额"],
}

FIELD_MAP_HK_LLB = {
    "C01": None,  # 间接法报表无销售收现行
    "C02": ["经营业务现金净额"],
    "C03": ("sum", ["购建固定资产", "购建无形资产及其他资产"]),
    "C04": ["投资业务现金净额"],
    "C05": ("sum", ["已付股息(融资)", "已付利息(融资)"]),
    "C06": ["融资业务现金净额"],
    "C07": ["现金净额"],
}

REPORTS_HK = {
    "fzb": ("RPT_HKF10_FN_BALANCE", FIELD_MAP_HK_FZB),
    "lrb": ("RPT_HKF10_FN_INCOME", FIELD_MAP_HK_LRB),
    "llb": ("RPT_HKSK_FN_CASHFLOW", FIELD_MAP_HK_LLB),
}


# ---------------------------------------------------------------- 共享解析（港美股通用）

def validate_global_ticker(raw, pattern, example):
    text = raw.strip().upper()
    if not re.fullmatch(pattern, text):
        raise ArgError(f"代码格式非法: {raw!r}（示例：{example}；须给全市场后缀与前导零，脚本不猜测）")
    return text


def _dc_fetch_all(report, secu, endpoint, max_pages=10):
    """长表分页拉全（每页 500 行）。"""
    rows = []
    for page in range(1, max_pages + 1):
        d = get_json(DC_URL, endpoint=endpoint, params={
            "reportName": report, "columns": "ALL", "filter": f'(SECUCODE="{secu}")',
            "pageSize": "500", "pageNumber": str(page),
            "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        }, headers=DC_HEADERS)
        if d.get("success") is False:
            raise FetchError(endpoint, f"业务失败: {d.get('message')}",
                             action_hint="报表名可能已变更，请检查数据源规范文档")
        data = (d.get("result") or {}).get("data") or []
        rows.extend(data)
        if len(data) < 500:
            break
    return rows


def _resolve(mapping, rec):
    """按映射取值。返回 (value, raw_field, note)；value=None 表示不可得。"""
    if mapping is None:
        return None, "", ""
    if isinstance(mapping, list):
        for name in mapping:
            v = rec.get(name)
            if v is not None:
                return float(v), name, ""
        return None, mapping[0], ""
    op, parts = mapping
    base = rec.get(parts[0])
    if base is None:
        return None, parts[0], ""
    total = float(base)
    used = [parts[0]]
    for p in parts[1:]:
        v = rec.get(p)
        if v is not None:
            total = total + float(v) if op == "sum" else total - float(v)
            used.append(p)
    joiner = "+" if op == "sum" else "−"
    return total, parts[0], f"推导值：{joiner.join(used)}"


def fill_global_statements(env, secu, reports, years):
    """港美股三表通用填数：年报过滤 → 按期分组 → 字段映射；币种/准则透传。"""
    for group, (report, fmap) in reports.items():
        endpoint = f"em_dc_{report}"
        rows = _dc_fetch_all(report, secu, endpoint)
        annual = {}
        currency = ""
        standards = ""
        for r in rows:
            if r.get("REPORT_TYPE") != "年报":
                continue  # 年报过滤（兼容美股财年错位，TDD §6.1）
            period = str(r.get("REPORT_DATE", ""))[:10]
            rec = annual.setdefault(period, {})
            name = r.get("ITEM_NAME")
            if name and r.get("AMOUNT") is not None:
                rec[name] = r["AMOUNT"]
            if not currency and r.get("CURRENCY"):
                currency = CURRENCY_MAP.get(r["CURRENCY"], r["CURRENCY"])
            if not standards and (r.get("ACCOUNT_STANDARD") or r.get("ACCOUNTING_STANDARDS")):
                # 实测键名为 ACCOUNT_STANDARD（M7 手写记录有误，执行期修正）；双键兼容防御
                standards = r.get("ACCOUNT_STANDARD") or r.get("ACCOUNTING_STANDARDS")
        if not annual:
            add_error(env, endpoint, "无年报数据返回（REPORT_TYPE=年报 过滤后为空）",
                      action_hint="请核实 SECUCODE 后缀（.HK/.O/.N）；或该标的未被收录")
            continue
        if standards:
            env["meta"]["accounting_standards"] = standards
        periods = sorted(annual.keys(), reverse=True)[:years]
        if len(periods) < years:
            w = f"年报期数不足：仅取得 {len(periods)} 期（目标 {years} 期）"
            if w not in env["meta"]["warnings"]:
                env["meta"]["warnings"].append(w)
        hit = 0
        for period in periods:
            rec = annual[period]
            for field_id, mapping in fmap.items():
                value, raw_field, note = _resolve(mapping, rec)
                if value is not None:
                    hit += 1
                    add_item(env, field_id, value, period, endpoint, raw_field,
                             unit=currency or "", currency=currency or "", note=note)
                else:
                    reason = "该准则口径下不可得（见 TDD §6.1 缺口清单）" if mapping is None \
                        else "科目未披露"
                    add_missing(env, field_id, period, endpoint,
                                raw_field or "（无对应科目）", reason,
                                unit=currency or "", currency=currency or "")
        env["meta"]["field_coverage"][group.upper()] = f"{hit}/{len(fmap) * len(periods)}"


def main_hk(ticker, years, env):
    secu = validate_global_ticker(ticker, r"^\d{4,5}\.HK$", "00700.HK")
    env["ticker"] = secu
    fill_global_statements(env, secu, REPORTS_HK, years)


if __name__ == "__main__":
    run_cli(SCRIPT, "HK", main_hk, "港股代码（如 00700.HK，须 5 位前导零+.HK 后缀）")
