# Phase 2 - 실제 데이터 기반 이동거리 · 혼잡 · 공간 엔트로피

Phase 1에서 구축한 실제 창고 graph와 Picking_Wave 피킹 순서를 그대로 사용하여 다음 다섯 가지를 계산한다.

1. **이동거리 (Travel Distance)**
2. **혼잡 및 대기 (Congestion / Waiting)**
3. **수요 공간 엔트로피 (Demand Spatial Entropy)**
4. **작업자 공간 엔트로피 (Worker Spatial Entropy)**
5. **공간 cell 점유도 (Node / Edge Occupancy)**

Phase 2의 목적은 지표 계산 계층을 만드는 것이다. Random / Equal / Volume Proportional / Entropy-based 배치 전략 비교는 Phase 3에서 수행한다.

## 실행

프로젝트 루트에서:

```bash
python -m entropy_thesis.simulation.phase2 --data-dir data/raw
```

날짜를 지정하려면:

```bash
python -m entropy_thesis.simulation.phase2 \
  --data-dir data/raw \
  --date 2023-01-05
```

PowerShell에서는 한 줄로 실행해도 된다.

```powershell
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05
```

`--date`를 생략하면 fully-resolvable picking list가 존재하는 가장 이른 날짜를 선택한다. 현재 데이터 기준 최초 날짜는 `2023-01-05`이다.

빠른 개발 테스트용으로 일부 list만 실행할 수 있다.

```powershell
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05 --max-lists 20
```

## 주요 옵션

```text
--speed 1.2                 작업자 보행속도 (m/s)
--pick-seconds 3.0          상품 1 unit 피킹시간 (초)
--edge-capacity 1           하나의 graph edge를 동시에 사용하는 작업자 용량
--pick-node-capacity 1      하나의 피킹 node를 동시에 사용하는 작업자 용량
--sample-seconds 5.0        공간 엔트로피 표본 간격 (초)
--no-return-to-io           picking list 종료 후 I/O 복귀를 생략
--output-dir results/phase2 결과 저장 위치
```

기본 모델은 한 picking list가 끝나면 작업자가 `CC-01` 기반 기본 I/O 지점으로 복귀한 뒤 다음 list를 처리한다.

## Phase 1 데이터 처리 원칙 유지

Phase 1에서 확정한 원칙을 변경하지 않는다.

- `Storage_Location.csv`에 좌표가 존재하는 pick task만 graph에 연결한다.
- `RC-01`, `RC-04` 등 좌표가 없는 Picking_Wave location에 임의 좌표를 만들지 않는다.
- Phase 2는 **모든 pick task가 resolve되는 fully-valid picking list만** 사용한다.
- 현재 실제 데이터에서는 `7,402 / 9,796` picking list가 이 조건을 만족한다.

## Phase 2 재현성 보강

Phase 1의 기본 graph build 동작은 유지한다. 다만 Phase 2는 edge 단위 congestion 결과가 경로 동률에 따라 바뀌지 않도록 `deterministic_order=True`로 graph를 생성한다.

또한 Phase 2 이동 경로는 다음 우선순위로 동일 거리 경로의 동률을 해소한다.

```text
1. 총 이동거리 최소
2. edge 수 최소
3. node ID sequence 사전순
```

이 규칙은 Phase 2에서만 사용되며 unresolved location 처리 원칙은 변경하지 않는다.

## 실제 시간축

`Customer_Order.creationDate`를 해당 wave의 release time으로 사용한다.

- 같은 wave에 여러 operator가 있으면 같은 시각에 작업이 release된다.
- 한 operator에게 여러 list가 배정되어 이전 list를 아직 처리 중이면 다음 list는 순차 대기한다.
- 이 지연은 `release_delay_seconds`로 별도 기록한다.

## 혼잡 정의

Phase 2의 `congestion conflict`는 **실제 사람끼리 물리적으로 충돌했다는 뜻이 아니다.**

다음 공유 자원을 요청했을 때 즉시 진입하지 못하고 양의 대기시간이 발생하면 conflict 1건으로 센다.

1. **edge conflict**: 동일한 undirected graph edge에 설정된 capacity를 초과하는 동시 이동 요청
2. **pick-node conflict**: 동일한 피킹 node에서 설정된 capacity를 초과하는 동시 피킹 요청

따라서 논문에서는 `collision count`보다 `congestion conflict count`, `resource contention count`, `waiting event count`처럼 표현하는 것이 안전하다.

혼잡 지연 비율은 다음과 같이 계산한다.

```text
Congestion Delay Ratio
= Total Congestion Wait Time
  / (Total Movement Time + Total Congestion Wait Time)
```

## 수요 공간 엔트로피 정의

선택 날짜의 fully-valid pick task를 warehouse pick node별로 집계한다.

```text
p_i = node i의 pick task 수 / 전체 pick task 수
H_D = - Σ p_i log2(p_i)
```

`task_entropy`는 pick task 빈도 기준이고 `unit_entropy`는 `quantity_units` 기준이다. 정규화 시에는 **실제로 수요가 발생한 node 수가 아니라 전체 warehouse pick node 수**를 분모 범주 수로 사용한다. 따라서 일부 node에만 수요가 집중될수록 값이 낮아진다.

## 작업자 공간 엔트로피 정의

각 표본시각 `t`에서 작업자 상태를 spatial cell로 변환한다.

- 이동 중: 현재 이동 중인 **undirected graph edge**
- 피킹 중: 현재 **graph node**
- congestion wait 중: 대기하고 있는 **graph node**
- wave 사이 idle 상태: 엔트로피 표본에서 제외

