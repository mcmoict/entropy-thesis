# Phase 4 - 다중 날짜 엔트로피 가중치 λ Calibration

Phase 4는 한 날짜에서 λ를 고르는 기존 방식 대신, **전체 적합 운영 날짜를 Calibration / Holdout으로 먼저 분리한 뒤 Calibration 날짜 전체에서 λ를 탐색**한다. Holdout 날짜는 Phase 4에서 DES를 수행하지 않으며 이후 Phase 5의 최종 검증에만 사용한다.

```text
Phase 4A  전체 적합 날짜 추출
    ↓
Phase 4B  Calibration / Holdout 날짜 분리
    ↓
Phase 4C  Calibration 날짜 × λ 후보 전체 DES
    ↓
Phase 4D  λ별 날짜 평균 및 통계 비교
    ↓
Phase 4E  최적 λ* 결정
    ↓
Phase 5   Holdout에서 Baseline / Random / Equal / Volume / Entropy(λ*) 검증
```

## 1. Phase 4A - 전체 적합 날짜 추출

우선 timestamp가 존재하고 모든 Picking Location이 Warehouse Graph에서 resolve되는 picking list만 날짜별로 묶는다.

기본적으로 한 날짜에 fully-valid picking list가 최소 20개 있어야 적합 날짜로 인정한다.

```text
--min-lists 20
```

또한 해당 날짜의 활성 zone 수와 작업자 수를 확인하여 `minimum_per_active_zone` 조건을 만족하지 못하면 Calibration/Holdout 대상에서 제외한다.

모든 날짜의 판정 결과는 다음 파일에 저장된다.

```text
results/phase4/phase4_dates.csv
```

주요 컬럼:

```text
selected_date
picking_lists
pick_tasks
picked_units
observed_workers
effective_workers
active_zones
eligible
reason
split                 # calibration / holdout / ineligible
```

현재 첨부 데이터에서 기본 조건(`--min-lists 20`, 4 zones)을 적용하면:

```text
전체 fully-valid 운영 날짜 : 176일
적합 날짜                  : 132일
Calibration (70%)          : 92일
Holdout (30%)              : 40일
```

## 2. Phase 4B - Calibration / Holdout 분리

기본 분할은 날짜 단위 chronological 70:30이다.

```text
앞쪽 70% 날짜 -> Calibration
뒤쪽 30% 날짜 -> Holdout
```

같은 날짜의 picking list가 양쪽에 섞이지 않는다. Holdout 날짜는 Phase 4C DES에 절대 사용하지 않는다.

기본값:

```text
--calibration-ratio 0.7
--split-strategy chronological
```

