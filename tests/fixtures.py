"""테스트용 고정 config fixture (네트워크/실제 yaml 비의존)."""
from datetime import date


def make_cfg() -> dict:
    return {
        "ticker": "SPCX",
        "exchange": "NASDAQ",
        "company_name": "Space Exploration Technologies Corp.",
        "ipo": {
            "pricing_date": date(2026, 6, 11),
            "listing_date": date(2026, 6, 12),
            "offer_price_usd": 100.0,          # 계산 검증을 쉽게 하려 100 사용
            "free_float_usd": 100_000_000_000, # → float 10억주
            "proceeds_usd": 75_000_000_000,
            "source": "test",
        },
        "shares": {"total_shares_outstanding": 10_000_000_000},
        "nasdaq100": {
            "fast_entry_trading_days": 15,
            "low_float_multiplier_max": 3.0,
            "weighting_float_usd": 100_000_000_000,
            "index_total_mcap_usd": 30_000_000_000_000,  # $30T
        },
        "tracking_aum": {"total_estimate_usd": 600_000_000_000},  # $600B
        "lockup": {
            "earnings_date_estimate": date(2026, 8, 14),
            "earnings_date_confirmed": False,
            "source": "test",
            "reported_tranches": [
                {"id": "musk_366d", "holder_group": "founder_musk",
                 "classification": "discretionary", "trigger_type": "absolute_days",
                 "trigger_ref": 366, "release_fraction": None,
                 "condition": "366일", "confidence": "rule_based", "needs_review": True,
                 "source": "press"},
                {"id": "insider_q1", "holder_group": "insiders",
                 "classification": "unknown", "trigger_type": "earnings_relative",
                 "trigger_ref": 2, "release_fraction": 0.10,
                 "condition": "실적+2거래일", "confidence": "rule_based",
                 "needs_review": True, "source": "press"},
            ],
        },
        "sp500": {"note": "test"},
        "holders": [
            {"name": "Alphabet", "group": "strategic", "classification": "discretionary",
             "est_pct": 0.07, "has_redemption_obligation": False, "source": "press"},
            {"name": "VC Growth", "group": "vc", "classification": "forced_seller",
             "est_pct": None, "has_redemption_obligation": True, "source": "seed"},
        ],
        "nasdaq100_top_constituents": {
            "members": [
                {"ticker": "NVDA", "weight": 0.09},
                {"ticker": "AAPL", "weight": 0.08},
                {"ticker": "MSFT", "weight": 0.07},
            ],
        },
        "self_reinforcing": {
            "price_impact_coeff": 0.10, "max_iterations": 25,
            "convergence_tol_usd": 100_000_000,
        },
    }
