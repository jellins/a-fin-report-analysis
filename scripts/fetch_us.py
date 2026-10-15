#!/usr/bin/env python3
"""美股对照组三表取数 + SEC EDGAR 官方源交叉校验（FR-18/FR-19）。

主源：东财美股 F10 三表（长表 ITEM_NAME/AMOUNT，年报按 REPORT_TYPE=="年报" 过滤，
兼容美股财年错位——AAPL 财年止于 9 月，REPORT_DATE=2025-09-27 仍计年报）。
校验源：SEC EDGAR companyconcept（S 级官方披露）；须配置 SEC_CONTACT 环境变量启用，
未配置则明确提示并跳过（exit 0，主数据不受影响）。容差 1%，仅标注不裁决：
超差进 conflicts，一致进 item note。

用法：SEC_CONTACT=you@example.com python3 scripts/fetch_us.py AAPL.O [--years 5] [--out receipt.json]

设计依据：docs/TDD-v1.0.0-datasource.md §6.1/§6.2。合规声明：代码自研；接口为公开事实信息。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    FetchError, add_conflict, add_error, get_json, run_cli,
)
from fetch_hk import fill_global_statements, validate_global_ticker  # noqa: E402

SCRIPT = os.path.basename(__file__)

# ---------------------------------------------------------------- 美股字段映射（2026-10-15 冻结，TDD §11 留白②）
# 值形态与港股一致：[候选科目名链] / ("sum"|"sub", [成分]) / None=MISSING。
# 对照 AAPL.O 最新期实测科目清单逐项核对：fzb 29 科目 / lrb 25 科目 / llb 24 科目。

FIELD_MAP_US_FZB = {
    "A01": ["现金及现金等价物"],
    "A02": ["有价证券投资(流动)"],
    "A03": None,  # 美股无应收票据分类
    "A04": ["应收账款"],
    "A05": None,
    "A06": None,  # 预付款项未单列
    "A07": ["其他应收款"],
    "A08": ["存货"],
    "A09": None,
    "A10": ["流动资产合计"],
    "A11": None,  # 长期股权投资未单列
    "A12": ["有价证券投资(非流动)"],
    "A13": None,
    "A14": ["物业、厂房及设备"],
    "A15": None,  # 在建工程未单列
    "A16": None,  # 使用权资产未单列
    "A17": ["无形资产"],
    "A18": None,
    "A19": None,  # 商誉未单列（已知缺口，须查 10-K 附注）
    "A20": None,
    "A21": None,  # 递延税项资产未单列
    "A22": ["总资产"],
    "A23": ["短期债务"],
    "A24": ["应付票据(流动)"],
    "A25": ["应付账款"],
    "A26": ["递延收入(流动)"],  # 近似合同负债口径
    "A27": None,
    "A28": ["流动负债合计"],
    "A29": ["长期负债"],
    "A30": None,
    "A31": None,  # 租赁负债未单列
    "A32": None,  # 递延税项负债未单列
    "A33": ["总负债"],
    "A34": ["归属于母公司股东权益"],
    "A35": ["少数股东权益"],  # 部分公司无此列（TDD §11），未披露记 MISSING
}

FIELD_MAP_US_LRB = {
    "B01": ["营业收入", "主营收入"],
    "B02": ["营业成本", "主营成本"],
    "B03": ["营销费用"],  # 美股 SG&A 合并列示，无单独行政开支
    "B04": None,
    "B05": ["研发费用"],
    "B06": None,  # 无财务费用对应科目（利息收支并入其他收入(支出)）
    "B06a": None,  # 美股不可得（TDD §11）
    "B06b": None,
    "B07": None,  # 「其他收入(支出)」为营业外性质，与 A 股其他收益口径不可比，不配
    "B08": None,
    "B09": None,
    "B10": None,
    "B11": ["营业利润"],
    "B12": None,  # 国际准则无营业外收支分类
    "B13": None,
    "B14": ["持续经营税前利润"],
    "B15": ["所得税"],
    "B16": ["净利润", "持续经营净利润"],  # fallback：部分期「净利润」科目为空（如 AAPL 2023 期），持续经营口径兜回（无非持续经营业务公司两者相等）
    "B17": ["归属于母公司股东净利润", "归属于普通股股东净利润"],
    "B18": ["少数股东损益"],  # 部分公司无此列（TDD §11）
    "B19": ["其他全面收益合计项"],
    "B20": ["全面收益总额"],
}

FIELD_MAP_US_LLB = {
    "C01": None,  # 间接法报表无销售收现行（已知缺口）
    "C02": ["经营活动产生的现金流量净额"],
    "C03": ["购买固定资产"],
    "C04": ["投资活动产生的现金流量净额"],
    "C05": ["股息支付"],  # US GAAP 利息支付列于经营活动，融资活动仅股息/回购
    "C06": ["筹资活动产生的现金流量净额"],
    "C07": ["现金及现金等价物增加(减少)额"],
}

REPORTS_US = {
    "fzb": ("RPT_USF10_FN_BALANCE", FIELD_MAP_US_FZB),
    "lrb": ("RPT_USF10_FN_INCOME", FIELD_MAP_US_LRB),
    "llb": ("RPT_USSK_FN_CASHFLOW", FIELD_MAP_US_LLB),
}

# ---------------------------------------------------------------- EDGAR 交叉校验（TDD §6.2）

EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
EDGAR_TOLERANCE = 0.01  # 容差 1%：汇率/口径差异仅标注不裁决

# 校验 tag（主备）：收入新 tag 优先，空则 fallback 旧 tag（§0 M9 tag 变迁）
EDGAR_CHECKS = {
    "B01": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
    "B16": ["NetIncomeLoss"],
    "C02": ["NetCashProvidedByUsedInOperatingActivities"],
}

_CIK_MAP = {}


def _edgar_cik(ticker_base, headers):
    """company_tickers.json → CIK（模块级缓存）。未命中返回 None。"""
    if not _CIK_MAP:
        d = get_json(EDGAR_TICKERS_URL, endpoint="edgar_tickers", headers=headers)
        # 实测（2026-10-15）：根节点为数字字符串键控 dict（{"0": {...}, ...}），非数组
        if isinstance(d, dict):
            entries = list(d.values())
        elif isinstance(d, list):
            entries = d
        else:
            raise FetchError("edgar_tickers", "返回结构漂移：根节点非 dict/list",
                             action_hint="EDGAR 映射表结构可能已变更")
        for e in entries:
            try:
                if isinstance(e, dict) and e.get("ticker") and e.get("cik_str"):
                    _CIK_MAP[str(e["ticker"]).upper()] = int(e["cik_str"])
            except (TypeError, ValueError):
                continue
    return _CIK_MAP.get(ticker_base)


def _edgar_concept(cik, tag, headers):
    """单 tag 年度序列。404 = tag 不存在（数据不存在语义）返回 None；其他故障上抛。"""
    url = EDGAR_CONCEPT_URL.format(cik=f"{cik:010d}", tag=tag)
    try:
        return get_json(url, endpoint=f"edgar_{tag}", headers=headers)
    except FetchError as exc:
        if exc.http_status == 404:
            return None
        raise


def _fy_points(concept):
    """form=10-K 且 fp=FY 的年度点 → {end_year: (end_date, value)}；同年多点取 filed 最新。"""
    units = concept.get("units") or {}
    rows = units.get("USD") or next(iter(units.values()), [])
    best = {}
    for r in rows:
        if r.get("form") != "10-K" or r.get("fp") != "FY":
            continue
        end = str(r.get("end") or "")
        if len(end) < 4 or r.get("val") is None:
            continue
        year = end[:4]
        filed = str(r.get("filed") or "")
        if year not in best or filed >= best[year][2]:
            best[year] = (end, float(r["val"]), filed)
    return {y: (v[0], v[1]) for y, v in best.items()}


def edgar_cross_check(env, secu):
    """EDGAR 官方源交叉（增强校验，不阻塞主数据）。

    期对齐键：财年结束年份（东财 period[:4] == EDGAR end[:4]，兼容 9 月财年）。
    rel_diff > 1% → conflicts；≤ 1% → item note 追加交叉一致标记；无对齐点静默。
    """
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if not contact:
        msg = "EDGAR 校验未启用：未配置 SEC_CONTACT 环境变量（配置联系邮箱后重跑可做官方源交叉）"
        print(f"[提示] {msg}", file=sys.stderr)
        env["meta"]["warnings"].append(msg)
        return
    headers = {"User-Agent": f"fin-report-skill contact: {contact}"}
    try:
        cik = _edgar_cik(secu.split(".")[0], headers)
        if not cik:
            env["meta"]["warnings"].append(
                f"EDGAR CIK 映射未命中：{secu}（可能为非美国上市公司），跳过官方源交叉")
            return
        for field_id, tags in EDGAR_CHECKS.items():
            concept, used_tag = None, ""
            for tag in tags:
                concept = _edgar_concept(cik, tag, headers)
                if concept is not None:
                    used_tag = tag
                    break
            if concept is None:
                env["meta"]["warnings"].append(
                    f"EDGAR 校验：{field_id} 全部候选 tag（{'/'.join(tags)}）无数据，跳过")
                continue
            fy = _fy_points(concept)
            for item in env["items"]:
                if item["field_id"] != field_id or item["status"] != "OK":
                    continue
                point = fy.get(str(item["period"])[:4])
                if point is None:
                    continue
                _, edgar_value = point
                em_value = float(item["value"])
                denom = max(abs(em_value), abs(edgar_value))
                rel = abs(em_value - edgar_value) / denom if denom else 0.0
                if rel > EDGAR_TOLERANCE:
                    add_conflict(env, field_id, item["period"],
                                 item["source"], em_value, f"edgar_{used_tag}", edgar_value)
                else:
                    mark = f"EDGAR 交叉一致（{used_tag}，rel_diff={rel:.4f}）"
                    item["note"] = f"{item['note']}；{mark}" if item["note"] else mark
    except FetchError as exc:
        # 校验源故障不阻塞主数据：errors 可观测 + warning，exit 仍为 0
        add_error(env, exc.endpoint, f"EDGAR 校验中断: {exc}", exc.http_status, exc.action_hint)
        env["meta"]["warnings"].append("EDGAR 校验因接口故障中断，主数据不受影响")


def main_us(ticker, years, env):
    secu = validate_global_ticker(ticker, r"^[A-Z]+\.(O|N)$",
                                  "AAPL.O（.O=纳斯达克，.N=纽交所）")
    env["ticker"] = secu
    fill_global_statements(env, secu, REPORTS_US, years)
    edgar_cross_check(env, secu)


if __name__ == "__main__":
    run_cli(SCRIPT, "US", main_us,
            "美股代码（如 AAPL.O；须含交易所后缀 .O/.N，脚本不猜测）")