필요하면 재현 가능한 random date split도 가능하다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --split-strategy random --seed 42
```

## 3. Phase 4C - Calibration 날짜 × λ 후보 전체 DES

엔트로피 기반 배치식은 기존 Phase 4와 동일하다.

```text
min  KL(p || d) - λ H(p)
```

closed form:

```text
p_i ∝ d_i ** (1 / (1 + λ))
```

해석:

```text
λ = 0       -> Volume Proportional Allocation
λ 증가      -> 작업자 분포가 점점 평탄화
λ -> 큼     -> 활성 zone 사이 Equal Allocation에 접근
```

기본 λ 후보는 다음과 같이 촘촘하게 변경했다.

```text
0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2, 4, 8
```

각 Calibration 날짜마다 위 λ 후보를 모두 평가한다.

```text
Calibration Date 1 × λ 10개
Calibration Date 2 × λ 10개
...
Calibration Date N × λ 10개
```

단, **같은 날짜에서 서로 다른 λ가 동일한 정수 worker allocation을 만들면 DES 입력이 완전히 동일**하므로 실제 DES는 한 번만 실행하고 결과를 재사용한다. λ 후보 자체는 결과표에 모두 남는다.

일별 λ 결과:

```text
results/phase4/phase4_daily_results.csv
```

zone별 배치:

```text
results/phase4/phase4_allocations.csv
```

## 4. Phase 4D - λ별 날짜 평균 및 통계 비교

각 Calibration 날짜를 **동일한 하나의 관측치**로 취급한다. 즉 list가 많은 날짜가 최적 λ 선정에 과도하게 큰 가중치를 갖지 않는다.

λ별로 다음 통계를 계산한다.

```text
n_dates
mean
std
median
min
max
```

대상 KPI:

```text
mean_flow_time_seconds
makespan_seconds
congestion_wait_seconds
congestion_conflicts
total_distance_m
mean_release_delay_seconds
mean_spatial_entropy_normalized
```

결과:

```text
results/phase4/phase4_lambda_statistics.csv
```

또한 `λ=0`, 즉 Volume Proportional을 기준으로 각 λ를 **동일 날짜끼리 paired comparison**한다.

```text
wins / ties / losses
mean improvement (%)
Wilcoxon signed-rank p-value
```

결과:

```text
results/phase4/phase4_pairwise_vs_lambda0.csv
```

Wilcoxon 검정은 Calibration 날짜별 paired KPI 차이를 검정하는 용도이다. 동일 정수 배치로 모든 날짜 차이가 0이면 p-value는 1.0으로 기록한다.

## 5. Phase 4E - 최적 λ* 결정

기본 primary KPI는 기존 연구 방향과 동일하게 Mean Flow Time이다.

```text
--selection-metric mean_flow_time_seconds
```

최종 λ*는 **Calibration 날짜별 primary KPI의 산술평균**을 기준으로 결정한다.

```text
시간/비용 KPI -> 평균 최소 λ
Spatial Entropy -> 평균 최대 λ
```

정확한 동률이면 더 작은 λ를 선택한다.

중요하게도 Wilcoxon p-value를 λ* 선택의 강제 필터로 사용하지 않는다. λ*는 운영 KPI 평균으로 선택하고, 통계적 유의성은 별도로 보고한다. 따라서 결과가 유의하지 않다면 논문에서는 그 사실을 그대로 해석할 수 있다.

최종 결과:

```text
results/phase4/phase4_recommendation.json
```

주요 항목:

```text
selection_metric
entropy_weight              # λ*
metric_mean
metric_median
n_calibration_dates
mean_improvement_vs_lambda0_pct
wins_vs_lambda0
ties_vs_lambda0
losses_vs_lambda0
wilcoxon_p_value_vs_lambda0
calibration_dates
holdout_dates
```

Phase 5는 이후 이 파일의 `entropy_weight`와 `holdout_dates`를 사용하도록 연결하면 된다.

## 6. 기본 실행

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw
```

기본 실행 조건:

```text
적합 날짜 최소 list 수 : 20
Calibration/Holdout    : 70% / 30%
Split                   : chronological
Zones                   : 4
λ                       : 0,0.05,0.1,0.25,0.5,0.75,1,2,4,8
Primary KPI             : mean_flow_time_seconds
```

λ 후보를 직접 지정하려면:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --entropy-weights 0,0.05,0.1,0.25,0.5,0.75,1,2,4,8
```

Calibration 비율 변경:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --calibration-ratio 0.8
```

빠른 개발 확인용으로 list를 제한할 경우 `--min-lists`도 함께 낮춰야 한다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --max-lists 20 --min-lists 20
```

## 7. 주요 출력 파일

```text
phase4_dates.csv                  # Phase 4A/4B 날짜 적합성 및 split
phase4_daily_results.csv          # Phase 4C 날짜 × λ DES KPI
phase4_allocations.csv            # 날짜 × λ × zone 배치
phase4_lambda_statistics.csv      # Phase 4D λ별 날짜 통계
phase4_pairwise_vs_lambda0.csv    # Phase 4D λ=0 대비 paired 통계
phase4_recommendation.json        # Phase 4E λ*
phase4_metadata.json              # 전체 실행 조건/정의/Calibration/Holdout 목록
```

## 8. 기존 단일 날짜 함수와 Phase 5 호환성

기존 `build_and_run_phase4()` 단일 날짜 함수는 삭제하지 않았다. 현재 Phase 5의 `--recalibrate` 코드가 이 함수를 호출하므로 API 호환성을 유지하기 위함이다.

다만 `python -m entropy_thesis.simulation.phase4` CLI의 기본 동작은 이제 단일 날짜 탐색이 아니라 **Phase 4A~4E 다중 날짜 Calibration**이다.

Phase 5는 다음 단계에서 `phase4_recommendation.json`의 `holdout_dates`만 읽어 Baseline / Random / Equal / Volume / Entropy(λ*)를 검증하도록 수정하는 것이 다음 작업이다.
