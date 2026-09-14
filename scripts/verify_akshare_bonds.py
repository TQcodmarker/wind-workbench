"""Run real AKShare functions in an isolated interpreter and retain evidence.

This is a bounded verification utility, not a workbench data provider.
It never imports the backend, reads credentials, or changes business data.
"""
import argparse
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "akshare-validation-20260912"
CASES = {
    "yield_curve": ("bond_china_yield", {"start_date": "20260901", "end_date": "20260911"}),
    "spot_deals": ("bond_spot_deal", {}),
    "spot_quotes": ("bond_spot_quote", {}),
    "local_issues": ("bond_local_government_issue_cninfo", {"start_date": "20260901", "end_date": "20260911"}),
    "treasury_issues": ("bond_treasure_issue_cninfo", {"start_date": "20260901", "end_date": "20260911"}),
    "bond_lookup": ("bond_info_cm", {"bond_code": "809336"}),
    "bond_detail": ("bond_info_detail_cm", {"symbol": "26河北23"}),
    "closing_curve": ("bond_china_close_return", {"symbol": "国债", "period": "1", "start_date": "20260911", "end_date": "20260911"}),
    "exchange_history": ("bond_zh_hs_daily", {"symbol": "sh101900"}),
}
SUPPLEMENTAL_CASES = {
    "bond_types": ("bond_info_cm_query", {"symbol": "债券类型"}),
    "bond_lookup_typed": ("bond_info_cm", {"bond_code": "809336", "bond_type": "地方政府债"}),
    "exchange_history_reference": ("bond_zh_hs_daily", {"symbol": "sh010107"}),
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def is_allowed(url):
    p = urlsplit(url)
    if p.hostname == "yield.chinabond.com.cn":
        return p.path in ("/cbweb-pbc-web/pbc/historyQuery", "/cbweb-mn/indices/singleIndexQueryResult", "/cbweb-mn/indices/singleIndexQuery")
    if p.hostname == "www.chinamoney.com.cn":
        return p.path.startswith("/ags/ms/")
    if p.hostname == "webapi.cninfo.com.cn":
        return p.path in ("/api/sysapi/p_sysapi1120", "/api/sysapi/p_sysapi1121")
    if p.hostname == "finance.sina.com.cn":
        return any(p.path.startswith(f"/realstock/company/{symbol}/") for symbol in ("sh101900", "sh010107"))
    return False


def run_case(name):
    import requests
    import akshare as ak

    folder = OUT / name
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "result.json").exists():
        raise SystemExit("Case result already exists; preserve the recorded run.")
    network = []
    denied_hosts = set()
    original_send = requests.Session.send
    started = time.monotonic()

    def tracked_send(session, request, **kwargs):
        host = urlsplit(request.url).hostname
        entry = {"sequence": len(network) + 1, "method": request.method, "url": request.url}
        if not is_allowed(request.url) or host in denied_hosts or len(network) >= 5:
            entry["blocked_before_send"] = True
            entry["reason"] = "Outside public-query allowlist, denied host, or per-case request budget"
            network.append(entry)
            save(folder / "network.json", network)
            raise RuntimeError(entry["reason"])
        network.append(entry)
        kwargs["timeout"] = (5, 15)
        kwargs["verify"] = True
        request_start = time.monotonic()
        try:
            response = original_send(session, request, **kwargs)
            entry.update(status=response.status_code, content_type=response.headers.get("content-type"), bytes=len(response.content))
            suffix = ".json" if "json" in response.headers.get("content-type", "") else ".txt"
            filename = f"response-{entry['sequence']:02d}{suffix}"
            (folder / filename).write_bytes(response.content)
            entry["body_file"] = filename
            if response.status_code in (401, 403, 429):
                denied_hosts.add(host)
            return response
        except Exception as error:
            entry.update(error_type=type(error).__name__, error=str(error)[:300])
            raise
        finally:
            entry["elapsed_ms"] = round((time.monotonic() - request_start) * 1000)
            save(folder / "network.json", network)

    original_init = requests.Session.__init__

    def session_init(session):
        original_init(session)
        session.trust_env = False

    requests.Session.__init__ = session_init
    requests.Session.send = tracked_send
    function_name, arguments = (CASES | SUPPLEMENTAL_CASES)[name]
    function = getattr(ak, function_name)
    source_path = Path(inspect.getsourcefile(inspect.unwrap(function)))
    result = {
        "case": name, "function": function_name, "arguments": arguments,
        "akshare_version": ak.__version__, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "function_signature": str(inspect.signature(function)),
        "source_file": str(source_path.relative_to(Path(ak.__file__).parent)),
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "transport_controls": "requests hook records raw response, enforces public-query allowlist and 5 sends/case, sets timeout and disables environmental proxies; SDK parser unchanged",
    }
    try:
        frame = function(**arguments)
        if not hasattr(frame, "to_json"):
            raise TypeError(f"Unexpected return type {type(frame).__name__}")
        frame.to_csv(folder / "data.csv", index=False, encoding="utf-8-sig")
        records = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
        save(folder / "data.json", records)
        result.update(status="returned_data" if len(frame) else "empty", rows=len(frame),
                      columns=[str(c) for c in frame.columns],
                      non_null_counts={str(c): int(frame[c].notna().sum()) for c in frame.columns},
                      sample=records[:2], date_ranges={})
        for col in frame.columns:
            if "日期" in str(col) or str(col) in ("date", "发行起始日", "发行终止日"):
                values = frame[col].dropna().astype(str)
                if len(values):
                    result["date_ranges"][str(col)] = {"min": values.min(), "max": values.max()}
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error)[:1000])
        (folder / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        requests.Session.send = original_send
        requests.Session.__init__ = original_init
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        result["http_requests_sent"] = sum(not n.get("blocked_before_send") for n in network)
        result["network"] = network
        save(folder / "result.json", result)
    print(json.dumps({k: v for k, v in result.items() if k in ("case", "status", "rows", "columns", "error", "http_requests_sent")}, ensure_ascii=True), flush=True)


def run_all(entry_script=None):
    import akshare as ak
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "manifest.json").exists():
        raise SystemExit("Results already exist; inspect saved evidence instead of rerunning requests.")
    dependencies = {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}
    manifest = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "akshare_version": ak.__version__,
                "python": sys.version, "paid_calls": 0, "cases": CASES,
                "per_case_http_limit": 5, "per_case_wall_timeout_seconds": 50, "dependencies": dependencies}
    save(OUT / "manifest.json", manifest)
    registry = {}
    for query in ("中债估值", "地方债", "国债", "久期", "收益率"):
        frame = ak.search(query, limit=30)
        registry[query] = json.loads(frame.to_json(orient="records", force_ascii=False))
    save(OUT / "offline-interface-search.json", registry)
    results = []
    for name in CASES:
        folder = OUT / name
        folder.mkdir(exist_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with (folder / "process.log").open("w", encoding="utf-8") as log:
            try:
                completed = subprocess.run([sys.executable, entry_script or __file__, "--case", name], stdout=log, stderr=subprocess.STDOUT,
                                           timeout=50, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if not (folder / "result.json").exists():
                    save(folder / "result.json", {"case": name, "status": "process_failed", "exit_code": completed.returncode})
            except subprocess.TimeoutExpired:
                save(folder / "result.json", {"case": name, "status": "timeout", "wall_timeout_seconds": 50})
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        results.append(result)
        save(OUT / "results.json", results)
        print(json.dumps({k: result.get(k) for k in ("case", "status", "rows", "error", "http_requests_sent")}, ensure_ascii=True), flush=True)
    manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["http_requests_sent"] = sum(r.get("http_requests_sent", 0) for r in results)
    save(OUT / "manifest.json", manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASES | SUPPLEMENTAL_CASES)
    args = parser.parse_args()
    run_case(args.case) if args.case else run_all()
