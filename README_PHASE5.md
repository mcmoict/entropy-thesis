# Phase 5 - Holdout 검증

Phase 5는 Phase 4A~4E에서 **한 번 확정한 λ\***와 **한 번 분리한 Holdout 날짜**를 그대로 사용하여 다음 5개 작업자 배치 방법을 out-of-sample로 비교한다.

```text
Baseline / Random / Equal / Volume Proportional / Entropy(λ*)
```

가장 중요한 원칙은 Phase 5에서 λ를 다시 최적화하거나 날짜를 다시 분할하지 않는 것이다. Phase 4의 Holdout 데이터는 Phase 4C~4E의 λ 선택에 사용되지 않았으므로, Phase 5에서 최종 일반화 성능을 검증하는 독립 표본이다.

## 1. Phase 4 결과 자동 연결

기본 실행은 다음 파일을 읽는다.

```text
results/phase4/phase4_recommendation.json

> 2026-08-22 모델 정정 이후 Phase 5는 recommendation의 `model_revision`을 검사한다. CC-01/cm 또는 이전 Zone 모델에서 생성된 recommendation은 자동 거부되므로 **Phase 4를 먼저 전부 재실행**해야 한다.
```

이 파일에서 다음 값을 동시에 불러온다.

```text
- selected λ*
- primary selection metric
- Calibration dates
- frozen Holdout dates
```

현재 저장된 Phase 4 결과는 다음과 같다.

```text
Calibration dates : 92
Holdout dates     : 40
Selected λ*       : 0.05
Primary metric    : mean_flow_time_seconds
```

Phase 5는 recommendation 파일이 없거나, 최신 `phase=4E` 형식이 아니거나, Calibration/Holdout 날짜가 겹치면 실행을 중단한다.

## 2. 본 실험 실행

Phase 4가 완료된 프로젝트 루트에서 다음 명령만 실행한다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```

기본 실행은 Phase 4에서 고정된 Holdout 40일을 모두 사용한다. 임의 12일 sampling이나 validation-day 재선택은 하지 않는다.

## 3. 비교 방법

각 Holdout 날짜에서 동일한 warehouse graph, 동일한 실제 picking list, 동일한 시뮬레이션 파라미터를 사용해 다음 방법을 비교한다.

```text
1. Baseline             : Picking_Wave.csv의 실제 operator 배정
2. Random               : 활성 zone에 무작위 작업자 배정
3. Equal                : 활성 zone에 균등 작업자 배정
4. Volume Proportional  : zone workload 비례 작업자 배정
5. Entropy-based        : Phase 4에서 고정한 λ* 적용
```

`--workers`를 지정하지 않으면 각 Holdout 날짜의 실제 observed operator 수를 비교 방법의 총 작업자 수로 사용한다.

## 4. Holdout 무결성 규칙

Phase 5는 다음을 허용하지 않는다.

```text
- λ 재탐색
- Holdout 재분할
- Calibration 날짜를 Holdout에 추가
- 현재 데이터에 없는 Holdout 날짜를 다른 날짜로 대체
```

현재 데이터셋에서 Phase 4가 고정한 Holdout 날짜가 사라졌다면 데이터셋이 Phase 4 이후 변경된 것으로 간주하여 오류를 발생시킨다.

빠른 smoke test가 필요할 때만 `--dates`로 **기존 Holdout의 부분집합**을 지정할 수 있다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --dates 2023-07-19
```

Holdout이 아닌 날짜를 지정하면 실행하지 않는다.

## 5. 주요 KPI

Primary KPI는 Phase 4에서 λ* 선택에 사용한 값과 동일하다.

```text
mean_flow_time_seconds
```

추가 비교 KPI는 다음과 같다.

```text
makespan_seconds
congestion_wait_seconds
congestion_conflicts
total_distance_m
mean_release_delay_seconds
mean_spatial_entropy_multiworker
mean_spatial_entropy_normalized
worker_allocation_entropy_normalized
demand_worker_l1_gap
```

Phase 5 콘솔에는 Phase 3의 `=== Comparison ===` 형식을 그대로 확장한
**Holdout 평균 비교표**도 출력한다.

```text
=== Comparison | Holdout Mean (40 dates) ===
Method               Distance(m)   Conflicts   Wait(s)   Congestion(%)   Mean release delay(s)   Mean flow time(s)   Makespan(s)   Mean spatial H (2+ workers)   Mean spatial H
baseline                  ...          ...        ...           ...                    ...                ...           ...                   ...              ...
random                    ...          ...        ...           ...                    ...                ...           ...                   ...              ...
equal                     ...          ...        ...           ...                    ...                ...           ...                   ...              ...
volume_proportional       ...          ...        ...           ...                    ...                ...           ...                   ...              ...
entropy_based             ...          ...        ...           ...                    ...                ...           ...                   ...              ...
```

Phase 3의 표는 한 운영일의 KPI지만, Phase 5의 표는 완료된 Holdout 날짜별 KPI를
방법별로 산술평균한 값이다. 따라서 `Conflicts` 역시 정수 총계가 아니라
**Holdout 날짜당 평균 충돌 수**로 소수점이 표시될 수 있다.

`Mean spatial H (2+ workers)`는 표본 시점에 active worker가 2명 이상인 경우만 대상으로 한
평균 spatial entropy이며, `Mean spatial H`는 전체 표본 시점을 대상으로 한 평균이다.

## 6. 작업자 배치 비교

