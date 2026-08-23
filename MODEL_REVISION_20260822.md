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

> **2026-08-23 추가 개정:** 2026-08-22의 연속 가중치→반올림 방식은 λ 변화가 실제 정수 인력배치 변화로 이어지지 않는 문제가 있어 폐기하고, Phase 4에서 아래 정수 목적함수로 대체했다. 물리/공간모델(CC-08, inch, 20 micro-zone, 4 macro-zone)은 그대로 유지한다.

Macro-zone `z` 내부 5개 micro-zone의 workload 분포에 대해 normalized Shannon entropy `H_z`와 집중도 `C_z=1-H_z`를 계산한다. `V_z`는 dominant-list dispatch 기준 macro-zone workload, `N`은 총 작업자 수, `n_z`는 zone별 정수 작업자 수이다.

```text
d_z = V_z / ΣV_z
p_z = n_z / N
D(n) = 0.5 × Σ |p_z - d_z|
R(n) = Σ C_z × C(n_z, 2)
J(n; lambda) = D(n) + lambda × R(n)
```

- `D(n)`: 수요비중과 작업자비중의 불일치(total-variation distance)
- `R(n)`: 같은 macro-zone에 배치된 작업자 쌍을 수요 집중도 `C_z`로 가중한 혼잡위험
- `lambda = 0`: Phase 3 Volume Proportional의 정수 배치를 정확한 control로 사용
- `lambda > 0`: `D` 손실을 감수하더라도 집중 zone의 작업자 쌍 위험 `R`을 줄이는 정수 배치를 선택 가능

가능한 정수 `[n1,n2,n3,n4]`를 직접 열거하여 `J`가 최소인 배치를 선택한다. 따라서 λ가 일정 임계값을 넘으면 **작업자 1명의 실제 zone 이동**이 목적함수 수준에서 직접 발생한다.

## 4. 기존 결과의 취급

물리모델 정정 및 2026-08-23 정수 목적함수 개정은 이동거리, 이동시간, 혼잡, 대기, release delay proxy, flow time, makespan뿐 아니라 Phase 4 λ 선택에도 영향을 준다. 따라서 이전 방식에서 계산한 λ*와 Phase 5 holdout 결과는 현재 모델의 최종 결과로 사용하지 않는다.

- CC-01 / centimeter 기반 구 결과: 별도 legacy 결과로 취급
- CC-08 / micro20-macro4이지만 **연속 가중치→반올림**을 사용한 Phase 4/5 결과: `results_legacy_pre_20260823_integer_objective/`로 이동
- 새 `results/phase4`, `results/phase5`: 정수 목적함수 재실행용으로 비움

Phase 5는 `phase4_recommendation.json`의 `model_revision`을 검사하여 `2026-08-22-cc08-inch-micro20-macro4-integer-objective-v1-pareto-knee-v1`이 아닌 recommendation을 자동 거부한다.

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
