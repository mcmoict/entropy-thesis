# Model Revision — 2026-08-22

Phase 6 이전 데이터/물리모델 감사에서 발견한 사항을 반영한 연구모델 정정본이다.

## 1. 물리모델 정정

- **단일 I/O / Depot:** `CC-01` → **`CC-08`**
- **원본 CAD 좌표 단위:** centimeter → **inch**
- **미터 변환:** `raw × 0.0254`
- `CC-08`이 데이터에 없으면 다른 support point로 fallback하지 않고 오류를 발생시킨다.
- 모든 picker는 기본적으로 `CC-08`에서 시작하고 picking list 완료 후 `CC-08`로 복귀한다.

## 2. 공간모델 정정

Navigation graph는 원래 창고 전체를 유지한다. 작업량/인력배치용 Zone만 별도로 정의한다.

### 20개 Demand Micro-zone

- `M01~M10`: `LC-08~LC-17`
- `M11~M20`: `RC-08~RC-17`
- `CC-08`의 x 좌표를 기준으로 left/right를 구분한다.
- 각 storage location은 해당 side의 08~17 support anchor 중 원래 y 좌표와 가장 가까운 micro-zone으로 귀속한다.

### 4개 Workforce Macro-zone

- `Z01`: `LC-08~LC-12` (Left / Near)
- `Z02`: `LC-13~LC-17` (Left / Far)
- `Z03`: `RC-08~RC-12` (Right / Near)
- `Z04`: `RC-13~RC-17` (Right / Far)

Picking list는 분할하지 않는다. 원래 pick sequence를 보존하며, list 내 pick task가 가장 많은 dominant macro-zone의 queue에 귀속한다. 실행 중 worker는 다른 macro-zone을 통과하거나 피킹할 수 있다.

## 3. Entropy-aware Allocation

Macro-zone `z` 내부 5개 micro-zone의 workload 분포에 대해 normalized Shannon entropy `H_z`를 계산한다.

```text
C_z = 1 - H_z
A_z(lambda) = V_z * (1 + lambda * C_z)
```

- `V_z`: dominant-list dispatch 기준 macro-zone workload
- `H_z`: macro-zone 내부 5개 micro-zone의 normalized Shannon entropy
- `C_z`: 내부 공간 수요 집중도
- `A_z(lambda)`: λ 적용 후 인력배치 가중치
- `lambda = 0`: 정확히 Volume Proportional Allocation
- `lambda > 0`: 동일/유사 물동량이라도 내부 수요가 더 집중된 macro-zone에 추가 가중치

작업자는 `A_z(lambda)`에 비례하여 기존 deterministic apportionment 로직으로 정수 배정한다.

## 4. 기존 결과의 취급

이 변경은 이동거리, 이동시간, 혼잡, 대기, release delay proxy, flow time, makespan뿐 아니라 Phase 4 λ 선택에도 영향을 줄 수 있다.
따라서 기존 `λ*=0.05`와 기존 Phase 5 holdout 결과는 현재 모델의 최종 결과로 사용하지 않는다.

이전 결과는 `results_legacy_pre_20260822_cc01_cm/`로 이동했다. 새 `results/`는 비워 두었다.
Phase 5는 `phase4_recommendation.json`의 `model_revision`을 검사하여 이전 모델의 recommendation을 자동 거부한다.

## 5. 실제 데이터 정적 검증 (DES 실행 전)

현재 데이터에서 확인한 값:

- Default I/O: `SUP:CC-08`
- CC-08 좌표: `(10.2362, 16.0274) m`
- Picking lists: `9,796`
- Fully-valid lists: `7,402`
- Fully-valid list pick tasks: `161,569`
- 20 micro-zones: **20개 모두 수요 존재**
- Overall 20-cell task entropy (normalized): 약 `0.9483`

Fully-valid lists의 micro-zone task 수:

```text
M01 LC-08  14,531    M11 RC-08  14,681
M02 LC-09   9,600    M12 RC-09  15,609
M03 LC-10  10,049    M13 RC-10  10,693
M04 LC-11  12,738    M14 RC-11   7,602
M05 LC-12   9,934    M15 RC-12   6,185
M06 LC-13   5,512    M16 RC-13   9,708
M07 LC-14   7,344    M17 RC-14   5,922
M08 LC-15   6,674    M18 RC-15   4,951
M09 LC-16     146    M19 RC-16   5,252
M10 LC-17   2,128    M20 RC-17   2,310
```

Macro-zone 내부 concentration (`C_z = 1-H_z`)은 전체 fully-valid lists 기준 참고값으로 대략:

```text
Z01: 0.0088
Z02: 0.1692
Z03: 0.0370
Z04: 0.0560
```

위 값은 전체 데이터 정적 감사값이며 Phase 4 Calibration 날짜별 값은 각 날짜에서 다시 계산한다.

## 6. 재실행 순서

```powershell
python -m entropy_thesis.simulation.phase1 --data-dir data/raw
python -m pytest
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05
python -m entropy_thesis.simulation.phase4 --data-dir data/raw
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```

Phase 1~3의 sanity check 결과를 먼저 확인한 뒤 Phase 4 전체 Calibration과 Phase 5 Frozen Holdout을 수행하는 것을 권장한다.
