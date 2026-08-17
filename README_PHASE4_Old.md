# Phase 4 - 엔트로피 기반 작업자 배치 최적화

Phase 4는 Phase 3에서 고정한 실제 데이터 실험 조건을 그대로 유지하면서 **Entropy-based Allocation의 엔트로피 가중치 λ(lambda)를 탐색**한다.

Phase 3에서 비교한 기존 방식은 다음과 같다.

1. Random Allocation
2. Equal Allocation
3. Volume Proportional Allocation

Phase 4에서는 이 baseline 구조를 바꾸지 않고 엔트로피 정규화만 추가한다. 즉 warehouse graph, fully-valid picking list, 원래 pick 순서, release time, zone 정의, 작업자 수, 보행속도, 피킹시간, edge/pick-node capacity, I/O 복귀 규칙은 Phase 3와 동일하다.

## 1. 엔트로피 기반 배치식

zone별 정규화 수요를 `d_i`, 연속적인 작업자 비율을 `p_i`라고 하면 다음 목적함수를 사용한다.

```text
min  KL(p || d) - λ H(p)
```

여기서:

```text
KL(p || d) : 작업자 분포가 수요 분포에서 벗어나는 정도
H(p)       : 작업자 분포의 Shannon entropy
λ           : 수요 적합도와 분산 정도의 trade-off를 조절하는 가중치
```

해는 다음과 같은 닫힌형식(closed form)을 갖는다.

```text
p_i ∝ d_i ** (1 / (1 + λ))
```

따라서:

```text
λ = 0       -> Volume Proportional Allocation과 동일
λ 증가      -> 작업자 분포가 점점 균등한 방향으로 이동
λ -> 매우 큼 -> 양의 수요가 있는 zone 사이의 Equal Allocation에 접근
```

연속 비율은 Phase 3와 동일한 water-filling + largest-remainder 방식으로 정수 작업자 수로 변환한다. workload가 0인 zone에는 작업자를 강제로 배치하지 않는다.

## 2. 기본 λ 후보

기본 탐색 범위는 다음과 같다.

```text
0, 0.25, 0.5, 1, 2, 4, 8
```

`λ=0`을 반드시 포함한 기본 이유는 **Phase 3의 Volume Proportional을 Phase 4 내부의 기준점(control)**으로 두기 위해서이다.

원하는 경우 CLI에서 후보를 바꿀 수 있다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05 --entropy-weights 0,0.1,0.25,0.5,1,1.5,2,4
```

## 3. 동일 정수 배치 재사용

실제 작업자 수가 적으면 서로 다른 λ가 같은 정수 worker allocation을 만들 수 있다.

예:

```text
λ=0.25 -> Z01=1, Z02=2, Z03=2, Z04=3
λ=0.50 -> Z01=1, Z02=2, Z03=2, Z04=3
```

이 두 후보는 DES 입력이 완전히 동일하다. 따라서 Phase 4는 모든 λ를 결과표에는 남기되, **동일한 정수 작업자 배치는 한 번만 시뮬레이션**한다.

결과에서:

```text
candidate_count        : 입력한 λ 후보 수
unique_simulation_count: 실제 수행한 서로 다른 DES 수
allocation_id          : 동일 worker allocation을 묶는 ID (A001, A002, ...)
```

이 방식은 결과를 바꾸지 않으면서 Phase 4 실행시간을 크게 줄일 수 있다.

## 4. 최적 λ 선택 기준

Phase 4에서는 서로 단위가 다른 여러 KPI를 임의의 가중합으로 합쳐 하나의 점수를 만들지 않는다. 기본값은 논문에서 직접 해석하기 쉬운 **Mean Flow Time**을 단일 primary KPI로 사용한다.

```text
Mean Flow Time = 각 picking list의 release 시점부터 완료 시점까지의 시간 평균
```

기본 선택 기준:

```text
--selection-metric mean_flow_time_seconds
```

지원하는 기준:

```text
mean_flow_time_seconds          최소화
makespan_seconds                최소화
congestion_wait_seconds         최소화
congestion_conflicts            최소화
total_distance_m                최소화
mean_release_delay_seconds      최소화
mean_spatial_entropy_normalized 최대화
```

예를 들어 Makespan을 primary KPI로 λ를 선택하려면:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05 --selection-metric makespan_seconds
```

