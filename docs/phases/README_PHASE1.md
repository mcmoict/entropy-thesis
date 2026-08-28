# Phase 1 - 실제 데이터 검증 및 Warehouse Graph 구축

Phase 1은 실제 물류센터 원천 CSV를 읽어 **데이터 스키마, 좌표계, 피킹 위치 해석 가능 여부, 창고 Navigation Graph**를 검증하는 단계입니다. 이후 Phase 2~6이 모두 이 물리 모델을 사용하므로, 연구 재현의 출발점에 해당합니다.

## 1. 목적

- 실제 CSV 파일을 연구용 객체로 로딩합니다.
- Storage / Support Point를 이용해 창고 이동 Graph를 구축합니다.
- 모든 피커의 공통 I/O를 **CC-08**로 고정합니다.
- 피킹 위치가 Storage 데이터에 존재하는지 감사(audit)합니다.
- fully-resolvable Picking List만 사용하여 간단한 SimPy smoke test를 수행합니다.

## 2. 입력 데이터

기본 위치는 `data/raw/`입니다.

```text
Storage_Location.csv
Support_Points_Navigation.csv
Product.csv
Customer_Order.csv
Picking_Wave.csv
```

`Picking_Wave.csv`는 원본 행의 피킹 순서를 유지하며, 동일 wave에 여러 작업자가 존재할 수 있으므로 **`(waveNumber, operator)` 단위**를 하나의 Picking List로 사용합니다.

## 3. 물리 모델 기준

### 좌표 단위

원본 CAD 좌표는 **inch**로 해석합니다.

```text
1 inch = 0.0254 m
meter = raw_coordinate × 0.0254
```

### 공통 I/O / Depot

모든 작업자의 기본 출발·복귀 지점은 다음으로 고정합니다.

```text
Support Point : CC-08
Graph node    : SUP:CC-08
Coordinate    : approximately (10.2362, 16.0274) m
```

`CC-08`이 없을 경우 임의의 다른 Support Point로 대체하지 않고 오류를 발생시킵니다.

### 미정의 피킹 위치

`Storage_Location.csv`에 존재하지 않는 `RC-01` 등의 위치는 좌표를 임의 생성하지 않습니다. 해당 task는 unresolved로 기록하며, 완전한 경로가 필요한 DES에서는 fully-resolvable list만 사용합니다.

## 4. 최종 데이터 감사 결과

현재 최종 데이터 기준:

| 항목 | 값 |
|---|---:|
| Storage Locations | 2,292 |
| Support Points | 44 |
| Products | 208 |
| Customer Order Lines | 122,370 |
| Picking Tasks | 215,192 |
| `(wave, operator)` Picking Lists | 9,796 |
| Operators | 22 |
| Navigation Graph Nodes | 510 |
| Navigation Graph Edges | 534 |
| Connected Components | 1 |
| Resolved Picking Tasks | 191,583 (89.03%) |
| Unresolved Picking Tasks | 23,609 (10.97%) |
| Fully-valid Picking Lists | 7,402 / 9,796 |

Graph가 하나의 connected component로 구성되어 있으므로, 해석 가능한 피킹 위치는 CC-08 기준의 이동 경로를 계산할 수 있습니다.

## 5. 실행

프로젝트 루트에서:

```powershell
python -m entropy_thesis.simulation.phase1 --data-dir data/raw
```

보행속도와 단위당 피킹시간을 변경하려면:

```powershell
python -m entropy_thesis.simulation.phase1 --data-dir data/raw --speed 1.2 --pick-seconds 3.0
```

주요 옵션:

| 옵션 | 기본값 | 의미 |
|---|---:|---|
| `--data-dir` | `data/raw` | 원천 CSV 위치 |
| `--speed` | `1.2` | 보행속도(m/s) |
| `--pick-seconds` | `3.0` | 단위당 피킹시간(s) |

## 6. 검증

전체 테스트:

```powershell
python -m pytest
```

Phase 1 관련 테스트는 데이터 로더, Warehouse Graph, Worker 기본 동작을 검증합니다.

## 7. 출력과 다음 단계

Phase 1은 결과 CSV를 생성하는 실험 단계라기보다 **콘솔 기반 데이터/Graph validation 단계**입니다. 주요 출력은 데이터 건수, Graph 통계, I/O 좌표, unresolved 위치 감사, operator별 smoke-test 결과입니다.

검증이 완료되면 Phase 2에서 실제 날짜의 관측 작업자 배정과 피킹 순서를 그대로 사용해 **Observed Baseline DES**를 실행합니다.
