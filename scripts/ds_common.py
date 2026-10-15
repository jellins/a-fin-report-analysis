#!/usr/bin/env python3
"""内置数据源公共基础设施：域级限流 / 重试 / 回执 envelope / 失败语义 / CLI 框架。

设计依据：docs/TDD-v1.0.0-datasource.md §2（统一基础设施）。
合规声明：全部代码自研；接口 URL/参数为公开事实信息（接口调研参考致谢见 README.md）。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- 常量

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CST = timezone(timedelta(hours=8))

# 退出码（TDD §2.2 失败语义分级）
EXIT_OK = 0        # 成功（允许 items 内含 MISSING/CONFLICT）
EXIT_ARG = 3       # 参数错误（代码格式非法、不支持的号段）
EXIT_API = 4       # 接口故障（连接/超时/5xx 重试耗尽、结构漂移）
EXIT_BLOCKED = 5   # 风控（HTTP 403，不重试）

# 域级限流：(域名后缀, 基础间隔秒, 随机抖动上限秒)。归域按 host 后缀匹配（TDD §2.3）。
DOMAIN_INTERVALS = (
    ("sec.gov", 0.15, 0.05),         # SEC 硬性限速 ≤10/s，设计取 ≤8/s 留余量
    ("eastmoney.com", 1.0, 0.5),     # 东财系全域合并：datacenter-web/push2/emweb/push2his
    ("sina.cn", 1.0, 0.5),
    ("cninfo.com.cn", 1.0, 0.5),     # 巨潮：www/static 子域同域
)
DEFAULT_INTERVAL = (1.0, 0.5)

BLOCKED_HINT = "HTTP 403 风控拒绝（不重试）；请更换网络环境或稍后降频重试，见 scripts/selftest.md 被封对策"
NETWORK_HINT = "连接失败；请检查网络，若持续失败见 scripts/selftest.md 被封对策"


# ---------------------------------------------------------------- 错误类型

class ArgError(Exception):
    """参数错误（退出码 3）。"""


class FetchError(Exception):
    """接口故障（退出码 4）。"""

    def __init__(self, endpoint, message, http_status=None, action_hint=""):
        super().__init__(message)
        self.endpoint = endpoint
        self.http_status = http_status
        self.action_hint = action_hint


class BlockedError(FetchError):
    """风控拒绝（退出码 5）。"""


# ---------------------------------------------------------------- 限流器

class RateLimiter:
    """按域串行限流：同域相邻请求间隔 >= 基础间隔 + 随机抖动。进程级单例。"""

    def __init__(self):
        self._last = {}  # domain -> time.monotonic()

    @staticmethod
    def domain_of(url):
        host = re.sub(r"^https?://", "", url).split("/", 1)[0].lower()
        for suffix, base, jitter in DOMAIN_INTERVALS:
            if host == suffix or host.endswith("." + suffix):
                return suffix, base, jitter
        return host, DEFAULT_INTERVAL[0], DEFAULT_INTERVAL[1]

    def wait(self, url):
        domain, base, jitter = self.domain_of(url)
        last = self._last.get(domain)
        if last is not None:
            gap = base + random.uniform(0, jitter)
            elapsed = time.monotonic() - last
            if elapsed < gap:
                time.sleep(gap - elapsed)
        self._last[domain] = time.monotonic()


_LIMITER = RateLimiter()
_SESSION = requests.Session()


# ---------------------------------------------------------------- 统一请求出口

def _request(method, url, *, endpoint, params=None, data=None, headers=None,
             timeout=15, retries=2):
    """所有 HTTP 请求的唯一出口（selftest 静态检查：scripts 内禁止裸 requests.get/post）。

    重试策略（TDD §2.3）：仅连接错误/超时/5xx 指数退避 1s→2s，最多 2 次；
    403 不重试抛 BlockedError；其他 4xx 抛 FetchError。
    """
    req_headers = {"User-Agent": UA}
    if headers:
        req_headers.update(headers)
    last_err = None
    for attempt in range(retries + 1):
        _LIMITER.wait(url)
        try:
            resp = _SESSION.request(method, url, params=params, data=data,
                                    headers=req_headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1s → 2s
                continue
            raise FetchError(endpoint, f"连接失败（重试 {retries} 次耗尽）: {exc}",
                             action_hint=NETWORK_HINT) from exc
        if resp.status_code == 403:
            raise BlockedError(endpoint, "HTTP 403 风控拒绝", http_status=403,
                               action_hint=BLOCKED_HINT)
        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise FetchError(endpoint, f"HTTP {resp.status_code}（重试 {retries} 次耗尽）",
                             http_status=resp.status_code, action_hint=NETWORK_HINT)
        if resp.status_code >= 400:
            raise FetchError(endpoint, f"HTTP {resp.status_code}",
                             http_status=resp.status_code)
        return resp
    raise FetchError(endpoint, f"请求失败: {last_err}", action_hint=NETWORK_HINT)


def limited_get(url, *, endpoint, params=None, headers=None, timeout=15, retries=2):
    return _request("GET", url, endpoint=endpoint, params=params,
                    headers=headers, timeout=timeout, retries=retries)


def limited_post(url, *, endpoint, data=None, headers=None, timeout=15, retries=2):
    return _request("POST", url, endpoint=endpoint, data=data,
                    headers=headers, timeout=timeout, retries=retries)


def get_json(url, *, endpoint, params=None, headers=None, timeout=15, retries=2):
    """GET 并解析 JSON；结构解析失败按接口故障处理（字段漂移探测，TDD §2.2）。"""
    resp = limited_get(url, endpoint=endpoint, params=params,
                       headers=headers, timeout=timeout, retries=retries)
    try:
        return resp.json()
    except ValueError as exc:
        raise FetchError(endpoint, f"JSON 解析失败（疑似接口字段漂移）: {exc}",
                         action_hint="接口结构可能已变更，请检查数据源规范文档") from exc


# ---------------------------------------------------------------- 回执 envelope（TDD §2.1）

def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def make_envelope(ticker, market, script):
    return {
        "ticker": ticker,
        "market": market,
        "script": script,
        "generated_at": now_iso(),
        "items": [],
        "conflicts": [],
        "errors": [],
        "meta": {"field_coverage": {}, "warnings": []},
    }


def add_item(env, field_id, value, period, source, raw_field,
             unit="CNY", currency="CNY", note="", extra=None):
    item = {
        "field_id": field_id,
        "value": value,
        "period": period,
        "source": source,
        "raw_field": raw_field,
        "retrieved_at": now_iso(),
        "unit": unit,
        "currency": currency,
        "status": "OK",
        "note": note,
    }
    if extra:
        item["extra"] = extra  # 可选结构化附加信息（如公告的 pdf_url/announcementId）
    env["items"].append(item)


def add_missing(env, field_id, period, source, raw_field, note,
                unit="CNY", currency="CNY"):
    env["items"].append({
        "field_id": field_id,
        "value": None,
        "period": period,
        "source": source,
        "raw_field": raw_field,
        "retrieved_at": now_iso(),
        "unit": unit,
        "currency": currency,
        "status": "MISSING",
        "note": note,
    })


def add_conflict(env, field_id, period, source_a, value_a, source_b, value_b):
    denom = max(abs(value_a), abs(value_b))
    rel = abs(value_a - value_b) / denom if denom else 0.0
    env["conflicts"].append({
        "field_id": field_id,
        "period": period,
        "source_a": source_a,
        "value_a": value_a,
        "source_b": source_b,
        "value_b": value_b,
        "rel_diff": round(rel, 6),
    })


def add_error(env, endpoint, message, http_status=None, action_hint=""):
    env["errors"].append({
        "endpoint": endpoint,
        "http_status": http_status,
        "message": message,
        "action_hint": action_hint,
    })


# ---------------------------------------------------------------- 数值解析

def parse_num(raw):
    """字符串数值 → float；空值/无法解析返回 None（由调用方记 MISSING）。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace(",", "")
    if text in ("", "-", "--", "None", "null", "nan"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- 新浪三表公共解析（TDD §3.2）

SINA_URL = ("https://quotes.sina.cn/cn/api/openapi.php/"
            "CompanyFinanceService.getFinanceReport2022")


def sina_prefix(code6):
    """代码 → 新浪市场前缀。老号段/非法代码由 normalize_a_code 拦截，此处不重复校验。"""
    if code6.startswith("6"):
        return "sh"
    if code6.startswith(("0", "3")):
        return "sz"
    if code6.startswith("920"):
        return "bj"
    raise ArgError(f"不支持的市场号段: {code6}")


def sina_report(code6, source, years=5):
    """拉取新浪三表年报序列（合并报表口径，TDD §3.2）。

    source: "fzb" | "lrb" | "llb"。
    返回 (data, warning)：data = {"YYYY-MM-DD": {科目名: float|None}} 按报告期倒序；
    期数不足 years 时如实返回已有期并给出 warning（次新股语义，不视为失败）。
    """
    paper = sina_prefix(code6) + code6
    endpoint = f"sina_{source}"
    report_list = None
    for num in (20, 28):
        d = get_json(SINA_URL, endpoint=endpoint, params={
            "paperCode": paper, "source": source, "type": "0",
            "page": "1", "num": str(num),
        })
        result = d.get("result")
        if isinstance(result, dict) and result.get("data") is None:
            # 实测（2026-10-15）：不存在代码返回 status.code=0 但 data=null → 数据不存在语义
            return {}, "标的无年报数据返回：代码可能不存在或未披露"
        if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
            raise FetchError(endpoint, "返回结构漂移：result.data 缺失",
                             action_hint="新浪接口结构可能已变更，请检查数据源规范文档")
        report_list = result["data"].get("report_list") or {}
        annual = sorted((p for p in report_list if p.endswith("1231")), reverse=True)
        if len(annual) >= years or num == 28:
            break
    out = {}
    for p in annual[:years]:
        rec = {}
        for it in (report_list[p].get("data") or []):
            title = it.get("item_title")
            if not title:
                continue
            rec[title] = parse_num(it.get("item_value"))
        out[f"{p[:4]}-{p[4:6]}-{p[6:8]}"] = rec
    warning = ""
    if len(out) < years:
        warning = f"年报期数不足：仅取得 {len(out)} 期（目标 {years} 期），可能为上市未满 {years} 年"
    return out, warning


# ---------------------------------------------------------------- CLI 框架（TDD §2.4）

def run_cli(script_name, market, main_fn, ticker_help):
    """统一 CLI：ticker [--years 5] [--out PATH] [--verbose]。

    main_fn(ticker, years, env) 负责填充 envelope；此处统一异常 → 退出码映射。
    """
    parser = argparse.ArgumentParser(prog=f"scripts/{script_name}",
                                     description=f"内置数据源：{ticker_help}")
    parser.add_argument("ticker", help=ticker_help)
    parser.add_argument("--years", type=int, default=5, help="年报期数（默认 5）")
    parser.add_argument("--out", default="", help="结果落盘路径（可选）")
    parser.add_argument("--verbose", action="store_true", help="stderr 输出诊断信息")
    args = parser.parse_args()

    env = make_envelope(args.ticker, market, script_name)
    try:
        main_fn(args.ticker, args.years, env)
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
    sys.exit(EXIT_OK)


def _emit(env, out_path):
    text = json.dumps(env, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
