#!/usr/bin/env python3
"""A 股公告检索与 PDF 下载（FR-16，巨潮直连）。

职责边界（TDD §5）：脚本只到「公告清单 + PDF 落地」为止；D01–D08/D10–D12 字段值提取
由 Agent 阅读 PDF 完成并自行生成回执（来源=公告 PDF 文件名+页码）。

用法：
  python3 scripts/fetch_announce.py 600519 --preset yearly --download
  python3 scripts/fetch_announce.py 600519 --searchkey 减持 --days 730

设计依据：docs/TDD-v1.0.0-datasource.md §5。合规声明：代码自研；巨潮为官方披露平台（S 级）。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds_common import (  # noqa: E402
    CST, EXIT_API, EXIT_ARG, EXIT_BLOCKED, FetchError, BlockedError, ArgError,
    add_error, add_item, add_missing, get_json, limited_post, limited_get,
    make_envelope, now_iso,
)
from fetch_a_share import normalize_a_code  # noqa: E402

SCRIPT = os.path.basename(__file__)
CNINFO_ORG_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "https://static.cninfo.com.cn/"
CNINFO_HEADERS = {
    "Referer": "https://www.cninfo.com.cn/new/disclosure",
    "Origin": "https://www.cninfo.com.cn",
}

# 预设检索矩阵（TDD §5 默认类别；searchkey 为标题关键词，存在召回局限——
# Agent 做 D11/D12 统计时可基于清单初筛扩大检索词复检）
PRESETS = {
    "yearly":    {"category": "category_ndbg_szsh;", "searchkey": ""},   # 年报（D01–D08 载体）
    "reduction": {"category": "", "searchkey": "减持"},                  # D11 大股东减持
    "pledge":    {"category": "", "searchkey": "质押"},                  # D10 控股股东质押明细
    "auditor":   {"category": "", "searchkey": "会计师事务所"},          # D12 审计机构变更
    "cfo":       {"category": "", "searchkey": "财务总监"},              # D12 财务负责人变更
}

_ORGID_MAP = {}


def cninfo_orgid(code6):
    """官方映射表动态查询（模块级缓存）；查不到回退硬编码规则（TDD §5）。"""
    if not _ORGID_MAP:
        d = get_json(CNINFO_ORG_URL, endpoint="cninfo_orgid", timeout=15)
        stocks = d.get("stockList")
        if not isinstance(stocks, list):
            raise FetchError("cninfo_orgid", "orgId 映射表结构漂移：stockList 缺失",
                             action_hint="巨潮映射表结构可能已变更")
        _ORGID_MAP.update({s["code"]: s["orgId"] for s in stocks
                           if s.get("code") and s.get("orgId")})
    org = _ORGID_MAP.get(code6)
    if org:
        return org
    if code6.startswith("6"):
        return f"gssh0{code6}"
    if code6.startswith(("4", "8")):
        return f"gsbj0{code6}"
    return f"gssz0{code6}"


def _ts_to_date(ts):
    """announcementTime 为 Unix 毫秒；异常值原样截断返回。"""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, CST).strftime("%Y-%m-%d")
    return str(ts or "")[:10]


def search_announcements(env, code6, category, searchkey, days, page_size):
    org = cninfo_orgid(code6)
    se_date = ""
    if days:
        end = datetime.now(CST)
        start = end - timedelta(days=days)
        se_date = f"{start:%Y-%m-%d}~{end:%Y-%m-%d}"
    payload = {
        "stock": f"{code6},{org}",
        "tabName": "fulltext",
        "pageSize": str(page_size),
        "pageNum": "1",
        "column": "",
        "category": category,
        "plate": "",
        "seDate": se_date,
        "searchkey": searchkey,
        "secid": "",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "false",  # 避免标题被 <em> 高亮标签污染
    }
    resp = limited_post(CNINFO_QUERY_URL, endpoint="cninfo_query", data=payload,
                        headers=CNINFO_HEADERS)
    try:
        d = resp.json()
    except ValueError as exc:
        raise FetchError("cninfo_query", f"JSON 解析失败（疑似接口漂移）: {exc}") from exc
    anns = d.get("announcements")
    if anns is None:
        # 巨潮对无结果返回 announcements=null（数据不存在），与结构漂移区分
        return []
    if not isinstance(anns, list):
        raise FetchError("cninfo_query", "返回结构漂移：announcements 非列表")
    return anns


def download_pdf(env, code6, ann, dest_dir):
    """下载并校验 %PDF 头；返回本地路径（失败记 error 返回空串）。"""
    adjunct = ann.get("adjunctUrl") or ""
    if not adjunct:
        return ""
    url = CNINFO_PDF_BASE + adjunct
    ann_id = str(ann.get("announcementId") or "unknown")
    path = os.path.join(dest_dir, code6, f"{ann_id}.pdf")
    try:
        resp = limited_get(url, endpoint="cninfo_pdf", timeout=30)
        content = resp.content
        if not content.startswith(b"%PDF"):
            raise FetchError("cninfo_pdf", f"PDF 校验失败（非 %PDF 头）: {adjunct}",
                             action_hint="链接可能已失效或被拦截")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path
    except FetchError as exc:
        add_error(env, exc.endpoint, str(exc), exc.http_status, exc.action_hint)
        return ""


def main():
    parser = argparse.ArgumentParser(
        prog=f"scripts/{SCRIPT}", description="内置数据源：巨潮公告检索与 PDF 下载")
    parser.add_argument("ticker", help="A 股代码（如 600519）")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="yearly",
                        help="预设检索：yearly 年报 / reduction 减持 / pledge 质押 / auditor 会所 / cfo 财务总监")
    parser.add_argument("--searchkey", default=None, help="自定义标题关键词（覆盖 preset）")
    parser.add_argument("--category", default=None, help="自定义巨潮类别（覆盖 preset）")
    parser.add_argument("--days", type=int, default=730, help="回溯天数（默认 730，0=不限）")
    parser.add_argument("--page-size", type=int, default=30)
    parser.add_argument("--download", action="store_true", help="下载公告 PDF 到 ./announcements/<代码>/")
    parser.add_argument("--max-download", type=int, default=5, help="PDF 下载条数上限（默认 5）")
    parser.add_argument("--out", default="", help="结果落盘路径（可选）")
    args = parser.parse_args()

    env = make_envelope(args.ticker, "A", SCRIPT)
    try:
        code6 = normalize_a_code(args.ticker)
        env["ticker"] = code6
        preset = PRESETS[args.preset]
        category = args.category if args.category is not None else preset["category"]
        searchkey = args.searchkey if args.searchkey is not None else preset["searchkey"]
        anns = search_announcements(env, code6, category, searchkey, args.days,
                                    args.page_size)
        if not anns:
            add_missing(env, "ANN", "", "cninfo", "announcements",
                        f"检索无结果（category={category or '全部'} searchkey={searchkey or '无'} "
                        f"days={args.days}）", unit="text", currency="")
        downloaded = 0
        for ann in anns:
            title = ann.get("announcementTitle") or ""
            adjunct = ann.get("adjunctUrl") or ""
            extra = {
                "type": ann.get("announcementTypeName") or "",
                "pdf_url": CNINFO_PDF_BASE + adjunct if adjunct else "",
                "announcementId": str(ann.get("announcementId") or ""),
            }
            if args.download and adjunct and downloaded < args.max_download:
                local = download_pdf(env, code6, ann, "./announcements")
                if local:
                    extra["local_path"] = local
                    downloaded += 1
            add_item(env, "ANN", title, _ts_to_date(ann.get("announcementTime")),
                     "cninfo", "announcementTitle", unit="text", currency="", extra=extra)
        env["meta"]["field_coverage"] = {"ANN": str(len(anns))}
    except ArgError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        sys.exit(EXIT_ARG)
    except BlockedError as exc:
        add_error(env, exc.endpoint, str(exc), exc.http_status, exc.action_hint)
        _emit(env, args.out)
        print(f"[风控] {exc} | {exc.action_hint}", file=sys.stderr)
        sys.exit(EXIT_BLOCKED)
    except FetchError as exc:
        add_error(env, exc.endpoint, str(exc), exc.http_status, exc.action_hint)
        _emit(env, args.out)
        print(f"[接口故障] {exc} | {exc.action_hint}", file=sys.stderr)
        sys.exit(EXIT_API)
    _emit(env, args.out)
    sys.exit(0)


def _emit(env, out_path):
    import json
    text = json.dumps(env, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
