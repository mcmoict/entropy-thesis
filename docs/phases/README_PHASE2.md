# Phase 2 - Observed Baseline DES

Phase 2는 Phase 1에서 구축한 실제 Warehouse Graph와 `Picking_Wave.csv`의 **관측 작업자 배정 및 원래 피킹 순서**를 그대로 사용하여 기준 이산 사건 시뮬레이션(Discrete-Event Simulation, DES)을 구성하는 단계입니다.

## 1. 목적

Phase 2에서는 다음 지표 계산 체계를 확립합니다.

1. Travel Distance
2. Congestion / Waiting
3. Demand Spatial Entropy
4. Worker Spatial Entropy
5. Node / Edge Occupancy
6. Release Delay, Flow Time, Makespan

Random / Equal / Volume / Entropy 방식의 작업자 재배치는 아직 수행하지 않습니다. 즉, Phase 2는 이후 모든 방법을 비교하기 위한 **Observed Baseline**입니다.

## 2. 유지되는 물리 가정

- 원본 좌표: inch → meter (`× 0.0254`)
- 공통 시작/종료 지점: `SUP:CC-08`
- 원래 `Picking_Wave.csv` 피킹 순서 유지
- unresolved location의 좌표를 임의 생성하지 않음
- 기본적으로 각 Picking List 완료 후 CC-08로 복귀

## 3. 실제 시간축

같은 날짜의 Picking List는 원본 release 시각을 기준으로 DES에 투입됩니다. 작업자가 이전 list를 수행 중이면 다음 list의 실제 시작은 늦어질 수 있으며, 이 차이를 `release delay`로 측정합니다.

```text
Release time ── 대기 ── Actual start ── 이동/피킹/혼잡 ── Completion
                 ↑
            Release Delay
```

## 4. 혼잡 정의

`congestion_conflicts`는 실제 사람이 물리적으로 부딪힌 횟수가 아닙니다. 통로나 pick node가 capacity를 사용 중이어서 다른 작업자가 즉시 진입하지 못한 **DES resource contention event**입니다.

기본 capacity:

```text
Edge capacity      = 1
Pick-node capacity = 1
```

주요 혼잡 KPI:

```text
congestion_conflicts
congestion_wait_seconds
congestion_delay_ratio = Wait / (Movement + Wait)
```

## 5. 엔트로피 지표

### Demand Spatial Entropy

선택 날짜의 피킹 수요가 창고 내 어느 위치에 분포하는지 Shannon entropy로 측정합니다. task 기준과 unit 기준 값을 모두 기록합니다.

### Worker Spatial Entropy

일정 간격으로 작업자 위치를 sampling하여 공간 분산도를 측정합니다.

- `mean_spatial_entropy_normalized`: 전체 sampling 시점 평균
- `mean_spatial_entropy_multiworker`: active worker가 2명 이상인 시점만 평균

두 번째 지표는 작업자가 0~1명만 활동하는 시간대 때문에 entropy가 기계적으로 낮아지는 영향을 줄이기 위한 보조 지표입니다.

## 6. 실행

가장 이른 fully-resolvable 운영일 자동 선택:

```powershell
python -m entropy_thesis.simulation.phase2 --data-dir data/raw
```

특정 날짜:

```powershell
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05
```

주요 옵션:

| 옵션 | 기본값 | 의미 |
|---|---:|---|
| `--max-lists` | 전체 | smoke test용 최대 list 수 |
| `--speed` | `1.2` | 보행속도(m/s) |
| `--pick-seconds` | `3.0` | 단위당 피킹시간(s) |
| `--edge-capacity` | `1` | edge 동시 사용 capacity |
| `--pick-node-capacity` | `1` | pick node capacity |
| `--sample-seconds` | `5.0` | 공간 entropy/occupancy sampling 간격 |
| `--no-return-to-io` | false | list 종료 후 CC-08 복귀 생략 |
| `--output-dir` | `results/phase2` | 결과 위치 |

## 7. 대표 Sanity Check: 2023-01-05

현재 포함된 최종 결과 기준:

| KPI | 값 |
|---|---:|
| Picking Lists | 85 |
| Operators | 8 |
| Pick Tasks | 1,996 |
| Total Distance | 8,053.12 m |
| Conflicts | 174 |
| Congestion Wait | 244.73 s |
| Congestion Ratio | 3.52% |
| Mean Release Delay | 521.22 s |
| Mean Spatial H | 0.8976 |
| Mean Spatial H (2+ workers) | 0.9926 |

이 날짜는 Phase 3의 방식별 비교 및 시각화 sanity check에도 사용합니다.

## 8. 출력 파일

`results/phase2/`:

| 파일 | 내용 |
|---|---|
| `phase2_summary.csv` | 날짜 전체 KPI 요약 |
| `phase2_workers.csv` | 작업자별 이동/작업 요약 |
| `phase2_lists.csv` | Picking List별 timing / distance |
| `phase2_congestion.csv` | contention event 상세 |
| `phase2_entropy.csv` | 시간대별 worker spatial entropy |
| `phase2_occupancy.csv` | node/edge occupancy sampling |
| `phase2_demand.csv` | demand 공간 분포 |
| `phase2_metadata.json` | 실행 조건과 모델 정의 |

## 9. 다음 단계

Phase 3에서는 같은 날짜, 같은 Graph, 같은 DES 규칙을 유지하면서 당일 작업자 수를 고정한 채 **Random / Equal / Volume Proportional** 방식으로 4개 macro-zone의 작업자 배치를 바꾸어 비교합니다.
