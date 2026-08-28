# Phase 6 - Trade-off, Mechanism and Robustness Analysis

Phase 6는 Phase 5의 Frozen Holdout 결과를 다시 시뮬레이션하거나 λ를 재선택하지 않고, **Calibration→Holdout 일반화, Entropy vs Volume paired 통계, 실제 정수 배치 변경 메커니즘, 목적함수 proxy의 방향성**을 분석하는 최종 단계입니다.

## 1. 입력 무결성

기본 입력:

```text
results/phase4/phase4_recommendation.json
results/phase4/phase4_daily_results.csv
results/phase5/phase5_daily_summary.csv
results/phase5/phase5_allocations.csv
results/phase5/phase5_allocation_equivalence.csv
```

최종 분석 조건:

```text
Selected λ*          : 0.25
Calibration dates    : 92
Frozen Holdout dates : 40
Holdout completed    : 40 / 40
Lambda reselection   : no
Holdout resampling   : no
```

## 2. 실행

```powershell
python -m entropy_thesis.simulation.phase6
```

주요 옵션:

| 옵션 | 기본값 | 의미 |
|---|---|---|
| `--phase4-dir` | `results/phase4` | Phase 4 입력 위치 |
| `--phase5-dir` | `results/phase5` | Phase 5 입력 위치 |
| `--output-dir` | `results/phase6` | Phase 6 결과 위치 |
| `--bootstrap-samples` | 코드 기본값 | paired bootstrap 반복 수 |
| `--seed` | 코드 기본값 | bootstrap 재현 seed |
| `--allow-partial-holdout` | false | smoke test용 partial 결과 허용; 최종 논문용 아님 |

## 3. Calibration → Holdout 일반화

Entropy(λ=0.25)와 기준 Volume/λ=0의 trade-off:

| Split | Flow Time Cost | Conflicts Reduction | Wait Reduction | Congestion Ratio Reduction | Composite Congestion Reduction |
|---|---:|---:|---:|---:|---:|
| Calibration | +4.82% | 11.40% | 22.66% | 14.57% | 16.21% |
| Holdout | +2.84% | 4.50% | 11.31% | 5.85% | 7.22% |

혼잡 감소 폭은 Holdout에서 작아졌지만 **효율을 일부 양보하고 혼잡을 줄이는 방향 자체는 유지**되었습니다.

## 4. Entropy vs Volume - 4개 Pareto KPI 통계

Holdout 40일 전체 paired 결과:

| KPI | Mean 기준 개선 | W/T/L | Wilcoxon p | Holm p | Paired Bootstrap 95% CI* |
|---|---:|---:|---:|---:|---:|
| Mean Flow Time | -2.836% | 1 / 32 / 7 | 0.0173 | **0.0692** | [-37.69, -4.30] s |
| Conflicts | +4.502% | 6 / 32 / 2 | 0.0929 | 0.2787 | [-0.40, 17.15] |
| Wait | +11.314% | 6 / 32 / 2 | 0.1235 | 0.2787 | [-1.16, 109.02] s |
| Congestion Ratio | +5.854% | 6 / 32 / 2 | 0.2076 | 0.2787 | [-0.00174, 0.01492] |

`*` CI의 차이는 Entropy 개선 방향이 양수가 되도록 정의된 native difference입니다.

해석:

- Flow Time 악화는 보정 전 Wilcoxon `p=0.0173`이지만 4개 KPI family의 Holm 보정 후 `p=0.0692`입니다.
- 혼잡 3지표는 평균적으로 개선 방향이지만 40일 전체에서는 Holm 0.05 기준 통계적 유의성이 확인되지 않았습니다.
- 32/40일에서 정수 배치가 동일하므로 많은 paired difference가 0인 구조입니다.

따라서 통계 결과는 과장하지 않고 **평균적 trade-off와 실제 배치 변경 메커니즘의 존재**를 중심으로 해석합니다.

## 5. 실제 정수 배치가 달라진 8일

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

메커니즘 검증:

```text
8 / 8 : J_entropy <= J_volume
8 / 8 : 작업자가 높은 concentration zone → 낮은 concentration zone 방향으로 이동
6 / 8 : 실제 DES composite congestion도 개선
```

분류:

```text
congestion_gain_efficiency_cost : 6 days
efficiency_gain_congestion_cost : 1 day
loss_loss                       : 1 day
```

즉 entropy 항은 단순한 설명용 score가 아니라 **일부 날짜의 실제 정수 worker vector를 변경**합니다.

## 6. Congestion-risk proxy R(n) 검증

배치가 변경된 8일에서 `R_volume - R_entropy`와 실제 DES 혼잡 감소량의 상관을 탐색적으로 분석합니다.

| Target | Pearson r | p | Spearman ρ | p |
|---|---:|---:|---:|---:|
| Conflict reduction | 0.731 | 0.039 | 0.452 | 0.260 |
| Wait reduction | 0.867 | 0.005 | 0.429 | 0.289 |
| Congestion Ratio reduction | 0.900 | 0.002 | 0.500 | 0.207 |

Pearson에서는 비교적 강한 선형 방향성을 보이지만 표본이 8일뿐이고 Spearman은 유의하지 않습니다. 따라서 이는 **목적함수 proxy의 방향성이 실제 DES 결과와 대체로 일치한다는 보조적 메커니즘 근거**로만 사용합니다.

`D(n)` 증가량과 Flow Time 증가량의 관계도 강한 직접 상관으로 해석하지 않습니다. `D(n)`은 Flow Time 자체를 예측하는 항이 아니라 workload와 worker share의 구조적 불일치를 나타내는 항입니다.

## 7. 출력 파일

`results/phase6/`:

| 파일 | 내용 |
|---|---|
| `phase6_calibration_holdout_generalization.csv` | Calibration→Holdout trade-off |
| `phase6_pareto_metric_statistics.csv` | Wilcoxon / Holm / bootstrap 결과 |
| `phase6_changed_dates.csv` | 배치 변경 8일의 목적함수와 DES 차이 |
| `phase6_changed_date_zone_details.csv` | 변경 날짜의 zone별 이동 상세 |
| `phase6_proxy_validation.csv` | R/D proxy와 DES KPI 관계 |
| `phase6_metadata.json` | 분석 조건과 정의 |

## 8. 논문에서의 최종 해석

현재 결과로 다음과 같이 주장하는 것은 적절하지 않습니다.

> “Entropy가 Volume보다 모든 상황과 모든 KPI에서 우수하다.”

최종 결과에 더 부합하는 해석은 다음과 같습니다.

> Entropy 기반 정수 목적함수는 micro-zone 수요 집중도가 높은 macro-zone에 작업자가 동시에 몰리는 위험을 반영하여 일부 운영일에서 실제 작업자 배치를 변경한다. Frozen Holdout에서 이 정책은 Volume Proportional 대비 평균 Flow Time을 약 2.84% 양보하는 대신 Conflicts 4.50%, Wait 11.31%, Congestion Ratio 5.85%를 줄이는 방향의 trade-off를 보였다. 40일 중 8일에서 배치가 실제로 달라졌고, 그 8일 모두에서 작업자가 상대적으로 높은 concentration zone에서 낮은 concentration zone 방향으로 이동했으며 6일에서는 DES composite congestion도 개선되었다.

따라서 본 연구의 기여는 **최단 Flow Time만을 추구하는 배치가 아니라, 처리효율과 공간 혼잡을 함께 고려하는 엔트로피 기반 정수 작업자 배치 정책을 설계하고 Frozen Holdout에서 그 trade-off와 메커니즘을 검증한 것**으로 정리합니다.
