# Phase 3 - 실제 데이터 기반 기존 작업자 배치 방식 비교

Phase 2에서 구축한 실제 창고 graph, Picking_Wave 피킹 순서, release time, 이동/혼잡/공간 엔트로피 계산 계층을 그대로 사용하여 **실제 운영 Baseline과 기존 작업자 배치 방식 3개**를 비교한다.

1. **Observed Baseline**: `Picking_Wave.csv`의 원본 operator 배정 유지
2. **Random Allocation**
3. **Equal Allocation**
4. **Volume Proportional Allocation**

`Entropy-based Allocation`은 Phase 3에 넣지 않는다. Phase 3에서 baseline 비교 구조를 먼저 고정하고, 같은 구조에 엔트로피 목적함수를 추가하는 작업은 Phase 4에서 수행한다.

## Phase 3의 핵심 원칙

Phase 3에서 가장 중요한 원칙은 **피킹 리스트 자체를 바꾸지 않는 것**이다.

- Phase 2와 동일한 fully-valid picking list만 사용한다.
- 원래 `Picking_Wave.csv`의 pick task 순서를 그대로 유지한다.
- 한 picking list를 zone별로 잘라서 새로운 리스트로 만들지 않는다.
- `Customer_Order.creationDate` 기반 release time을 그대로 유지한다.
- 창고 graph, 최단경로, 보행속도, 피킹시간, edge/pick-node capacity 정의를 그대로 사용한다.
- picking list 종료 후 I/O 복귀도 Phase 2 기본값과 동일하게 유지한다.

즉 Phase 2와 Phase 3의 주요 차이는 **어떤 zone에 몇 명의 작업자를 배치하느냐**이다.

## Zone 정의

창고 zone을 Location 이름의 `A`, `B`, `C` 같은 prefix로 만들지 않는다. 해당 prefix는 물리 좌표와 완전히 일치하는 독립 작업구역이라고 보기 어렵기 때문이다.

Phase 1에서 storage location은 가장 가까운 horizontal support-point aisle의 `y` 좌표로 투영되어 있다. 따라서 Phase 3에서는 이 실제 graph 구조를 사용한다.

기본값 `--zones 4`에서는 전체 horizontal aisle y 좌표를 정렬한 뒤, 연속된 aisle 개수가 가능한 균등하도록 4개 zone으로 나눈다.

현재 데이터의 horizontal aisle이 17개이므로 기본 zone은 다음과 같이 5/4/4/4 aisle 수준으로 구성된다.

```text
Z01 : 앞쪽 5개 aisle
Z02 : 다음 4개 aisle
Z03 : 다음 4개 aisle
Z04 : 마지막 4개 aisle
```

이 zone 경계는 선택 날짜의 수요를 보고 동적으로 만들지 않는다. 따라서 날짜별 실험에서도 동일한 물리적 공간 구획을 유지할 수 있다.

## Picking list의 zone 귀속

한 picking list에 여러 zone의 location이 포함될 수 있다. Phase 3에서는 리스트를 분할하지 않고 다음 규칙으로 하나의 **dispatch zone**에 귀속한다.

```text
1. pick task 수가 가장 많은 zone
2. 동률이면 pick units가 많은 zone
3. 다시 동률이면 zone 순서가 앞선 zone
```

예를 들어 한 리스트가 Z02에 3개 task, Z03에 15개 task, Z04에 6개 task를 갖는다면 그 리스트 전체를 Z03 workload로 본다. 실제 Worker는 원래 리스트를 그대로 처리하므로 필요한 경우 다른 zone까지 이동한다.

이 방식은 엄격한 zone-picking 모델이라기보다 **공간적으로 유사한 picking list를 workload pool로 묶고 그 pool에 작업자 수를 배정하는 모델**이다. 리스트 분할에 따른 추가 I/O 이동이나 order consolidation 효과를 Phase 3 결과에 섞지 않기 위한 선택이다.

## Workload 기준

기본값은 `--volume-basis tasks`이다.

```text
tasks = zone에 귀속된 picking list의 전체 pick task 수
units = zone에 귀속된 picking list의 quantity units 합
```

