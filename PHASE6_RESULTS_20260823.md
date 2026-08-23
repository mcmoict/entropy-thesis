# Phase 6 결과 요약 - 2026-08-23

## 1. 최종 실험 조건

```text
Phase 4 selection rule : Pareto knee
Selected λ*            : 0.25
Calibration dates      : 92
Frozen Holdout dates   : 40
Phase 5 completed      : 40 / 40
Skipped                : 0
Volume basis           : tasks
```

Phase 5와 Phase 6에서는 λ를 재선택하지 않았고 Holdout 날짜도 다시 샘플링하지 않았다.

## 2. Phase 5 Holdout 평균

| Method | Mean Flow Time (s) | Conflicts | Wait (s) | Congestion Ratio |
|---|---:|---:|---:|---:|
| Baseline | 794.815 | 103.375 | 180.973 | 5.042% |
| Random | 762.637 | 138.975 | 304.601 | 7.327% |
| Equal | 858.625 | 116.050 | 244.597 | 6.300% |
| Volume | **680.607** | 155.475 | 345.341 | 8.484% |
| Entropy (λ=0.25) | 699.910 | 148.475 | 306.271 | 7.987% |

핵심적으로 Volume은 처리효율이 가장 좋지만 혼잡이 가장 큰 축에 있고, Entropy는 Volume의 일부 처리효율을 양보하면서 혼잡을 완화하는 중간 지점으로 나타났다.

Aggregate mean 기준 Entropy는 Baseline보다 Mean Flow Time이 약 **11.94% 짧고**, Volume보다는 약 **2.84% 길다**. Baseline→Volume이 얻는 Flow Time 개선폭의 약 **83.10%를 유지**하면서 Volume 대비 Conflicts **4.50%**, Wait **11.31%**, Congestion Ratio **5.85%**를 줄였다.

## 3. Calibration → Holdout trade-off

| Split | Flow Time Cost | Conflicts Reduction | Wait Reduction | Congestion Ratio Reduction | Composite Congestion Reduction |
|---|---:|---:|---:|---:|---:|
| Calibration | +4.82% | +11.40% | +22.66% | +14.57% | +16.21% |
| Holdout | +2.84% | +4.50% | +11.31% | +5.85% | +7.22% |

부호 정의:

```text
Flow Time Cost > 0       : Entropy가 Volume/λ=0보다 느림
Congestion Reduction > 0 : Entropy가 혼잡을 줄임
```

Calibration과 Holdout 모두 **효율 손실 + 혼잡 감소**라는 Pareto trade-off의 방향은 동일했다. 다만 Holdout에서는 혼잡 감소 폭이 Calibration보다 작아졌다.

## 4. Entropy vs Volume - 4개 Pareto KPI 통계

| KPI | Mean 기준 개선 | W/T/L | Wilcoxon p | Holm p | Paired Bootstrap 95% CI* |
|---|---:|---:|---:|---:|---:|
| Mean Flow Time | -2.836% | 1/32/7 | 0.0173 | **0.0692** | [-37.69, -4.30] s |
| Conflicts | +4.502% | 6/32/2 | 0.0929 | 0.2787 | [-0.40, 17.15] |
| Wait | +11.314% | 6/32/2 | 0.1235 | 0.2787 | [-1.16, 109.00] s |
| Congestion Ratio | +5.854% | 6/32/2 | 0.2076 | 0.2787 | [-0.00174, 0.01492] |

`*` Bootstrap CI는 Entropy 개선 방향을 양수로 통일한 native difference이다.

중요한 해석은 다음과 같다.

