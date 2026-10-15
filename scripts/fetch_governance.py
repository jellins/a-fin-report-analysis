#!/usr/bin/env python3
"""A 股治理数据取数（FR-17 + D14 推导）。

覆盖：D13 分红送转 / 股东户数历史 / D11 高管持股变动 / D10 质押粗筛（中登口径）/
D14 历年股权融资（现金流量表推导）。各报表独立成败，互不阻塞（TDD §4）。

用法：python3 scripts/fetch_governance.py 600519 [--years 5] [--out receipt.json]

设计依据：docs/TDD-v1.0.0-datasource.md §4。合规声明：代码自研；接口为公开事实信息。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    FetchError, add_error, add_item, add_missing, get_json, run_cli, sina_report,
)
from fetch_a_share import normalize_a_code  # noqa: E402

SCRIPT = os.path.basename(__file__)
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DC_HEADERS = {"Referer": "https://data.eastmoney.com/"}


def dc_query(report, code6, sort, page_size):
    """东财 datacenter 统一查询；业务失败（如 9501 报表配置不存在）按接口故障处理。"""
    d = get_json(DC_URL, endpoint=f"em_dc_{report}", params={
        "reportName": report, "columns": "ALL",
        "filter": f'(SECURITY_CODE="{code6}")',
        "pageSize": str(page_size), "sortColumns": sort, "sortTypes": "-1",
    }, headers=DC_HEADERS)
    if d.get("success") is False:
        raise FetchError(f"em_dc_{report}", f"业务失败: {d.get('message')}",
                         action_hint="报表名可能已变更，请检查数据源规范文档")
    return (d.get("result") or {}).get("data") or []


def _date10(raw):
    return str(raw or "")[:10]


# ---------------------------------------------------------------- D13 分红送转

def fill_dividend(env, code6):
    rows = dc_query("RPT_SHAREBONUS_DET", code6, "EX_DIVIDEND_DATE", 20)
    if not rows:
        add_missing(env, "D13", "", "em_dc_RPT_SHAREBONUS_DET", "分红送转",
                    "无分红送转记录（可能从未分红）", unit="CNY/股")
        return
    for r in rows:
        period = _date10(r.get("EX_DIVIDEND_DATE")) or _date10(r.get("PLAN_NOTICE_DATE"))
        bonus = r.get("PRETAX_BONUS_RMB")
        # 实测（2026-10-15）：该字段为每 10 股派息口径（600519 返回 280.24 = 10派280.24元，
        # 每股口径不可能超过 EPS）——参考项目注释“每股派息”有误，按实测口径标注
        note = (f"每10股转增{r.get('TRANSFER_RATIO') or 0}/送股{r.get('BONUS_RATIO') or 0}；"
                f"进度={r.get('ASSIGN_PROGRESS') or ''}")
        add_item(env, "D13", float(bonus) if bonus is not None else 0.0, period,
                 "em_dc_RPT_SHAREBONUS_DET", "PRETAX_BONUS_RMB 每10股派息(税前)",
                 unit="CNY/10股", note=note)


# ---------------------------------------------------------------- 股东户数历史

def fill_holder_num(env, code6):
    rows = dc_query("RPT_HOLDERNUM_DET", code6, "END_DATE", 10)
    if not rows:
        add_missing(env, "INFO_HOLDER_NUM", "", "em_dc_RPT_HOLDERNUM_DET", "股东户数",
                    "无股东户数记录", unit="户")
        return
    for r in rows:
        num = r.get("HOLDER_NUM")
        ratio = r.get("HOLDER_NUM_RATIO")
        note = f"环比 {ratio}%" if ratio is not None else ""
        add_item(env, "INFO_HOLDER_NUM", float(num) if num is not None else None,
                 _date10(r.get("END_DATE")), "em_dc_RPT_HOLDERNUM_DET", "HOLDER_NUM 股东户数",
                 unit="户", note=note)


# ---------------------------------------------------------------- D11 高管持股变动（大股东减持主路径走公告检索，见 fetch_announce.py）

def fill_executive_change(env, code6):
    rows = dc_query("RPT_EXECUTIVE_HOLD_DETAILS", code6, "CHANGE_DATE", 50)
    if not rows:
        add_missing(env, "D11", "", "em_dc_RPT_EXECUTIVE_HOLD_DETAILS", "高管持股变动",
                    "无高管持股变动记录；大股东减持请结合公告检索", unit="股")
        return
    for r in rows:
        shares = r.get("CHANGE_SHARES")
        note = (f"{r.get('PERSON_NAME') or ''} {r.get('CHANGE_REASON') or ''}；"
                "高管口径（大股东减持主路径见公告检索）")
        add_item(env, "D11", float(shares) if shares is not None else None,
                 _date10(r.get("CHANGE_DATE")), "em_dc_RPT_EXECUTIVE_HOLD_DETAILS",
                 "CHANGE_SHARES 变动股数(正增负减)", unit="股", note=note)


# ---------------------------------------------------------------- D10 质押粗筛（中登公司整体口径）

def fill_pledge(env, code6):
    rows = dc_query("RPT_CSDC_LIST", code6, "TRADE_DATE", 1)
    if not rows:
        add_missing(env, "D10", "", "em_dc_RPT_CSDC_LIST", "质押比例",
                    "无质押记录或标的不在中登统计范围", unit="%")
        return
    r = rows[0]
    ratio = r.get("PLEDGE_RATIO")
    add_item(env, "D10", float(ratio) if ratio is not None else None,
             _date10(r.get("TRADE_DATE")), "em_dc_RPT_CSDC_LIST", "PLEDGE_RATIO",
             unit="%",
             note="中登公司整体质押比例（总股本中被质押占比）；控股股东个体质押明细须以巨潮质押公告为准（S5-01 裁决口径）")


# ---------------------------------------------------------------- D14 历年股权融资（现金流口径推导，TDD §0 M11）

def fill_equity_financing(env, code6, years):
    table, warning = sina_report(code6, "llb", years=years)
    if warning and warning not in env["meta"]["warnings"]:
        env["meta"]["warnings"].append(warning)
    if not table:
        add_error(env, "sina_llb", "现金流量表无数据，D14 无法推导",
                  action_hint="请核实代码；或改用公告口径手工统计")
        return
    for period, rec in table.items():
        absorb = rec.get("吸收投资收到的现金")
        minority = rec.get("子公司吸收少数股东投资收到的现金")
        if absorb is None and minority is None:
            add_missing(env, "D14", period, "sina_llb", "吸收投资收到的现金",
                        "科目未披露")
            continue
        net = (absorb or 0.0) - (minority or 0.0)
        add_item(env, "D14", net, period, "sina_llb",
                 "吸收投资收到的现金 − 子公司吸收少数股东投资收到的现金",
                 note="现金流口径：含增发/配股/IPO 到账；与公告口径可能差异，大额时请结合公告核验")


# ---------------------------------------------------------------- 主流程（各报表独立成败，TDD §4）

def main(ticker, years, env):
    code6 = normalize_a_code(ticker)
    env["ticker"] = code6
    for name, fn in (("D13", fill_dividend), ("INFO_HOLDER_NUM", fill_holder_num),
                     ("D11", fill_executive_change), ("D10", fill_pledge)):
        try:
            fn(env, code6)
        except FetchError as exc:
            add_error(env, exc.endpoint, f"{name} 取数失败: {exc}", exc.http_status,
                      exc.action_hint or "该组数据缺失不阻塞其他组；可稍后重试或手工补充")
    try:
        fill_equity_financing(env, code6, years)
    except FetchError as exc:
        add_error(env, exc.endpoint, f"D14 推导失败: {exc}", exc.http_status, exc.action_hint)


if __name__ == "__main__":
    run_cli(SCRIPT, "A", main, "A 股代码（如 600519）")