`tasks`는 피킹 위치 방문 및 작업 건수에 가까운 물동량 기준이고, `units`는 실제 수량 중심 기준이다. 두 기준은 옵션으로 민감도 비교할 수 있다.

## 작업자 수

`--workers`를 지정하지 않으면 선택 날짜의 fully-valid picking list에서 확인되는 **실제 distinct operator 수**를 전체 작업자 수로 사용한다.

예를 들어 선택 날짜에 실제 operator가 8명이면 Random / Equal / Volume Proportional 방법 모두 총 8명으로 비교한다.

따라서 전략별 총 인원은 동일하고 **zone별 분포만 달라진다.**

## Active zone 처리

선택한 날짜에 workload가 0인 zone에는 작업자를 강제로 배치하지 않는다. 기본값 `--minimum-per-active-zone 1`에 따라 workload가 있는 zone에는 최소 1명을 둔다.

이 규칙은 다음 두 문제를 피한다.

- 수요가 전혀 없는 zone에 작업자를 두어 baseline을 인위적으로 불리하게 만드는 문제
- workload가 있는데 작업자가 0명이 되어 시뮬레이션에서 처리할 수 없는 문제

## 비교 전략

### 0. Observed Baseline

`Picking_Wave.csv`에 기록된 원래 operator별 picking list 배정을 그대로 유지한다.
이는 Phase 2의 실제 operator schedule과 동일한 방식이며, Random / Equal / Volume Proportional의 비교 기준선으로 사용한다.

Baseline 작업자는 하나의 Phase 3 dispatch zone에 고정되지 않고 원래 배정된 picking list를 처리하면서 여러 zone을 이동할 수 있다. 따라서 `Zone Workload / Worker Allocation` 표에는 Baseline의 zone별 worker count를 표시하지 않고, 최종 `Comparison` 표에 성능 지표만 표시한다.

### 1. Random Allocation

활성 zone에 최소 작업자를 먼저 보장한 뒤 남은 작업자를 동일 확률로 무작위 배치한다.

```text
seed 고정 가능
같은 seed -> 같은 zone별 작업자 수
```

기본 seed는 `42`이다.

### 2. Equal Allocation

활성 zone 사이에 전체 작업자를 가능한 균등하게 배치한다.

예:

```text
활성 zone = 3개
작업자 = 8명
-> 3 / 3 / 2
```

정수 나눗셈의 동률은 zone 순서로 재현 가능하게 처리한다.

### 3. Volume Proportional Allocation

각 활성 zone의 workload 비율에 따라 작업자를 배치한다.

```text
worker_share_i ≈ workload_share_i
```

정수화는 기존 `allocation/strategies.py`의 water-filling + largest-remainder 방식을 그대로 사용한다.

## 실행

프로젝트 루트에서:

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05
```

빠른 개발 확인:

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05 --max-lists 20
```

특정 방법만 실행:

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05 --methods equal,volume_proportional
```

작업자 수를 직접 고정:

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05 --workers 8
```

