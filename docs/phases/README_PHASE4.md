# Phase 4 - Entropy 기반 정수 작업자 배치 및 λ Calibration

Phase 4는 본 연구의 핵심 제안 방법을 정의하고, Calibration 운영일에서 엔트로피 가중치 `λ`를 선택하는 단계입니다.

Phase 3의 Volume Proportional이 workload 적합도만 중시한다면 Phase 4는 **macro-zone 내부의 micro-zone 수요 집중도**를 추가하여 작업자가 특정 공간에 동시에 몰릴 위험을 함께 고려합니다.

## 1. 연구 질문

> **작업처리 효율성을 과도하게 희생하지 않으면서 수요가 집중된 공간의 작업자 중첩 위험과 DES 혼잡을 줄일 수 있는가?**

## 2. Entropy 기반 정수 목적함수

Macro-zone `z`의 workload를 `V_z`, 총 작업자 수를 `N`, 해당 zone의 정수 작업자 수를 `n_z`라 정의합니다.

```text
d_z = V_z / ΣV_z
p_z = n_z / N
```

### 2.1 Demand-fit term

```text
D(n) = 0.5 × Σ |p_z - d_z|
```

`D(n)`은 수요 비중과 작업자 비중의 total-variation distance이며 작을수록 workload에 잘 맞는 배치입니다.

### 2.2 Entropy concentration

각 macro-zone에는 5개의 demand micro-zone이 포함됩니다. 그 5개 workload의 normalized Shannon entropy를 `H_z`라 두고 집중도를 다음과 같이 정의합니다.

```text
C_z = 1 - H_z
```

- `H_z`가 높음: 수요가 비교적 고르게 분산
- `C_z`가 높음: 일부 micro-zone에 수요가 집중

### 2.3 Congestion-risk term

```text
R(n) = Σ C_z × C(n_z, 2)
```

같은 macro-zone에 배치된 작업자 쌍의 수를 해당 zone 수요 집중도로 가중합니다.

### 2.4 최종 목적함수

```text
J(n; λ) = D(n) + λR(n)
```

- `λ = 0`: Phase 3 **Volume Proportional 정수 배치와 동일한 control**
- `λ > 0`: workload 적합도 일부를 양보해 집중 zone의 작업자 pair-risk를 낮출 수 있음

가능한 정수 worker vector `[n1,n2,n3,n4]`를 직접 평가하여 `J`가 최소인 배치를 선택합니다. 이 때문에 λ가 특정 임계값을 넘으면 **실제 작업자 1명 이상의 zone 이동**이 직접 발생합니다.

## 3. Phase 4A - Eligible Operating Dates

전체 운영일 중 연구 조건을 만족하는 날짜를 선별합니다.

현재 결과:

```text
Operating dates : 176
Eligible dates  : 132
```

기본 `--min-lists` 조건을 통과하고 필요한 picking route를 구성할 수 있는 날짜만 이후 분석에 사용합니다.

## 4. Phase 4B - Calibration / Holdout Split

기본 분할:

```text
Split strategy    : chronological
Calibration ratio : 70%
Calibration       : 92 dates (2023-01-05 ~ 2023-07-18)
Frozen Holdout    : 40 dates (2023-07-19 ~ 2023-10-19)
```

Holdout 날짜는 λ 선택에 사용하지 않습니다.

## 5. Phase 4C - λ 후보 전체 DES

기본 후보:

```text
0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2, 4, 8
```

각 Calibration 날짜에서 λ별 정수 배치를 계산하고 동일한 배치가 반복되는 λ는 DES를 중복 실행하지 않도록 재사용합니다.

## 6. Phase 4D/E - Pareto-knee 선택

Calibration 92일에서 다음 4개 KPI 평균을 모두 작게 만드는 방향을 평가합니다.

```text
Mean Flow Time
Congestion Conflicts
Congestion Wait
Congestion Ratio
```

