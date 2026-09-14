"""Supplement the bond verification with actual ChinaBond index SDK calls."""
import argparse
import verify_akshare_bonds as runner

runner.OUT = runner.ROOT / "research" / "akshare-index-validation-20260912"
runner.CASES = {
    "catalog": ("bond_available_index_cbond", {}),
    "local_wealth": ("bond_index_general_cbond", {"index_category": "地方政府债指数", "indicator": "财富", "period": "总值"}),
    "local_yield": ("bond_index_general_cbond", {"index_category": "地方政府债指数", "indicator": "平均市值法到期收益率", "period": "总值"}),
    "local_duration": ("bond_index_general_cbond", {"index_category": "地方政府债指数", "indicator": "平均市值法久期", "period": "总值"}),
    "zhejiang_yield": ("bond_index_general_cbond", {"index_category": "浙江省地方政府债指数", "indicator": "平均市值法到期收益率", "period": "总值"}),
    "treasury_yield": ("bond_index_general_cbond", {"index_category": "固定利率国债指数", "indicator": "平均市值法到期收益率", "period": "总值"}),
}
runner.SUPPLEMENTAL_CASES = {}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=runner.CASES)
    args = parser.parse_args()
    runner.run_case(args.case) if args.case else runner.run_all(__file__)
