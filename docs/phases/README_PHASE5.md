# Phase 5 - Frozen Holdout Validation

Phase 5는 Phase 4의 Calibration 과정에서 선택한 `λ*`를 **다시 조정하지 않고**, 미사용 Holdout 운영일에 적용하여 out-of-sample 성능을 검증하는 단계입니다.

## 1. Frozen 원칙

Phase 5는 `results/phase4/phase4_recommendation.json`에서 다음 정보를 읽습니다.

```text
Selected λ*       = 0.25
Calibration dates = 92
Holdout dates     = 40
Model revision    = 2026-08-22-cc08-inch-micro20-macro4-integer-objective-v1-pareto-knee-v1
```

핵심 무결성 규칙:

1. Holdout 결과를 보고 λ를 다시 선택하지 않습니다.
2. Holdout 날짜를 다시 샘플링하지 않습니다.
3. Phase 4 recommendation의 model revision이 다르면 실행을 거부합니다.
4. 같은 날짜의 각 방법은 동일한 데이터와 DES 물리 조건에서 paired 비교합니다.

## 2. 비교 방법

```text
Observed Baseline
Random
Equal
Volume Proportional
Entropy-based (λ*=0.25)
```

각 날짜의 기본 총 작업자 수는 그 날짜의 관측 operator 수입니다.

## 3. 실행

전체 Frozen Holdout:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```

일부 Holdout 날짜만 smoke test할 때:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --dates 2023-07-19,2023-07-24
```

`--dates`에 지정한 날짜는 반드시 Phase 4의 Frozen Holdout에 포함되어야 합니다.

다른 recommendation 파일을 명시할 때:

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --recommendation results/phase4/phase4_recommendation.json
```

## 4. 주요 KPI

| KPI | 해석 |
|---|---|
| `mean_flow_time_seconds` | list release부터 완료까지 평균 시간 |
| `makespan_seconds` | 해당 날짜 전체 작업 완료시간 |
| `congestion_conflicts` | capacity contention event 수 |
| `congestion_wait_seconds` | contention 총 대기시간 |
| `congestion_delay_ratio` | `Wait / (Movement + Wait)` |
| `mean_release_delay_seconds` | list release 대비 실제 시작 지연 |
| `mean_spatial_entropy_multiworker` | 2명 이상 active 시점 공간 entropy |
| `mean_spatial_entropy_normalized` | 전체 sampling 시점 공간 entropy |

## 5. Holdout 40일 최종 평균

| Method | Mean Flow Time (s) | Conflicts | Wait (s) | Congestion Ratio |
|---|---:|---:|---:|---:|
| Baseline | 794.82 | 103.38 | 180.97 | 5.04% |
| Random | 762.64 | 138.98 | 304.60 | 7.33% |
| Equal | 858.62 | 116.05 | 244.60 | 6.30% |
| Volume | **680.61** | 155.48 | 345.34 | 8.48% |
| Entropy (λ*=0.25) | 699.91 | 148.48 | 306.27 | 7.99% |

Volume은 평균 Flow Time이 가장 짧지만 혼잡이 큰 축에 위치합니다. Entropy는 일부 효율을 양보하면서 Volume의 혼잡을 낮추는 중간 Pareto 정책으로 나타납니다.

## 6. Entropy vs Volume

Holdout 평균 기준:

| KPI | 결과 |
|---|---:|
| Mean Flow Time cost | **+2.84%** |
| Conflicts reduction | **4.50%** |
| Wait reduction | **11.31%** |
| Congestion Ratio reduction | **5.85%** |
| Composite Congestion reduction | **7.22%** |

부호 해석:

```text
Flow Time cost > 0       : Entropy가 Volume보다 느림
Congestion reduction > 0 : Entropy가 Volume보다 혼잡이 낮음
```

Calibration에서 관측한 **효율 손실 + 혼잡 감소** 방향이 Holdout에서도 유지되었습니다.

## 7. 정수 배치 동일성

40일 중 Entropy와 Volume의 정수 worker vector가 완전히 동일한 날짜가 많습니다.

```text
Same as Volume : 32 / 40 dates
Changed        :  8 / 40 dates
```

이 구조는 Phase 6 통계와 메커니즘 해석에서 매우 중요합니다. 32일은 배치가 같으므로 DES KPI도 사실상 동일하고, 실제 제안법의 차이는 8일에서 집중적으로 발생합니다.

## 8. 출력 파일

`results/phase5/`:

| 파일 | 내용 |
|---|---|
| `phase5_daily_summary.csv` | 날짜·방법별 핵심 DES KPI |
| `phase5_method_summary.csv` | 방법별 40일 통계 요약 |
| `phase5_comparison_summary.csv` | 방법별 평균 핵심 KPI |
| `phase5_allocations.csv` | 날짜·방법별 정수 worker 배치 |
| `phase5_allocation_comparison.csv` | Entropy/Volume 배치 및 목적함수 비교 |
| `phase5_allocation_equivalence.csv` | 정수배치 동일 여부 |
| `phase5_paired_comparison.csv` | paired 방법 비교 |
| `phase5_primary_comparison.csv` | 주요 KPI 비교 |
| `phase5_date_profiles.csv` | 날짜별 workload / concentration profile |
| `phase5_skipped_dates.csv` | 제외 날짜; 최종 실행은 0건 |
| `phase5_metadata.json` | Frozen 조건과 model revision |

## 9. 해석 주의

Phase 5의 목적은 Entropy가 Volume보다 모든 KPI에서 우월함을 증명하는 것이 아닙니다. **Calibration에서 선택한 효율-혼잡 균형이 새로운 날짜에서도 유지되는지**를 검증하는 것이 핵심입니다.

최종 Holdout에서는 Flow Time 약 2.84%의 비용으로 세 혼잡 지표가 평균적으로 4.50~11.31% 감소했습니다. 이 trade-off의 통계적 강건성과 실제 배치 변경 메커니즘은 Phase 6에서 분석합니다.
