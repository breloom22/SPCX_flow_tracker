from datetime import date

from src.mechanical import index_flows as a1
from tests.fixtures import make_cfg


def test_float_shares():
    cfg = make_cfg()
    # 100B / $100 = 10억주
    assert a1._float_shares(cfg) == 1_000_000_000


def test_compute_inclusion_base_math():
    cfg = make_cfg()
    r = a1.compute_inclusion(cfg)
    # float시총 = 1e9 * 100 = 100B; weighting = 100B*3 = 300B
    assert r.float_mcap_usd == 100_000_000_000
    assert r.weighting_mcap_usd == 300_000_000_000
    # weight = 300B / (30T + 300B)
    expected_w = 300e9 / (30_000e9 + 300e9)
    assert abs(r.weight_pct - expected_w) < 1e-12
    # forced_buy = weight * 600B
    assert abs(r.forced_buy_usd - expected_w * 600e9) < 1.0


def test_multiplier_monotonic():
    cfg = make_cfg()
    r1 = a1.compute_inclusion(cfg, multiplier=1.0)
    r3 = a1.compute_inclusion(cfg, multiplier=3.0)
    assert r3.forced_buy_usd > r1.forced_buy_usd


def test_sensitivity_matrix_scenarios():
    cfg = make_cfg()
    m = a1.sensitivity_matrix(cfg)
    names = {r.scenario for r in m}
    for expected in ("base", "mult_1x", "mult_2x", "mult_3x",
                     "price_+20", "price_-20", "float_+30", "float_-30"):
        assert expected in names


def test_funded_sell_sorted_and_proportional():
    cfg = make_cfg()
    base = a1.compute_inclusion(cfg)
    sells = a1.funded_sell_by_constituent(cfg, base.forced_buy_usd)
    # 내림차순 정렬
    vals = [s["est_sell_usd"] for s in sells]
    assert vals == sorted(vals, reverse=True)
    # NVDA(0.09)가 최상위, 매도액 = weight * forced_buy
    assert sells[0]["ticker"] == "NVDA"
    assert abs(sells[0]["est_sell_usd"] - 0.09 * base.forced_buy_usd) < 1.0


def test_reinforcing_loop_converges_and_grows():
    cfg = make_cfg()
    loop = a1.reinforcing_loop(cfg)
    assert len(loop) >= 1
    # 가격은 단조 증가 (양의 임팩트)
    prices = [r["price_usd"] for r in loop]
    assert all(prices[i] <= prices[i + 1] + 1e-6 for i in range(len(prices) - 1))
    # 누적 매수 비음수, float 대비 비율 산출
    assert loop[-1]["cumulative_buy_usd"] >= 0
    assert loop[-1]["cum_buy_to_float_pct"] >= 0
