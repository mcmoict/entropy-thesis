# Phase 4 Integer Objective Revision — 2026-08-23

## 1. 목적

기존 Phase 4의 연속 가중치 `A_z(lambda)`를 정수 작업자 수로 반올림하는 구조에서는 λ가 변해도 실제 `[n1,n2,n3,n4]`가 동일한 구간이 길게 나타났다. 이번 개정은 λ가 **한 명의 작업자 zone 이동 결정**에 직접 영향을 주도록 가능한 정수 작업자 배치를 직접 평가한다.

## 2. 정수 목적함수

Macro-zone `z`의 dominant-list workload를 `V_z`, 총 작업자 수를 `N`, 정수 작업자 수를 `n_z`, micro-zone normalized Shannon entropy를 `H_z`라고 한다.

```text
d_z = V_z / ΣV_z
p_z = n_z / N
C_z = 1 - H_z

D(n) = 0.5 × Σ |p_z - d_z|
R(n) = Σ C_z × C(n_z, 2)
J(n; lambda) = D(n) + lambda × R(n)
```

- `D(n)`: 수요비중과 작업자비중의 불일치(total-variation distance)
- `R(n)`: 같은 macro-zone에 배치된 작업자 쌍을 해당 zone의 수요 집중도로 가중한 혼잡위험
- `lambda`: 수요 적합도 `D`와 집중-zone 작업자 쌍 위험 `R` 사이 trade-off

제약조건:

```text
Σ n_z = N
n_z: non-negative integer
active zone: n_z >= minimum_per_active_zone
inactive zone: n_z = 0
```

`lambda=0`에서는 Phase 3 Volume Proportional의 정수 배치를 정확한 control로 사용한다.

## 3. 2023-01-05 PRE-DES 검증

실제 입력:

```text
Observed workers = 8
Workload = [822, 417, 254, 503]
C_z     = [0.073863, 0.175485, 0.098158, 0.411893]
```

정수 목적함수 결과:

```text
lambda  allocation   D         R         J         moved workers
0       [3,2,1,2]    0.041082  0.808966  0.041082  0
0.05    [3,2,1,2]    0.041082  0.808966  0.081530  0
0.10    [3,2,1,2]    0.041082  0.808966  0.121979  0
0.25    [3,2,1,2]    0.041082  0.808966  0.243324  0
0.50    [3,2,2,1]    0.163828  0.495230  0.411443  1  (Z04 -> Z03)
0.75    [3,2,2,1]    0.163828  0.495230  0.535250  1
1.00    [3,2,2,1]    0.163828  0.495230  0.659058  1
2.00    [3,2,2,1]    0.163828  0.495230  1.154288  1
4.00    [3,2,2,1]    0.163828  0.495230  2.144749  1
8.00    [3,2,2,1]    0.163828  0.495230  4.125669  1
```

첫 실제 정수 배치 변화는 `lambda=0.5`에서 발생하며, `[3,2,1,2] -> [3,2,2,1]`로 **Z04의 작업자 1명이 Z03으로 이동**한다.

### DES KPI 확인

동일한 2023-01-05 입력/물리모델/seed=42에서 두 고유 정수 배치를 비교하면 다음과 같다.

```text
Allocation   Distance(m)  Conflicts  Wait(s)   Cong(%)  Release(s)  Flow(s)   Makespan(s)  SpatialH(2+)
[3,2,1,2]      8,053.12       313    512.750     7.098     351.940   507.372    15,476.237      0.982378
[3,2,2,1]      8,053.12       241    329.007     4.673     421.044   574.314    15,801.710      0.987687
```

작업자 1명을 `Z04 -> Z03`으로 이동하면 conflicts와 congestion wait는 크게 감소하고 spatial entropy는 소폭 증가하지만, release delay / mean flow time / makespan은 증가한다. 따라서 **혼잡 완화와 처리시간 사이의 trade-off가 실제 DES KPI에서 나타난다.** 이 하루만 Mean Flow Time을 primary KPI로 선택하면 λ=0이 선택된다. 전체 λ*는 반드시 Calibration 92일을 다시 실행해 결정한다.

## 4. 실행 순서

먼저 한 날짜에서 PRE-DES 정수 목적함수와 DES KPI를 연속으로 확인한다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05
```

콘솔 출력 순서:

```text
① zone별 workload / C_z
② λ별 [n1,n2,n3,n4], D, R, J
③ 첫 작업자 이동 λ 및 이동 zone
④ DES KPI by λ
```

단일일자 결과는 `results/phase4/diagnostic_2023-01-05/`에 저장된다. 전체 Phase 4E recommendation을 덮어쓰지 않는다.

단일일자 검증 후 전체 Phase 4A~4E를 다시 실행한다.

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw
```

그 다음 새 recommendation으로 Phase 5 Frozen Holdout을 실행한다.

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```

## 5. 결과 호환성

새 Phase 4 model revision은 다음과 같다.

```text
2026-08-22-cc08-inch-micro20-macro4-integer-objective-v1
```

Phase 5는 위 revision이 아닌 과거 Phase 4 recommendation을 거부한다. 과거 연속 가중치 방식의 Phase 4/5 결과는 `results_legacy_pre_20260823_integer_objective/`에 보존한다.