unit 기준 물동량 비교:

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05 --volume-basis units
```

## 주요 옵션

```text
--zones 4                       물리적 aisle zone 수
--workers N                     전체 작업자 수; 생략 시 실제 operator 수
--volume-basis tasks|units      Volume Proportional의 물동량 기준
--minimum-per-active-zone 1     workload가 있는 zone의 최소 작업자 수
--seed 42                       Random Allocation 재현용 seed
--speed 1.2                     작업자 보행속도(m/s)
--pick-seconds 3.0              상품 1 unit 피킹시간(초)
--edge-capacity 1               graph edge 동시 사용 capacity
--pick-node-capacity 1          pick node 동시 피킹 capacity
--sample-seconds 5.0            공간 엔트로피 표본 간격
--no-return-to-io               list 종료 후 I/O 복귀 생략
--output-dir results/phase3     결과 저장 위치
```

## 출력 파일

```text
phase3_summary.csv
phase3_zones.csv
phase3_workers.csv
phase3_lists.csv
phase3_congestion.csv
phase3_entropy.csv
phase3_occupancy.csv
phase3_list_zones.csv
phase3_metadata.json
```

### phase3_summary.csv

전략별 핵심 결과를 한 행으로 저장한다.

- zone별 worker allocation의 normalized Shannon entropy
- workload share와 worker share 차이(`demand_worker_l1_gap`)
- 총 이동거리 / movement event / movement time
- congestion conflict 수와 edge / pick-node conflict 구분
- total / mean / P95 / max congestion wait time
- congestion delay ratio
- 평균/최대 release delay
- 평균/최소/최대 spatial entropy
- worker concentration / shared-worker ratio
- visited spatial cell / congested cell seconds / max cell occupancy
- simulation elapsed seconds

### phase3_zones.csv

전략·zone별:

- zone 경계와 aisle 수
- zone에 귀속된 picking list 수
- 귀속된 list의 task / unit workload
- 실제 물리적으로 해당 zone에 존재한 pick task / unit 수
- 여러 physical zone을 횡단하는 list 수
- 전략별 작업자 수와 worker share

`assigned_list_tasks`와 `physical_pick_tasks`를 둘 다 두는 이유는 **dispatch zone workload와 실제 공간 수요를 구분하기 위해서**이다.

### phase3_list_zones.csv

각 원본 picking list가 어떤 zone에 귀속되었는지 기록한다.

- original operator
- assigned zone
- list task / unit 수
- list가 실제로 걸친 physical zone 수
- dominant zone task / unit 수

이 파일로 zone 귀속 규칙을 사후 감사할 수 있다.

### phase3_lists.csv

전략별 실제 실행 결과:

- original operator
- assigned zone
- assigned synthetic worker
- release / start / finish time
- release delay
- pick task / unit 수
- physical zone 수

### phase3_workers.csv

전략별 synthetic worker 단위:

- 소속 zone
- 이동거리
- 피킹 unit
- 이동 / 피킹 event 수
- congestion conflict 수
- congestion wait time

### phase3_congestion.csv / phase3_entropy.csv / phase3_occupancy.csv

Phase 2와 같은 정의를 Baseline 및 전략별로 기록한다. 따라서 동일한 congestion / spatial entropy 정의로 방법 간 비교가 가능하다.

`Comparison`의 `Congestion(%)`은 Phase 2에서 정의한 congestion delay ratio를 백분율로 표시한 값이다.

```text
Congestion(%)
= 100 × Total Congestion Wait Time
        / (Total Movement Time + Total Congestion Wait Time)
```

## 추가 지표

### Worker Allocation Entropy

zone별 작업자 수를 `w_i`라고 하면:

```text
p_i = w_i / sum(w_i)
H_W = - Σ p_i log2(p_i)
```

`phase3_summary.csv`의 `worker_allocation_entropy_normalized`는 전체 zone 수를 범주 수로 한 normalized Shannon entropy이다.

- 낮음: 작업자가 일부 zone에 집중
- 높음: 작업자가 zone 사이에 고르게 분산

이는 **입력 배치의 엔트로피**이고, Phase 2/3의 `mean_spatial_entropy_normalized`는 시간에 따라 실제 이동 중인 작업자의 위치를 측정한 **결과 공간 엔트로피**이므로 서로 다른 지표이다.

### Demand-Worker L1 Gap

```text
0.5 × Σ |worker_share_i - workload_share_i|
```

- `0`: workload 비중과 worker 비중이 완전히 동일
- 값이 커질수록 수요와 작업자 배치 비중의 불일치가 큼

Volume Proportional 방법이 이 값을 낮추는 방향이고, Phase 4의 Entropy-based 방법은 이 수요 적합성과 분산도를 동시에 고려하게 된다.

## Phase 3에서 하지 않는 것

아직 다음은 구현하지 않는다.

- Entropy-based Allocation
- entropy weight `lambda` 민감도 분석
- Entropy + Genetic Algorithm
- 다중 날짜 반복실험 및 통계적 유의성 검정
- 최종 목적함수 가중치 튜닝

다음 Phase 4에서 **동일한 실데이터·zone·스케줄링 구조를 유지한 채 Entropy-based Allocation을 추가**한다.
