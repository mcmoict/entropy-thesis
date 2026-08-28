# Phase 3 - 기존 작업자 배치 전략 비교

Phase 3는 당일 피킹 작업량과 당일 사용 가능한 작업자 수는 유지하면서, 작업자를 4개 workforce macro-zone에 **어떻게 분산 배치할 것인지** 비교하는 단계입니다.

비교 방법은 다음과 같습니다.

```text
Observed Baseline
Random
Equal
Volume Proportional
```

Entropy-based 방식은 Phase 4에서 추가합니다.

## 1. 핵심 원칙

- 당일 실제 operator 수를 기본 총 작업자 수로 사용합니다.
- Picking location과 원래 pick sequence는 그대로 사용합니다.
- 비교 방법은 **작업자를 zone에 배치하는 정책만 변경**합니다.
- Picking List를 여러 zone으로 잘라서 재구성하지 않습니다.
- 실제 DES 중 작업자는 자신의 dispatch zone 밖으로 이동하거나 피킹할 수 있습니다.

즉, Phase 3의 질문은 다음과 같습니다.

> **같은 날, 같은 작업량, 같은 인력 수에서 4개 영역에 인력을 어떻게 분산하면 운영 KPI가 달라지는가?**

## 2. 공간 모델

### 2.1 Demand Micro-zone: 20개

```text
M01 ~ M10 : LC-08 ~ LC-17
M11 ~ M20 : RC-08 ~ RC-17
```

CC-08의 x 좌표를 기준으로 left/right를 구분하고, storage location은 같은 side의 08~17 Support anchor 중 y 좌표가 가장 가까운 micro-zone에 귀속합니다.

20개 micro-zone은 수요의 세부 공간 분포와 Phase 4의 entropy concentration 계산에 사용합니다.

### 2.2 Workforce Macro-zone: 4개

```text
Z01 : LC-08 ~ LC-12 = Left / Near
Z02 : LC-13 ~ LC-17 = Left / Far
Z03 : RC-08 ~ RC-12 = Right / Near
Z04 : RC-13 ~ RC-17 = Right / Far
```

작업자의 인력 배치는 이 4개 macro-zone 단위로 결정합니다.

## 3. Picking List의 Macro-zone 귀속

하나의 Picking List는 분할하지 않고 list 내 pick task가 가장 많은 **dominant macro-zone**에 귀속합니다.

동률이면 다음 순서로 결정합니다.

```text
1. pick task 수
2. picked units
3. configured macro-zone order
```

이 귀속은 작업자 dispatch를 위한 논리적 workload pool이며, 실제 route를 해당 zone에 가두는 제약은 아닙니다.

## 4. Workload 기준

기본값은 task 수입니다.

```text
--volume-basis tasks
```

필요하면 unit 기준도 사용할 수 있습니다.

```text
--volume-basis units
```

최종 논문 실험은 `tasks`를 사용합니다.

## 5. 비교 전략

### Observed Baseline

`Picking_Wave.csv`의 실제 operator 배정을 그대로 사용합니다.

### Random

당일 총 작업자 수를 active macro-zone에 난수로 배치합니다. 재현성을 위해 기본 seed는 `42`입니다.

### Equal

active macro-zone에 가능한 균등하게 작업자를 배치합니다.

### Volume Proportional

macro-zone workload 비중에 비례하여 정수 작업자를 배치합니다. Phase 4에서 `λ=0`의 정확한 control이 됩니다.

## 6. 실행

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05
```

방법을 선택하여 실행할 수도 있습니다.

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05 --methods random,equal,volume_proportional
```

주요 옵션:

| 옵션 | 기본값 | 의미 |
|---|---:|---|
| `--methods` | 3개 비교법 | 실행 전략 목록 |
| `--zones` | `4` | workforce macro-zone 수; 현 연구모델은 4 고정 |
| `--workers` | 당일 관측 인원 | 총 작업자 수 override |
| `--volume-basis` | `tasks` | workload 기준 |
| `--minimum-per-active-zone` | `1` | active zone 최소 인원 |
| `--seed` | `42` | Random 재현 seed |
| `--output-dir` | `results/phase3` | 결과 위치 |

DES 관련 `--speed`, `--pick-seconds`, `--edge-capacity`, `--pick-node-capacity`, `--sample-seconds`, `--no-return-to-io` 옵션은 Phase 2와 동일합니다.

## 7. 대표 결과: 2023-01-05

| Method | Mean Flow Time (s) | Conflicts | Wait (s) | Congestion Ratio |
|---|---:|---:|---:|---:|
| Baseline | 673.50 | 174 | 244.73 | 3.52% |
| Random | 669.04 | 207 | 292.29 | 4.17% |
| Equal | 570.36 | 241 | 397.34 | 5.59% |
| Volume | **507.37** | **313** | **512.75** | **7.10%** |

이 예시는 **Volume이 처리시간을 줄이면서 혼잡을 증가시킬 수 있음**을 보여줍니다. 이 관찰이 Phase 4의 엔트로피 기반 다목적 배치 문제를 정의하는 직접적인 배경이 됩니다.

> 위 표는 한 날짜의 예시이며 최종 결론은 Phase 4~6의 다중 날짜 분석과 Frozen Holdout을 기준으로 해석합니다.

## 8. 보조 배치 지표

### Worker Allocation Entropy

4개 macro-zone에 작업자가 얼마나 균등하게 분산되었는지 normalized Shannon entropy로 표현합니다.

### Demand-Worker L1 Gap

수요 비중과 작업자 비중의 차이를 측정합니다. Phase 4의 `D(n)`과 연결되는 배치 적합도 지표입니다.

## 9. 출력 파일

`results/phase3/`:

```text
phase3_summary.csv
phase3_zones.csv
phase3_microzones.csv
phase3_list_zones.csv
phase3_lists.csv
phase3_workers.csv
phase3_congestion.csv
phase3_entropy.csv
phase3_occupancy.csv
phase3_metadata.json
```

`phase3_summary.csv`가 방법별 핵심 KPI 비교의 출발점입니다.

## 10. 다음 단계

Phase 3에서 확인된 **“Volume은 빠르지만 혼잡할 수 있다”**는 trade-off를 바탕으로 Phase 4에서는 20개 micro-zone의 수요 집중도를 목적함수에 추가하여, 가능한 정수 작업자 배치 중 효율성과 혼잡위험의 균형점을 찾습니다.