- 보정 전 Wilcoxon에서는 Flow Time 악화가 `p=0.0173`으로 보이지만, Phase 4의 4개 Pareto KPI를 하나의 family로 보고 Holm 보정하면 `p=0.0692`가 되어 0.05 기준을 넘는다.
- Conflicts / Wait / Congestion Ratio는 평균 방향은 모두 개선이지만 40일 전체 paired 검정에서는 통계적 유의성이 확인되지 않았다.
- 40일 중 32일은 Entropy와 Volume의 정수 작업자 배치가 완전히 같아 KPI도 동일하다. 따라서 실질적인 차이는 8일에서 발생한다.

## 5. 실제 정수 작업자 배치가 달라진 날짜

```text
2023-07-24
2023-07-25
2023-08-10
2023-08-24
2023-09-25
2023-10-05
2023-10-06
2023-10-18
```

총 **8/40일**에서 Entropy가 Volume과 다른 정수 worker vector를 선택했다.

메커니즘 검증 결과:

```text
8/8 : J_entropy <= J_volume
8/8 : 작업자를 더 높은 concentration Zone에서 더 낮은 concentration Zone 방향으로 이동
6/8 : 실제 DES composite congestion도 개선
```

날짜별 분류:

```text
congestion_gain_efficiency_cost : 6일
efficiency_gain_congestion_cost : 1일
loss_loss                       : 1일
```

즉 새 엔트로피 항은 더 이상 단순한 연속 score에 머무르지 않고 **작업자 한 명 이상의 실제 정수 Zone 배치를 바꾸는 역할**을 수행했다.

## 6. R(n) congestion-risk proxy 진단

배치가 달라진 8일에서 `R_volume - R_entropy`와 실제 DES 혼잡 감소량의 상관을 exploratory하게 확인했다.

| Target | Pearson r | p | Spearman ρ | p |
|---|---:|---:|---:|---:|
| Conflict reduction | 0.731 | 0.039 | 0.452 | 0.260 |
| Wait reduction | 0.867 | 0.005 | 0.429 | 0.289 |
| Congestion Ratio reduction | 0.900 | 0.002 | 0.500 | 0.207 |

Pearson 기준으로는 `R` 감소가 실제 혼잡 감소와 비교적 강한 선형 관계를 보였지만, 표본이 8일뿐이고 Spearman 결과는 유의하지 않다. 따라서 논문에서는 **목적함수 proxy의 방향성이 DES 결과와 대체로 일치한다는 보조적 메커니즘 근거**로만 사용하고, 확증적 증거로 과장하지 않는 것이 적절하다.

반면 `D` 증가량과 Flow Time 증가량의 관계는 강하지 않았다(Pearson r≈0.437, Spearman ρ≈0.238). 즉 demand-fit `D`는 Flow Time의 완전한 직접 proxy가 아니라 배치 적합도를 나타내는 구조적 항으로 해석해야 한다.

## 7. 현재 논문에서 가장 안전한 결론

현재 결과만으로 “Entropy가 Volume보다 모든 면에서 우수하다”고 결론 내리는 것은 적절하지 않다. 대신 다음과 같이 정리하는 것이 연구 결과와 일치한다.

> Entropy 기반 정수 목적함수는 미시 수요 집중도가 높은 Zone에 작업자가 동시에 몰리는 위험을 반영하여 일부 운영일에서 실제 작업자 배치를 변경한다. Frozen Holdout에서 이 정책은 Volume Proportional 대비 평균 Flow Time을 약 2.84% 양보하는 대신 Conflicts 4.50%, Wait 11.31%, Congestion Ratio 5.85%를 줄이는 방향의 trade-off를 보였다. 40일 중 8일에서 배치가 실제로 달라졌고, 그 8일 모두에서 작업자가 고집중 Zone에서 저집중 Zone 방향으로 이동했으며 6일에서는 DES 혼잡도 함께 개선되었다.

따라서 본 연구의 기여는 단순한 “최단 Flow Time” 최적화가 아니라 **처리효율과 혼잡을 동시에 고려하는 엔트로피 기반 작업자 배치 정책의 설계 및 out-of-sample trade-off 검증**으로 정리하는 것이 가장 타당하다.
