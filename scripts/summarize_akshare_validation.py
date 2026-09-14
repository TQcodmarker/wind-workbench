"""Audit saved AKShare verification responses offline; no network or business writes."""
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "akshare-validation-20260912"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def equal_number(a, b):
    return number(a) == number(b)


local = read(ROOT / "outputs" / "20260911-Excel全部债券取数结果.json")
by_code = {row["excelCode"].upper(): row for row in local["bonds"]}
deals = read(OUT / "spot_deals" / "response-01.json")
sdk_deals = read(OUT / "spot_deals" / "data.json")
groups = defaultdict(list)
for row in deals["records"]:
    groups[str(row["bondcode"]) + ".IB"].append(row)
matched = sorted(set(groups) & set(by_code))
core = ("dmiLatestRate", "dmiLatestContraRate", "dmiWghtdContraRate", "showDate", "isinCode", "termToMaturity")
conflicts = [code for code, rows in groups.items() if len({tuple(r.get(k) for k in core) for r in rows}) > 1]
mapping = {"成交净价": "dmiLatestRate", "最新收益率": "dmiLatestContraRate",
           "涨跌": "bpNum", "加权收益率": "dmiWghtdContraRate", "交易量": "dmiTtlTradedAmnt"}
parser_mismatches = {col: sum(not equal_number(a.get(col), b.get(key)) for a, b in zip(sdk_deals, deals["records"]))
                     for col, key in mapping.items()}
parser_mismatches["债券简称"] = sum(a["债券简称"] != b["abdAssetEncdShrtDesc"]
                                    for a, b in zip(sdk_deals, deals["records"]))
coverage = {
    "local_code_count": len(by_code), "local_target_date": local["targetDate"],
    "match_rule": "raw interbank bondcode + .IB equals local excelCode; no name or cross-market inference",
    "raw_rows": len(deals["records"]), "unique_codes": len(groups),
    "duplicate_code_groups": sum(len(v) > 1 for v in groups.values()),
    "conflicting_core_value_codes": conflicts,
    "matched_unique_codes": len(matched), "matched_percent": round(100 * len(matched) / len(by_code), 4),
    "matched_duplicate_code_groups": sum(len(groups[c]) > 1 for c in matched),
    "matched_conflicting_codes": sorted(set(matched) & set(conflicts)),
    "matched_with_latest_yield": sum(any(number(r.get("dmiLatestContraRate")) is not None for r in groups[c]) for c in matched),
    "matched_previously_fetched": sum(bool(by_code[c]["matched"]) for c in matched),
    "matched_previously_not_fetched": sum(not bool(by_code[c]["matched"]) for c in matched),
    "source_display_date": deals["data"].get("showDateCN"),
    "matched_trade_dates": sorted({str(r["showDate"])[:10] for c in matched for r in groups[c]}),
    "sdk_raw_row_count_equal": len(sdk_deals) == len(deals["records"]),
    "sdk_vs_raw_value_mismatches": parser_mismatches,
    "not_proven": "Required-field completeness, original-rule inclusion, full AKShare coverage, historical coverage, real-time latency, or ChinaBond per-security valuations",
}
save("coverage.json", coverage)
save("matched-deals.json", [{"excelCode": c, **{k: groups[c][0].get(k) for k in ("abdAssetEncdFullDescByRmb", *core)}} for c in matched])
if coverage["matched_conflicting_codes"]:
    raise ValueError("Do not export deduplicated matches with conflicting values")
with (OUT / "matched-deals.csv").open("w", encoding="utf-8-sig", newline="") as stream:
    writer = csv.writer(stream)
    writer.writerow(["清单代码", "来源债券名称", "ISIN", "成交净价", "最新成交收益率(%)", "加权成交收益率(%)", "来源行情时间", "来源", "此前已有Wind资料"])
    for code in matched:
        row = groups[code][0]
        writer.writerow([code, row["abdAssetEncdFullDescByRmb"], row["isinCode"], row["dmiLatestRate"],
                         row["dmiLatestContraRate"], row["dmiWghtdContraRate"], row["showDate"],
                         "中国货币网公开成交列表；AKShare调用时保存的原始响应", bool(by_code[code]["matched"])])

curve = read(OUT / "closing_curve" / "response-02.json")
curve_data = read(OUT / "closing_curve" / "data.json")
quotes = read(OUT / "spot_quotes" / "response-02.json")
issues = {}
for case in ("local_issues", "treasury_issues"):
    rows = read(OUT / case / "data.json")
    issues[case] = {
        "rows": len(rows), "unique_market_code_pairs": len({(r["交易市场"], r["债券代码"]) for r in rows}),
        "unique_name_date_combinations_not_verified_entities": len({(r["债券名称"], r["发行起始日"], r["发行终止日"]) for r in rows}),
        "markets": dict(Counter(r["交易市场"] for r in rows)),
        "exact_local_interbank_matches": sorted({r["债券代码"] + ".IB" for r in rows
                                               if r["交易市场"] == "银行间债券市场" and r["债券代码"] + ".IB" in by_code}),
        "invalid_date_order_rows": [r for r in rows if r["发行起始日"] > r["发行终止日"]],
        "missing_issue_price_rows": sum(r["发行价格"] is None for r in rows),
        "reissue_rows": sum((r["增发次数"] or 0) > 0 for r in rows),
    }
all_results = [read(path) for path in sorted(OUT.glob("*/result.json"))]
audit = {
    "akshare_version": "1.18.94", "test_cases": len(all_results),
    "statuses": dict(Counter(r["status"] for r in all_results)),
    "http_requests_sent": sum(r.get("http_requests_sent", 0) for r in all_results),
    "paid_calls": 0,
    "results": [{k: r.get(k) for k in ("case", "function", "arguments", "status", "rows", "error")} for r in all_results],
    "closing_curve": {"returned_rows": len(curve_data), "raw_pagination": curve["data"],
                      "available_required_tenors": [r for r in curve_data if r["期限"] in (3, 5, 7, 10, 15, 20, 30)],
                      "max_returned_tenor": max(r["期限"] for r in curve_data)},
    "spot_quotes": {"returned_rows": len(quotes["records"]), "raw_pagination": quotes["data"]},
    "issuance": issues,
}
save("audit.json", audit)
print(json.dumps({"test_cases": audit["test_cases"], "statuses": audit["statuses"],
                  "http_requests": audit["http_requests_sent"], "coverage": coverage}, ensure_ascii=True))