Phase 5는 KPI 표와 별도로 각 방법의 인력배치 규칙과 Zone별 Holdout 평균 배치를 출력한다.

```text
=== Worker Allocation Rules ===
baseline            : Picking_Wave.csv의 원본 operator -> picking-list 배정을 그대로 유지
random              : 활성 Zone 최소 인원 + 잔여 인원 seed 기반 무작위 배치
equal               : 활성 Zone에 가능한 균등 배치
volume_proportional : 날짜별 Zone workload 비율에 비례 배치
entropy_based       : 고정 λ*로 macro workload × (1 + λ* × micro-zone concentration)을 적용한 배치

=== Worker Allocation | Holdout Mean by Zone (40 dates) ===
Zone   Workload(%)   Baseline*   Random   Equal   Volume   Entropy   Baseline touch**
Z01          ...         ...      ...      ...      ...       ...              ...
...
```

Baseline은 다른 네 방법과 달리 작업자를 하나의 Zone에 고정하지 않는다. 따라서 비교표의
`Baseline*`은 **비교용 dominant-zone projection**이다. 날짜별로 각 원본 operator가 담당한
picking list workload를 Zone별로 합산한 뒤 가장 큰 Zone에 operator를 한 번만 귀속하여 계산하며,
그 합은 해당 날짜의 observed worker 수와 같다. 실제 baseline DES는 이 projection을 사용하지 않고
기존 원본 operator 배정을 그대로 유지한다.

`Baseline touch**`는 해당 Zone을 dominant zone으로 갖는 picking list를 하나 이상 수행한 원본
operator의 수이다. 한 operator가 여러 Zone에 나타날 수 있으므로 Zone 합계는 전체 작업자 수보다
클 수 있다.

## 7. 통계 검증

운영일을 paired observation으로 두고 Entropy(λ*)와 각 비교 방법을 같은 날짜끼리 비교한다.

```text
Entropy vs Baseline
Entropy vs Random
Entropy vs Equal
Entropy vs Volume Proportional
```

각 KPI에 대해 다음을 계산한다.

```text
- 방법별 평균 / 표준편차 / 중앙값 / 최소 / 최대
- Entropy의 Wins / Ties / Losses
- 평균 개선율(%)
- Wilcoxon signed-rank test
- two-sided p-value
- p < 0.05 여부
```

## 8. 정수 작업자 배치 동일성 확인

λ*=0.05처럼 작은 가중치는 연속 점수에서는 차이를 만들더라도 최종 정수 작업자 수로 변환될 때 Volume 배치와 같은 결과를 만들 수 있다.

이를 분리해서 확인하기 위해 날짜별로 다음을 저장한다.

```text
Entropy allocation == Random allocation ?
Entropy allocation == Equal allocation ?
Entropy allocation == Volume allocation ?
DES result reused from which method ?
```

따라서 Entropy와 Volume KPI가 동일한 경우에도 그 원인이 **실제 동일 정수 배치인지** 확인할 수 있다.

## 9. 출력 파일

```text
results/phase5/
  phase5_daily_summary.csv
  phase5_date_profiles.csv
  phase5_allocations.csv
  phase5_allocation_comparison.csv
  phase5_allocation_equivalence.csv
  phase5_method_summary.csv
  phase5_comparison_summary.csv
  phase5_paired_comparison.csv
  phase5_primary_comparison.csv
  phase5_skipped_dates.csv
  phase5_metadata.json
```

### phase5_daily_summary.csv

Holdout 날짜 × 방법 단위의 DES 결과이다. 40개 날짜를 모두 완료하면 기본적으로 40 × 5 = 200개의 방법별 결과가 저장된다.

### phase5_method_summary.csv

방법 × KPI별 전체 Holdout 요약 통계이다.

### phase5_comparison_summary.csv

콘솔의 `Comparison | Holdout Mean` 표와 동일한 Phase 3 스타일의 wide-format 요약이다.
Baseline / Random / Equal / Volume Proportional / Entropy-based 순서로 Holdout 평균 KPI를 저장한다.

### phase5_allocation_comparison.csv

Holdout 날짜 × Zone 단위로 workload와 인력배치를 wide-format으로 저장한다.

```text
baseline_dominant_zone_workers
baseline_distinct_operators_touching_zone
random_workers
equal_workers
volume_proportional_workers
entropy_based_workers
```

이 파일은 Phase 6에서 Volume과 Entropy의 정수 배치가 달라진 날짜를 골라 Zone별 수요분포 →
인력배치 → KPI 변화를 추적할 때 바로 사용할 수 있다.

### phase5_paired_comparison.csv

Entropy(λ*)와 네 비교 방법의 날짜별 paired 비교 결과이다.

### phase5_primary_comparison.csv

Phase 4에서 사용한 primary KPI만 추린 논문용 핵심 비교표이다.

### phase5_allocation_equivalence.csv

Entropy 정수 작업자 배치가 Random / Equal / Volume과 동일한 날짜인지 기록한다. λ*=0.05의 실제 효과가 정수화 과정에서 소멸하는지 분석할 때 사용한다.

### phase5_metadata.json

Phase 4 Calibration/Holdout 날짜, λ*, 실행 파라미터, 완료/skip 날짜, 통계 검정 규칙을 보존한다.

## 10. 권장 실행 순서

코드 경로만 빠르게 확인하려면 Phase 4 Holdout 첫 날짜 1개로 smoke test를 한다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw --dates 2023-07-19 --max-lists 20
```

최종 논문 실험은 별도 옵션 없이 전체 Holdout을 실행한다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```