활성 작업자 수가 `W`이고 cell별 작업자 수가 `n_i`이면:

```text
p_i = n_i / W
H(t) = - Σ p_i log2(p_i)
```

정규화 엔트로피는 다음과 같다.

```text
H_norm(t) = H(t) / log2(W),   W >= 2
```

- `H_norm = 0`: 활성 작업자가 한 공간 cell에 완전히 집중
- `H_norm = 1`: 활성 작업자가 가능한 한 서로 다른 cell에 분산
- 활성 작업자가 1명뿐이면 `H_norm = 0`으로 정의

`mean_spatial_entropy_multiworker`는 활성 작업자가 2명 이상인 표본만 평균하여, 단일 작업자 시간대 때문에 평균이 인위적으로 낮아지는 문제를 함께 확인할 수 있게 한다.

## 출력 파일

기본 출력 위치는 `results/phase2/`이다.

```text
phase2_summary.csv
phase2_workers.csv
phase2_congestion.csv
phase2_lists.csv
phase2_entropy.csv
phase2_occupancy.csv
phase2_demand.csv
phase2_metadata.json
```

### phase2_summary.csv

하루 단위 전체 요약:

- picking list / operator / pick task 수
- 총 피킹 unit
- 수요 발생 pick node 수
- task / unit 기준 demand entropy
- 총 이동거리
- movement event 수와 실제 이동시간
- congestion conflict 수
- edge / pick-node conflict 수
- 총/평균/P95/최대 대기시간
- congestion delay ratio
- 평균/최대 wave release delay
- 평균/최소/최대 spatial entropy
- 평균 worker concentration
- shared-worker ratio
- 방문 spatial cell 수
- 동시 occupancy 2 이상인 congested cell time
- 최대 cell 동시 작업자 수

### phase2_workers.csv

operator별:

- 이동거리
- 피킹량
- 이동/피킹 event 수
- congestion conflict 수
- congestion wait time

### phase2_congestion.csv

혼잡이 발생한 edge 또는 pick node별:

- conflict 횟수
- 영향을 받은 작업자 수
- 총/평균/최대 대기시간

Phase 3에서 hotspot 분석에 바로 사용할 수 있다.

### phase2_lists.csv

각 `(waveNumber, operator)` picking list의:

- release time
- 실제 시작시각
- 종료시각
- release delay
- pick task 수

### phase2_entropy.csv

시간 표본별:

- active worker 수
- occupied spatial cell 수
- Shannon entropy(bits)
- normalized entropy
- 가장 혼잡한 cell의 작업자 비중 (`max_concentration`)
- 복수 작업자가 공유한 cell 수
- shared cell에 존재한 작업자 수
- excess worker 수

### phase2_occupancy.csv

방문한 spatial cell별 정확한 interval 기반 점유 지표:

- worker-seconds
- move / pick / wait worker-seconds
- 실제 점유시간
- 동시 작업자 2명 이상인 congested seconds
- 최대 동시 작업자 수
- unique worker 수

### phase2_demand.csv

warehouse pick node별 수요 분포:

- 좌표 `x_m`, `y_m`
- 연결된 storage location 수
- pick task 수 / 비율
- pick unit 수 / 비율

### phase2_metadata.json

실험 파라미터와 모든 지표 정의를 기록한다. 논문 작성 시 지표 정의가 실행별로 달라지는 것을 방지하기 위한 재현성 파일이다.

## Phase 2에서 하지 않는 것

아직 다음은 구현하지 않는다.

- Random Allocation 비교
- Equal Allocation 비교
- Volume Proportional Allocation 비교
- Entropy-based Allocation 비교
- Entropy + GA 최적화

이들은 각각 Phase 3 이후의 작업이다.

## 실행 진행상태 / ETA 표시

Phase 2 CLI는 장시간 실행 중 멈춘 것처럼 보이지 않도록 전체 wall-clock 진행상태를 출력한다.

- `[RUN.]`, `[RUN..]`, `[RUN...]`: 프로그램이 계속 실행 중임을 나타내는 heartbeat 형태 표시
- `current=...`: 현재 읽는 입력 파일, 분석 중인 wave/operator, 또는 생성 중인 결과 파일
- `elapsed=HH:MM:SS`: 프로그램 시작 후 실제 경과시간
- `ETA=HH:MM:SS`: 현재 전체 진행률과 실제 경과시간을 이용한 종료 예상 잔여시간
- `[DONE] Total execution time`: 전체 처리가 완료된 뒤 실제 총 실행시간

예시:

```text
[START] Phase 2 real-data simulation
[RUN.  ]    2.0% | Loading input file | current=data/raw/Product.csv | elapsed=00:00:00 | ETA=--:--:--
[RUN...]   53.5% | Simulating picking lists 40/85 | current=wave=..., operator=Operator_... | elapsed=00:00:18 | ETA=00:00:16
[RUN.  ]   90.0% | Generating result file | current=results/phase2/phase2_summary.csv | elapsed=00:00:31 | ETA=00:00:03
[RUN.. ]  100.0% | Phase 2 processing completed | elapsed=00:00:34 | ETA=00:00:00
[DONE] Total execution time: 00:00:34
```

ETA는 예측값이다. CSV 로딩, 그래프 구성, DES, 지표 집계, 파일 저장의 단위 작업 비용이 서로 다르므로 실행 초반에는 값이 크게 변할 수 있으며, 처리가 진행될수록 실제 종료시간에 가까워진다. 분석 결과 계산식과 Phase 1 unresolved 처리 원칙에는 영향을 주지 않는다.
