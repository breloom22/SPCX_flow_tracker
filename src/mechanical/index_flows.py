"""A1 — 나스닥100 인덱스 편입 계산기.

가정(assumptions) — 모든 출력의 confidence는 'deterministic'(계산)이나, *입력*이 추정이면
그 추정 출처를 source에 명시한다. 핵심 가정:
  1. 나스닥100 가중치 ≈ (float시총 × 저유통 멀티플라이어) / 지수 전체 시총.
     - 실제 나스닥100은 modified market-cap 방식 + 분기 캡 조정이 있으나, 신규 편입 시점의
       1차 근사로 위 식을 사용한다. (정교화는 추후)
  2. 편입 매수액 = 가중치 × 나스닥100 추종 AUM(추정).
  3. 펀딩 매도: 패시브 펀드는 신규 종목 매수 재원을 기존 구성종목을 비중대로 매도해 마련한다고 가정
     → 종목별 매도액 ≈ 해당 종목 현재가중치 × 총 편입 매수액.
  4. float 주식수 = free_float_usd / offer_price (config). 가격 변동 시 float 시총만 변하고 주식수는 고정.

confidence 규칙(스펙 §1.1): 계산 자체는 deterministic, 단 input AUM/지수시총이 추정이므로
리포트에서는 "deterministic 계산 / rule_based 입력"으로 각주 처리.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date

from ..config import load_spcx


@dataclass
class InclusionResult:
    scenario: str
    price_usd: float
    float_shares: float
    float_mcap_usd: float
    multiplier: float
    weighting_mcap_usd: float
    weight_pct: float
    tracking_aum_usd: float
    forced_buy_usd: float


def _float_shares(cfg: dict) -> float:
    """float 주식수 = free_float_usd / offer_price."""
    ipo = cfg["ipo"]
    return ipo["free_float_usd"] / ipo["offer_price_usd"]


def compute_inclusion(
    cfg: dict,
    *,
    price_usd: float | None = None,
    float_shares: float | None = None,
    multiplier: float | None = None,
    scenario: str = "base",
) -> InclusionResult:
    """단일 시나리오의 편입 매수 추정."""
    ipo = cfg["ipo"]
    n100 = cfg["nasdaq100"]
    aum = cfg["tracking_aum"]["total_estimate_usd"]

    price = price_usd if price_usd is not None else ipo["offer_price_usd"]
    fshares = float_shares if float_shares is not None else _float_shares(cfg)
    mult = multiplier if multiplier is not None else n100["low_float_multiplier_max"]

    float_mcap = price * fshares
    weighting_mcap = float_mcap * mult
    index_total = n100["index_total_mcap_usd"]
    # SPCX를 분모에 포함 (편입 후 지수)
    weight = weighting_mcap / (index_total + weighting_mcap)
    forced_buy = weight * aum

    return InclusionResult(
        scenario=scenario,
        price_usd=price,
        float_shares=fshares,
        float_mcap_usd=float_mcap,
        multiplier=mult,
        weighting_mcap_usd=weighting_mcap,
        weight_pct=weight,
        tracking_aum_usd=aum,
        forced_buy_usd=forced_buy,
    )


def sensitivity_matrix(cfg: dict) -> list[InclusionResult]:
    """민감도 분석: 주가 ±20%, float ±30%, 멀티플라이어 1x/2x/3x.

    base 1건 + 각 축 변형. 멀티플라이어는 3개 시나리오 모두.
    """
    base_price = cfg["ipo"]["offer_price_usd"]
    base_float = _float_shares(cfg)
    results: list[InclusionResult] = []

    # base (멀티플라이어 max)
    results.append(compute_inclusion(cfg, scenario="base"))

    # 멀티플라이어 시나리오
    for m in (1.0, 2.0, 3.0):
        results.append(compute_inclusion(cfg, multiplier=m, scenario=f"mult_{int(m)}x"))

    # 주가 ±20%
    for pct, tag in ((1.20, "price_+20"), (0.80, "price_-20")):
        results.append(compute_inclusion(cfg, price_usd=base_price * pct, scenario=tag))

    # float ±30%
    for pct, tag in ((1.30, "float_+30"), (0.70, "float_-30")):
        results.append(compute_inclusion(cfg, float_shares=base_float * pct, scenario=tag))

    return results


def funded_sell_by_constituent(cfg: dict, forced_buy_usd: float) -> list[dict]:
    """편입 매수 재원 마련을 위한 기존 구성종목별 예상 매도액 테이블.

    sell_i ≈ current_weight_i × forced_buy_usd. (비례 재조정 가정)
    이 리스트가 C모듈 '선행 매매 탐지' 대상 종목이 된다.
    """
    members = cfg["nasdaq100_top_constituents"]["members"]
    rows = []
    for m in members:
        rows.append({
            "ticker": m["ticker"],
            "current_weight": m["weight"],
            "est_sell_usd": m["weight"] * forced_buy_usd,
        })
    rows.sort(key=lambda r: r["est_sell_usd"], reverse=True)
    return rows


def reinforcing_loop(cfg: dict) -> list[dict]:
    """자기강화 루프 시뮬레이터.

    편입 매수 → 가격 상승 → float시총↑ → 가중치↑ → 추가 매수 … 반복.
    단순 선형 가격임팩트 가정:
        Δprice% = coeff × (이번 라운드 순매수액 / 직전 float시총)
    누적 패시브 매수 / float시총 비율을 출력한다.

    가정: 패시브 매수가 전량 시장에서 체결되어 가격을 끌어올린다(상한 없음). 단순화 모델.
    """
    sr = cfg["self_reinforcing"]
    coeff = sr["price_impact_coeff"]
    max_iter = sr["max_iterations"]
    tol = sr["convergence_tol_usd"]

    price = cfg["ipo"]["offer_price_usd"]
    fshares = _float_shares(cfg)
    mult = cfg["nasdaq100"]["low_float_multiplier_max"]
    index_total = cfg["nasdaq100"]["index_total_mcap_usd"]
    aum = cfg["tracking_aum"]["total_estimate_usd"]

    rows: list[dict] = []
    cumulative_buy = 0.0
    prev_required_holding = 0.0

    for i in range(1, max_iter + 1):
        float_mcap = price * fshares
        weighting_mcap = float_mcap * mult
        weight = weighting_mcap / (index_total + weighting_mcap)
        required_holding = weight * aum            # 패시브가 보유해야 할 총액
        round_buy = required_holding - prev_required_holding  # 이번 라운드 추가 매수
        if round_buy < 0:
            round_buy = 0.0
        cumulative_buy += round_buy

        rows.append({
            "iteration": i,
            "price_usd": price,
            "float_mcap_usd": float_mcap,
            "weight_pct": weight,
            "cumulative_buy_usd": cumulative_buy,
            "cum_buy_to_float_pct": cumulative_buy / float_mcap if float_mcap else 0.0,
        })

        if i > 1 and round_buy < tol:
            break

        # 가격 임팩트 적용 (다음 라운드 가격)
        price = price * (1 + coeff * (round_buy / float_mcap if float_mcap else 0.0))
        prev_required_holding = required_holding

    return rows


def to_db_rows(results: list[InclusionResult], as_of: date,
               confidence: str, source: str, created_at) -> list[dict]:
    rows = []
    for r in results:
        d = asdict(r)
        d.update({
            "as_of_date": as_of,
            "confidence": confidence,
            "source": source,
            "created_at": created_at,
        })
        rows.append(d)
    return rows


# 간단 자체 점검 (python -m src.mechanical.index_flows)
if __name__ == "__main__":
    cfg = load_spcx()
    base = compute_inclusion(cfg)
    print(f"[base] weight={base.weight_pct*100:.3f}%  forced_buy=${base.forced_buy_usd/1e9:.2f}B")
    for r in sensitivity_matrix(cfg):
        print(f"  {r.scenario:12s} w={r.weight_pct*100:6.3f}%  buy=${r.forced_buy_usd/1e9:6.2f}B")
    loop = reinforcing_loop(cfg)
    last = loop[-1]
    print(f"[loop] {len(loop)} iters  cum_buy=${last['cumulative_buy_usd']/1e9:.2f}B  "
          f"cum_buy/float={last['cum_buy_to_float_pct']*100:.2f}%")
