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

Phase 4의 제안 방식은 **4개 macro-zone의 총 물동량**과 **각 macro-zone 내부 5개 micro-zone의 공간적 집중도**를 함께 사용한다.

macro-zone `z` 내부 micro-zone workload를 `v_zj`라고 하면 normalized Shannon entropy를 계산한다.

```text
H_z = normalized Shannon entropy(v_z1, ..., v_z5)
C_z = 1 - H_z
```

`C_z`는 0~1 범위의 수요 집중도이다. 5개 micro-zone에 수요가 고르게 퍼질수록 0에 가깝고, 한두 micro-zone에 집중될수록 1에 가까워진다.

Picking list를 dominant macro-zone으로 귀속해 계산한 macro workload를 `V_z`, 전체 작업자 수를 `N`, zone별 정수 작업자 수를 `n_z`라고 한다. Phase 4는 더 이상 연속 가중치를 만든 뒤 반올림하지 않고, **가능한 정수 작업자 배치 벡터를 직접 평가**한다.

```text
d_z = V_z / ΣV_z
p_z = n_z / N

D(n) = 0.5 × Σ |p_z - d_z|
R(n) = Σ C_z × C(n_z, 2)
J(n; lambda) = D(n) + lambda × R(n)
```

여기서 `D(n)`은 Volume 수요비중과 정수 작업자비중의 불일치(total-variation distance)이고, `R(n)`은 같은 macro-zone에 함께 배치된 작업자 쌍의 수를 해당 zone의 수요 집중도 `C_z`로 가중한 혼잡위험 지수이다.

제약조건은 다음과 같다.

```text
Σ n_z = N
n_z는 정수
활성 zone: n_z >= minimum_per_active_zone
비활성 zone: n_z = 0
```

`lambda=0`은 Phase 3의 기존 Volume Proportional 정수 배치를 정확한 control로 사용한다. `lambda`가 증가하면 수요 적합도 `D`가 조금 나빠지더라도, 수요가 집중된 zone에서 여러 작업자가 동시에 겹치는 위험 `R`을 줄이는 정수 배치가 선택될 수 있다. 따라서 λ 변화가 **작업자 한 명의 실제 zone 이동 결정**으로 직접 연결된다.

기본 λ 후보는 다음과 같다.

```text
0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2, 4, 8
```

각 Calibration 날짜마다 위 λ 후보를 모두 평가한다. 같은 날짜에서 서로 다른 λ가 동일한 정수 worker allocation을 만들면 DES 입력이 동일하므로 실제 DES는 한 번만 실행하고 결과를 재사용한다.

`phase4_daily_results.csv`에는 λ별 `[n1,n2,n3,n4]`, `D`, `R`, `J`, Volume 기준 이동 작업자 수를 함께 저장한다. `phase4_allocations.csv`에는 zone별 `microzone_concentration`, `microzone_entropy_normalized`, `pair_congestion_risk_contribution`을 저장한다.

### 단일 날짜 정수 목적함수 진단

전체 Calibration을 돌리기 전에 `2023-01-05` 하루에서 정수 배치 변화와 DES KPI를 확인할 수 있다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05
```

출력 순서:

```text
1. zone별 workload / workload share / C_z
2. λ별 [n1,n2,n3,n4], D, R, J
3. Volume 대비 실제 이동 작업자 수
4. λ별 DES KPI
```

단일 날짜 결과는 전체 Phase 4E recommendation을 덮어쓰지 않도록 `results/phase4/diagnostic_2023-01-05/` 아래에 별도로 저장한다.

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
