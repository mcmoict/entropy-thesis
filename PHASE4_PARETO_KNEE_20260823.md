# Phase 4 Pareto-Knee Lambda Selection Revision — 2026-08-23

## 1. 연구 문제 변경

기존 Phase 4E는 Calibration 날짜에서 `mean_flow_time_seconds` 평균이 가장 작은 λ를 선택했다. 이 규칙은 엔트로피 기반 배치가 의도적으로 줄이려는 혼잡을 λ 선택 단계에서 직접 평가하지 못한다.

이번 개정의 연구 질문은 다음과 같다.

```text
작업처리 효율성을 과도하게 희생하지 않으면서
작업자 공간 집중과 혼잡을 얼마나 줄일 수 있는가?
```

따라서 정수 배치 목적함수 `J(n;λ)=D(n)+λR(n)`은 우선 유지하고, Phase 4D/E의 λ 평가/선택 규칙을 다목적으로 변경한다. 이 결과를 먼저 확인한 뒤 `D(n)`을 작업부하/처리부하 기반 `L(n)`으로 교체할지 결정한다.

## 2. Phase 4D 다목적 평가

Calibration 92일에서 각 λ의 날짜 평균을 계산한다.

```text
Flow(λ)      = mean(mean_flow_time_seconds)
Conflict(λ)  = mean(congestion_conflicts)
Wait(λ)      = mean(congestion_wait_seconds)
Cong(λ)      = mean(congestion_delay_ratio)
```

네 값은 모두 작을수록 좋다. 다른 λ가 네 값에서 모두 같거나 작고 적어도 하나에서 더 작으면 해당 λ는 dominated point로 판정한다. 지배되지 않는 λ만 4-objective Pareto frontier로 표시한다.

## 3. Knee point 계산

세 혼잡 KPI는 각각 λ=0 평균으로 정규화한다.

```text
I_cong(λ) = 1/3 × [
    Conflict(λ) / Conflict(0)
  + Wait(λ)     / Wait(0)
  + Cong(λ)     / Cong(0)
]
```

`I_cong(0)=1`이며 작을수록 λ=0보다 종합 혼잡이 낮다. 4-objective Pareto 후보 중 `(Flow, I_cong)`에서도 지배되지 않는 점을 knee frontier로 사용한다.

Flow와 `I_cong`를 각각 0~1로 min-max 정규화한 뒤, best-flow endpoint와 best-congestion endpoint를 잇는 chord를 만든다. 각 Pareto point가 이 chord에서 **ideal point (0,0) 방향으로 떨어진 수직거리**를 `knee_score`로 정의하고 가장 큰 점을 λ*로 선택한다. 동률이면 더 작은 λ를 선택한다.

## 4. 기존 92일 결과에 대한 사전 적용

첨부 소스에 포함된 기존 `results/phase4/phase4_daily_results.csv` 920행(92일 × 10개 λ)을 새 선택 규칙으로 재분석하면 다음과 같다.

| λ | Flow(s) | Flow Δ vs λ=0 | Conflicts | Conflict 감소 | Wait(s) | Wait 감소 | Congestion | Congestion 감소 | Knee score |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 440.89 | 0.00% | 146.54 | 0.00% | 320.80 | 0.00% | 8.05% | 0.00% | 0.0000 |
| 0.05 | 443.67 | +0.63% | 144.32 | 1.52% | 307.01 | 4.30% | 7.87% | 2.31% | 0.0334 |
| 0.10 | 447.51 | +1.50% | 139.91 | 4.52% | 292.93 | 8.69% | 7.66% | 4.84% | 0.0721 |
| **0.25** | **462.13** | **+4.82%** | **129.84** | **11.40%** | **248.11** | **22.66%** | **6.88%** | **14.57%** | **0.1769** |
| 0.50 | 488.25 | +10.74% | 120.45 | 17.81% | 225.34 | 29.76% | 6.34% | 21.30% | 0.1629 |
| 0.75 | 506.03 | +14.78% | 114.84 | 21.64% | 211.24 | 34.15% | 5.86% | 27.24% | 0.1557 |
| 1 | 523.74 | +18.79% | 109.62 | 25.20% | 200.22 | 37.59% | 5.55% | 31.02% | 0.1290 |
| 2 | 558.78 | +26.74% | 100.91 | 31.14% | 179.95 | 43.90% | 5.15% | 36.03% | 0.0525 |
| 4 | 574.47 | +30.30% | 96.87 | 33.90% | 173.33 | 45.97% | 4.95% | 38.48% | 0.0155 |
| 8 | 580.41 | +31.65% | 95.37 | 34.92% | 170.89 | 46.73% | 4.90% | 39.20% | 0.0000 |

새 규칙에서는 **λ*=0.25**가 knee point이다. λ=0.25까지는 약 4.8%의 Flow Time 증가로 Conflict 약 11.4%, Wait 약 22.7%, Congestion ratio 약 14.6%를 줄이지만, λ=0.5부터 Flow Time 손실이 10%를 넘으면서 혼잡 개선의 추가 한계효용이 둔화된다.

이 패턴이 재실행 후에도 유지된다면 현재 `D(n)`을 즉시 폐기하기보다, 먼저 λ=0.25를 고정하여 Phase 5 Holdout에서 효율성-혼잡 trade-off가 재현되는지 검증하는 것이 타당하다. Holdout에서 trade-off가 무너지거나 작업량이 큰 날짜에서 Flow Time 페널티가 과도하면 그때 `D(n)`을 처리부하 기반 `L(n)`으로 개정한다.

## 5. 신규 출력

```text
results/phase4/phase4_pareto_analysis.csv
```

주요 컬럼:

```text
entropy_weight
mean_flow_time_seconds
flow_time_change_vs_lambda0_pct
congestion_conflicts
conflicts_reduction_vs_lambda0_pct
congestion_wait_seconds
wait_reduction_vs_lambda0_pct
congestion_delay_ratio
congestion_percent
congestion_reduction_vs_lambda0_pct
congestion_index_lambda0
composite_congestion_reduction_vs_lambda0_pct
pareto_frontier
knee_frontier
flow_normalized
congestion_index_normalized
knee_score
selected_knee
```

## 6. 실행

기본 실행은 Pareto-knee 선택이다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw
```

기존 Mean Flow Time 단일 KPI 선택을 재현하려면 다음과 같이 실행한다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --selection-rule single_metric --selection-metric mean_flow_time_seconds
```

새 model revision:

```text
2026-08-22-cc08-inch-micro20-macro4-integer-objective-v1-pareto-knee-v1
```