세 혼잡 KPI를 각각 λ=0 값으로 정규화하여 composite congestion index를 만들고, Flow Time과 congestion index의 Pareto curve에서 **knee point**를 λ*로 선택합니다.

최종 선택:

```text
Selected λ* = 0.25
Selection rule = pareto_knee
```

### Calibration 결과: λ=0 대비 λ=0.25

| KPI | 변화 |
|---|---:|
| Mean Flow Time | **+4.82%** |
| Conflicts | **−11.40%** |
| Congestion Wait | **−22.66%** |
| Congestion Ratio | **−14.57%** |
| Composite Congestion | **−16.21%** |

λ=0.25 이후에도 혼잡은 추가로 감소하지만 Flow Time 손실이 빠르게 커집니다. λ=0.25는 효율 손실과 혼잡 감소 사이의 곡률이 큰 지점으로 선택되었습니다.

## 7. 단일 날짜 정수 목적함수 진단

전체 Calibration 전에 특정 날짜에서 λ별 정수 worker vector와 `D`, `R`, `J`, 실제 DES KPI를 확인할 수 있습니다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05
```

이 모드는 전체 recommendation을 덮어쓰지 않고 다음과 같은 별도 디렉터리에 저장합니다.

```text
results/phase4/diagnostic_2023-01-05/
```

## 8. 전체 실행

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw
```

기존 단일 KPI 선택 규칙을 재현해야 할 때만:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --selection-rule single_metric --selection-metric mean_flow_time_seconds
```

최종 논문 실험은 `pareto_knee`를 사용합니다.

주요 옵션:

| 옵션 | 기본값 | 의미 |
|---|---:|---|
| `--min-lists` | 코드 기본값 | Eligible date 최소 list 수 |
| `--calibration-ratio` | `0.7` | Calibration 비율 |
| `--split-strategy` | `chronological` | 날짜 분할 방식 |
| `--entropy-weights` | 10개 후보 | λ 후보 목록 |
| `--selection-rule` | `pareto_knee` | λ 선택 규칙 |
| `--selection-metric` | `mean_flow_time_seconds` | single-metric 모드 기준 KPI |
| `--output-dir` | `results/phase4` | 결과 위치 |

## 9. 주요 출력 파일

`results/phase4/`:

| 파일 | 내용 |
|---|---|
| `phase4_dates.csv` | Eligible / Calibration / Holdout 날짜 |
| `phase4_allocations.csv` | 날짜·λ별 정수 배치와 목적함수 구성요소 |
| `phase4_daily_results.csv` | 날짜·λ별 DES KPI |
| `phase4_unique_runs.csv` | 실제 수행된 고유 정수배치 DES |
| `phase4_lambda_statistics.csv` | λ별 Calibration 통계 |
| `phase4_pairwise_vs_lambda0.csv` | λ=0 대비 paired 통계 |
| `phase4_pareto_analysis.csv` | Pareto / knee 계산 결과 |
| `phase4_selected_congestion_kpis_vs_lambda0.csv` | 선택 λ의 혼잡 KPI 비교 |
| `phase4_recommendation.json` | **λ*, Calibration/Holdout 날짜, model revision** |
| `phase4_metadata.json` | 전체 모델/실행 메타데이터 |

가장 중요한 연결 파일은 `phase4_recommendation.json`입니다. Phase 5는 이 파일을 읽어 λ*와 Frozen Holdout을 그대로 사용합니다.

## 10. 모델 Revision

최종 Phase 4/5/6 모델 revision:

```text
2026-08-22-cc08-inch-micro20-macro4-integer-objective-v1-pareto-knee-v1
```

과거 CC-01/centimeter 모델이나 연속 entropy weight를 반올림하던 이전 배치 방식의 결과는 최종 논문 결과로 사용하지 않습니다.

## 11. 다음 단계

Phase 5에서는 Calibration에서 선택한 `λ*=0.25`와 Holdout 40일을 **고정**합니다. λ 재선택이나 Holdout 재분할 없이 Baseline / Random / Equal / Volume / Entropy를 out-of-sample로 비교합니다.