동일한 KPI 값이 정확히 동률이면 더 작은 λ를 선택한다. 이는 같은 운영성과라면 불필요하게 강한 엔트로피 정규화를 선택하지 않기 위한 보수적 규칙이다.

## 5. Phase 4와 Phase 5의 역할 분리

Phase 4에서 선택된 λ가 모든 날짜에 최적이라고 바로 주장하지 않는다.

```text
Phase 4 : calibration date에서 λ 탐색 및 후보 선택
Phase 5 : 다른 날짜/부하 조건에서도 선택 λ가 baseline보다 우수한지 검증
```

예를 들어 `2023-01-05`를 calibration date로 사용했다면 Phase 5에서는 다른 날짜들을 validation date로 사용하여 Random / Equal / Volume Proportional / selected Entropy-based Allocation을 비교하는 구조가 적절하다.

이 분리는 특정 하루에 과도하게 맞춘 λ를 전체 데이터에 일반화하는 문제를 줄인다.

## 6. 실행

기본 실행:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05
```

빠른 개발 확인:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05 --max-lists 20
```

작업자 수 직접 지정:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05 --workers 8
```

unit 기준 workload:

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05 --volume-basis units
```

## 7. 주요 출력 파일

```text
phase4_summary.csv
phase4_allocations.csv
phase4_unique_runs.csv
phase4_recommendation.json
phase4_metadata.json
phase4_selected_workers.csv
phase4_selected_lists.csv
phase4_selected_congestion.csv
phase4_selected_entropy.csv
phase4_selected_occupancy.csv
```

### phase4_summary.csv

λ 후보별 핵심 비교 결과이다.

- entropy weight λ
- allocation ID
- zone별 worker counts
- 동일 정수 배치 재사용 여부
- worker allocation entropy
- demand-worker L1 gap
- 이동거리
- congestion conflict / wait
- release delay
- mean flow time
- makespan
- spatial entropy
- `λ=0` 대비 주요 KPI 변화율
- 최종 선택 여부

### phase4_allocations.csv

λ·zone별 workload share와 worker share를 저장한다. λ가 증가할 때 작업자 분포가 실제로 어떻게 평탄화되는지 확인할 수 있다.

### phase4_unique_runs.csv

실제로 수행한 서로 다른 DES만 기록한다. 여러 λ가 같은 정수 배치를 만들면 하나의 allocation ID로 묶인다.

### phase4_recommendation.json

선택 기준과 최종 λ를 별도로 저장한다.

```text
selection_metric
entropy_weight
allocation_id
worker_counts
metric_value
allocation_entropy_normalized
demand_worker_l1_gap
```

### phase4_selected_*.csv

최종 선택된 λ의 작업자별/리스트별/혼잡/공간 엔트로피 상세 결과를 저장한다. Phase 5에서는 이 선택 결과를 baseline과 여러 날짜에서 재검증한다.

## 8. 논문에서의 해석 주의점

Phase 4의 엔트로피는 단순히 "높을수록 무조건 좋다"는 목적이 아니다.

- λ가 너무 낮으면 수요가 큰 zone에 작업자가 집중될 수 있다.
- λ가 너무 높으면 수요와 관계없이 작업자가 지나치게 균등해져 flow time 또는 makespan이 악화될 수 있다.
- 따라서 연구의 핵심은 **수요 적합성과 공간적 분산 사이에 실제 운영 KPI가 가장 좋아지는 trade-off가 존재하는지**를 실데이터 DES로 확인하는 것이다.

또한 Phase 2~4의 congestion conflict는 실제 사람끼리 부딪힌 물리적 충돌 횟수가 아니라 capacity-limited edge 또는 pick node에서 발생한 simulated resource contention waiting event이다.
