# Entropy-Based Picking Workforce Allocation Optimization

실제 물류센터 피킹 데이터를 기반으로 **Shannon Entropy**와 **이산 사건 시뮬레이션(Discrete-Event Simulation, DES)** 을 결합하여, 작업자 배치에 따른 **처리효율과 혼잡의 trade-off**를 분석하는 석사 논문 연구 프로젝트입니다.

본 프로젝트의 핵심 실험은 **Phase 1 ~ Phase 6**으로 구성되며, Phase 8에서 이를 **AI-Adaptive EWA**로 확장합니다. 최종 제안 방법은 20개 demand micro-zone의 수요 집중도를 이용해 4개 workforce macro-zone의 **정수 작업자 배치**를 최적화하고, XGBoost가 날짜별 운영상황에 적합한 λ를 동적으로 선택합니다.

---

## 1. 연구 목적

물동량 비례(Volume Proportional) 배치는 수요량에 따라 인력을 배치하므로 처리효율 측면에서는 유리할 수 있지만, 특정 공간에 작업자가 동시에 집중되어 통로 및 피킹 지점의 혼잡이 증가할 수 있습니다.

본 연구의 핵심 질문은 다음과 같습니다.

> **수요량뿐 아니라 수요의 공간적 집중도까지 고려하여 작업자를 배치하면, 처리효율의 과도한 손실 없이 혼잡을 줄일 수 있는가?**

이를 검증하기 위해 동일한 실제 피킹 데이터와 DES 환경에서 다음 방법을 비교합니다.

```text
Observed Baseline
Random
Equal
Volume Proportional
Entropy-based
```

---

## 2. Phase 1 ~ Phase 6 + Phase 8 AI 확장

| Phase | 내용 | 핵심 목적 |
|---|---|---|
| **Phase 1** | 실제 데이터 로딩 및 Warehouse Graph 구축 | 좌표·피킹 데이터 검증 및 물리 네트워크 생성 |
| **Phase 2** | Observed Baseline DES | 실제 작업자 배정과 피킹 순서를 이용한 기준 시뮬레이션 |
| **Phase 3** | 기존 배치 방법 비교 | Random / Equal / Volume Proportional 비교 |
| **Phase 4** | Entropy 기반 정수 작업자 배치 | 목적함수 정의 및 Pareto-knee 기반 λ Calibration |
| **Phase 5** | Frozen Holdout 검증 | Calibration에서 고정한 λ*를 독립 Holdout에 적용 |
| **Phase 6** | Trade-off / Mechanism / Robustness | 일반화 성능, 배치 변경 메커니즘, 통계적 강건성 분석 |
| **Phase 8** | AI-Adaptive EWA | XGBoost KPI 예측 + Fixed-EWA Flow guardrail 기반 날짜별 Adaptive λ 선택 |

전체 흐름:

```text
Raw Warehouse Data
        ↓
Phase 1  Data Validation / Navigation Graph
        ↓
Phase 2  Observed Baseline DES
        ↓
Phase 3  Random / Equal / Volume Comparison
        ↓
Phase 4  Entropy Integer Allocation + λ Calibration
        ↓
Phase 5  Frozen Holdout Validation
        ↓
Phase 6  Trade-off / Mechanism / Robustness Analysis
        ↓
Phase 8  XGBoost KPI Prediction + Adaptive λ_t
        ↓
Visualization (HTML / Desktop / EXE)
```

---

## 3. 최종 연구 모델

### 3.1 물리 모델

- 원본 CAD 좌표 단위: **inch**
- 거리 변환: `1 inch = 0.0254 m`
- 모든 picker의 공통 시작/종료 지점: **CC-08**
- 기본 I/O node: `SUP:CC-08`
- CC-08 좌표: 약 `(10.2362, 16.0274) m`
- `Picking_Wave.csv`의 원래 pick sequence를 보존
- 좌표가 정의되지 않은 picking location은 임의 생성하지 않고 unresolved로 처리

### 3.2 Demand Micro-zone: 20개

```text
M01 ~ M10 : LC-08 ~ LC-17
M11 ~ M20 : RC-08 ~ RC-17
```

20개 micro-zone은 각 macro-zone 내부의 **수요 공간 집중도**를 계산하는 데 사용합니다.

### 3.3 Workforce Macro-zone: 4개

```text
Z01 : LC-08 ~ LC-12  = Left / Near
Z02 : LC-13 ~ LC-17  = Left / Far
Z03 : RC-08 ~ RC-12  = Right / Near
Z04 : RC-13 ~ RC-17  = Right / Far
```

작업자 배치는 4개 macro-zone 단위로 결정합니다.

Picking list 자체를 여러 zone으로 분할하지는 않습니다. 각 list는 pick task가 가장 많은 **dominant macro-zone**의 workload pool에 귀속되지만, 실제 DES 이동 중에는 작업자가 다른 macro-zone을 통과하거나 그곳에서 피킹할 수 있습니다.

---

## 4. Entropy 기반 정수 작업자 배치

Macro-zone `z`의 workload를 `V_z`, 전체 작업자 수를 `N`, zone별 정수 작업자 수를 `n_z`라 정의합니다.

```text
d_z = V_z / ΣV_z
p_z = n_z / N
```

수요 비중과 작업자 비중의 불일치:

```text
D(n) = 0.5 × Σ |p_z - d_z|
```

각 macro-zone 내부 5개 micro-zone의 normalized Shannon entropy를 `H_z`라 할 때:

```text
C_z = 1 - H_z
```

수요 집중도를 반영한 동시 작업자 위험:

```text
R(n) = Σ C_z × C(n_z, 2)
```

최종 목적함수:

```text
J(n; λ) = D(n) + λR(n)
```

- `D(n)`: workload 비중과 작업자 비중의 불일치
- `R(n)`: 집중된 zone에 여러 작업자가 동시에 배치되는 위험
- `λ = 0`: **Volume Proportional 정수 배치와 동일한 control**
- `λ > 0`: workload 적합도를 일부 양보하면서 집중 zone의 작업자 중첩 위험 감소 가능

가능한 정수 worker vector `[n1, n2, n3, n4]`를 직접 평가하여 `J(n;λ)`가 최소인 배치를 선택합니다.

---

## 5. 데이터 및 실험 조건

### 5.1 데이터 출처 및 라이선스

본 연구에서 사용한 실제 물류센터 주문·피킹 데이터는 Mendeley Data에 공개된 다음 데이터셋을 기반으로 합니다.

- **Dataset:** *Order Picking Dataset from a Warehouse of a Footwear Manufacturing Company*
- **Contributor:** Rodrigo Furlan de Assis
- **Repository:** [Mendeley Data](https://data.mendeley.com/datasets/pf2w725pw3/1)
- **Version:** 1
- **DOI:** [`10.17632/pf2w725pw3.1`](https://doi.org/10.17632/pf2w725pw3.1)
- **License:** [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/)

원 데이터셋은 실제 신발 제조기업의 Warehouse Management System(WMS)에서 수집된 자료를 기반으로 하며, 공개 과정에서 기밀성 보호를 위해 **anonymization 및 randomization**이 적용되었습니다.

본 저장소의 `data/raw_original/` 디렉터리는 연구 재현성을 위해 원 데이터셋에서 제공된 자료를 보존한 영역이며, 해당 데이터 자료의 이용 및 재배포는 원 데이터셋의 **CC BY 4.0** 조건을 따릅니다.

`data/raw/` 디렉터리는 본 연구의 시뮬레이션 및 분석에 사용하기 위해 원 데이터셋에서 필요한 자료를 선택하거나 연구 환경에 맞게 정리한 입력 데이터입니다. 원 데이터 및 그 가공본을 이용하거나 재배포할 경우에는 원 데이터셋의 출처, DOI 및 CC BY 4.0 라이선스를 함께 표시해야 합니다.

> **Dataset attribution:** Rodrigo Furlan de Assis, *Order Picking Dataset from a Warehouse of a Footwear Manufacturing Company*, Mendeley Data, Version 1, DOI: `10.17632/pf2w725pw3.1`, licensed under CC BY 4.0.

> **Note:** 위 CC BY 4.0 표기는 Mendeley 원 데이터셋 및 해당 데이터를 기반으로 한 자료에 대한 라이선스 안내입니다. 본 저장소의 독립적인 연구 코드 전체에 동일한 라이선스가 자동 적용된다는 의미는 아니며, 연구 코드의 라이선스는 별도의 `LICENSE` 파일을 추가하는 경우 그 조건을 따릅니다.

### 5.2 데이터 규모 및 실험 조건

현재 최종 모델 기준 주요 데이터 규모:

| 항목 | 값 |
|---|---:|
| Storage Locations | 2,292 |
| Support Points | 44 |
| Products | 208 |
| Customer Order Lines | 122,370 |
| Picking Tasks | 215,192 |
| `(wave, operator)` Picking Lists | 9,796 |
| 전체 데이터의 Operators | 22 |
| Navigation Graph Nodes / Edges | 510 / 534 |
| Connected Components | 1 |
| Resolved Picking Tasks | 191,583 (89.03%) |
| Fully-valid Picking Lists | 7,402 / 9,796 |

Phase 4 실험 날짜:

```text
Operating dates : 176
Eligible dates  : 132
Calibration     : 92 dates
Frozen Holdout  : 40 dates
Split strategy  : chronological 70 / 30
```

각 운영일의 비교 방법에는 해당 날짜에서 실제 관측된 operator 수를 기본 총 작업자 수로 사용합니다.

---

## 6. 최종 실험 결과

### 6.1 Phase 4 - λ Calibration

λ 후보:

```text
0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2, 4, 8
```

Calibration 92일에서 Mean Flow Time과 `Conflicts / Wait / Congestion Ratio`를 함께 평가한 Pareto frontier의 knee point:

```text
Selected λ* = 0.25
```

`λ=0`(Volume control) 대비 Calibration 평균:

| KPI | 변화 |
|---|---:|
| Mean Flow Time | **+4.82%** |
| Conflicts | **−11.40%** |
| Congestion Wait | **−22.66%** |
| Congestion Ratio | **−14.57%** |
| Composite Congestion | **−16.21%** |

### 6.2 Phase 5 - Frozen Holdout 40일

| Method | Mean Flow Time (s) | Conflicts | Wait (s) | Congestion |
|---|---:|---:|---:|---:|
| Observed Baseline | 794.82 | 103.38 | 180.97 | 5.04% |
| Random | 762.64 | 138.98 | 304.60 | 7.33% |
| Equal | 858.62 | 116.05 | 244.60 | 6.30% |
| Volume Proportional | **680.61** | 155.48 | 345.34 | 8.48% |
| Entropy-based (λ*=0.25) | 699.91 | 148.48 | 306.27 | 7.99% |

Entropy vs Volume:

| KPI | Holdout 결과 |
|---|---:|
| Mean Flow Time cost | **+2.84%** |
| Conflicts reduction | **4.50%** |
| Wait reduction | **11.31%** |
| Congestion Ratio reduction | **5.85%** |
| Composite Congestion reduction | **7.22%** |

Calibration과 Holdout 모두 **“효율 손실 + 혼잡 감소”**라는 동일한 Pareto trade-off 방향을 보였습니다.

### 6.3 Phase 6 - 메커니즘 및 통계

```text
Same as Volume : 32 / 40 dates
Changed        :  8 / 40 dates
```

Entropy와 Volume의 정수 작업자 배치가 달라진 8일 모두에서 작업자가 상대적으로 **높은 수요 집중도 zone에서 낮은 집중도 zone 방향으로 이동**했고, 이 중 6일에서는 실제 DES composite congestion도 개선되었습니다.

전체 Holdout 40일 paired 통계:

| KPI | 평균 개선 방향 | Wilcoxon p | Holm p |
|---|---:|---:|---:|
| Mean Flow Time | −2.84% | 0.0173 | 0.0692 |
| Conflicts | +4.50% | 0.0929 | 0.2787 |
| Wait | +11.31% | 0.1235 | 0.2787 |
| Congestion Ratio | +5.85% | 0.2076 | 0.2787 |

따라서 본 연구는 **Entropy가 Volume보다 모든 KPI에서 우월하다**고 주장하지 않습니다. 최종 결과는 수요 집중도를 목적함수에 포함함으로써 일부 운영일의 실제 정수 인력배치를 변경하고, **처리효율과 혼잡 사이의 대안적 Pareto 배치 정책을 구성할 수 있음**을 보여주는 것으로 해석합니다.

---

## 7. 개발 환경

### Python

```text
Python >= 3.13
```

### 권장 Conda 환경

Windows PowerShell:

```powershell
conda deactivate
conda env remove -n thesis-env
conda create -n thesis-env python=3.13 -y
conda activate thesis-env
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

개발·테스트·Notebook 환경까지 사용할 경우:

```powershell
python -m pip install -e ".[dev,notebook]"
python -m pip install networkx
```

설치 확인:

```powershell
python --version
python -c "import pandas, networkx, simpy, scipy; print('OK')"
```

---

## 8. Phase 실행 방법

프로젝트 루트에서 실행합니다.

### Phase 1 - 데이터 / Warehouse Graph 검증

```powershell
python -m entropy_thesis.simulation.phase1 --data-dir data/raw
```

### 테스트

```powershell
python -m pytest
```

### Phase 2 - Observed Baseline DES

```powershell
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05
```

### Phase 3 - Random / Equal / Volume 비교

```powershell
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05
```

### Phase 4 - Entropy λ Calibration

```powershell
python -m entropy_thesis.simulation.phase4 --data-dir data/raw
```

최종 λ*와 Calibration/Holdout 날짜는 다음 파일에 저장됩니다.

```text
results/phase4/phase4_recommendation.json
```

### Phase 5 - Frozen Holdout 검증

```powershell
python -m entropy_thesis.simulation.phase5 --data-dir data/raw
```

Phase 5에서는 Phase 4의 λ*와 Holdout 날짜를 그대로 사용하며 **λ 재선택 및 Holdout 재분할을 하지 않습니다.**

### Phase 6 - Trade-off / Mechanism / Robustness

```powershell
python -m entropy_thesis.simulation.phase6
```

Phase 6 역시 λ를 다시 선택하지 않고 Frozen Holdout 결과를 분석합니다.

### Phase 8 - AI-Adaptive EWA

Phase 8 추가 package 설치:

```powershell
python -m pip install -e .[ai]
```

먼저 Calibration 데이터만으로 학습 및 내부 날짜 검증을 수행할 수 있습니다.

```powershell
python -m entropy_thesis.simulation.phase8 --train-only
```

최종 Frozen Holdout 40일까지 포함한 AI-Adaptive EWA 검증:

```powershell
python -m entropy_thesis.simulation.phase8 --data-dir data/raw
```

Phase 8은 `phase4_metadata.json`의 기존 DES 조건을 자동 상속하며, XGBoost에는 λ 숫자 자체를 직접 입력하지 않고 **λ가 생성한 후보 정수 작업자 배치와 운영상태 feature**를 입력합니다. AI가 예측한 Flow / Conflict / Wait / Congestion을 이용해 **고정 EWA(λ*=0.25)보다 예측 Flow가 나빠지지 않는 후보 중 예측 혼잡지수가 가장 낮은 λ**를 날짜별로 선택합니다.

---

## 9. 결과 디렉터리

```text
results/
├─ phase2/     Observed Baseline DES 결과
├─ phase3/     Random / Equal / Volume 비교 결과
├─ phase4/     λ Calibration / Pareto 분석 / recommendation
├─ phase5/     Frozen Holdout 5방법 비교 및 paired 통계
├─ phase6/     Generalization / mechanism / bootstrap / Holm 분석
├─ phase8/     XGBoost / Adaptive λ / Frozen Holdout AI-EWA 검증
└─ figures/    Picking Animation 시각화 결과
```

대표 결과 파일:

```text
results/phase4/phase4_recommendation.json
results/phase4/phase4_pareto_analysis.csv
results/phase5/phase5_daily_summary.csv
results/phase5/phase5_comparison_summary.csv
results/phase5/phase5_paired_comparison.csv
results/phase6/phase6_calibration_holdout_generalization.csv
results/phase6/phase6_pareto_metric_statistics.csv
results/phase6/phase6_changed_dates.csv
```

각 Phase의 전체 출력 파일 정의는 Phase별 README를 참고합니다.

---

## 10. 주요 KPI

| KPI | 의미 |
|---|---|
| `total_distance_m` | CC-08 출발 → 피킹 → CC-08 복귀 총 이동거리 |
| `congestion_conflicts` | capacity-limited resource에 즉시 진입하지 못한 contention event 수 |
| `congestion_wait_seconds` | resource contention으로 발생한 총 대기시간 |
| `congestion_delay_ratio` | `Wait / (Movement + Wait)` |
| `mean_release_delay_seconds` | picking list가 늦게 시작된 평균 지연시간 |
| `mean_flow_time_seconds` | list release부터 완료까지의 평균 시간 |
| `makespan_seconds` | 해당 운영일의 전체 작업 완료시간 |
| `mean_spatial_entropy_normalized` | 시간에 따른 작업자 공간 분산 정도 |
| `mean_spatial_entropy_multiworker` | active worker가 2명 이상인 시점만 계산한 공간 엔트로피 |

> `congestion_conflicts`는 실제 사람끼리 부딪힌 물리적 충돌 횟수가 아니라 **DES resource contention event**입니다.

---

## 11. Picking Animation Visualization

연구 결과 확인 및 논문 시연을 위해 HTML Viewer와 PySide6 Desktop Viewer를 제공합니다.

### 11.1 전체 날짜 JSON + HTML 생성

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --all-dates
```

생성 구조:

```text
results/figures/
├─ picking_animation_actual.html
└─ picking_animation_actual_data/
   ├─ 2023-01.json
   ├─ 2023-02.json
   ├─ ...
   └─ 2023-10.json
```

생성과 동시에 localhost 서버 실행:

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --all-dates --serve
```

기존 JSON을 유지하고 HTML만 다시 생성:

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --html-only
```

HTML 재생성 후 바로 확인:

```powershell
python -m entropy_thesis.visualization.picking_animation_actual --html-only --serve
```

### 11.2 Desktop Viewer

```powershell
python -m pip install PySide6
python -m entropy_thesis.visualization.picking_animation_desktop
```

특정 날짜의 Entropy 시나리오:

```powershell
python -m entropy_thesis.visualization.picking_animation_desktop --date 2023-08-30 --method entropy
```

지원 method:

```text
observed / equal / random / volume / entropy
```

### 11.3 Windows EXE 빌드

```powershell
python -m pip install PySide6 pyinstaller orjson
.\src\entropy_thesis\visualization\build_PickingSimulation.bat
```

실행:

```powershell
.\dist\PickingSimulation.exe
```

Desktop/EXE Viewer는 연구 결과를 다시 계산하지 않고 기존 DES 월별 JSON을 읽어 재생합니다.

상세 사용법: [`src/entropy_thesis/visualization/README.md`](src/entropy_thesis/visualization/README.md)


### 11.4 발표·시연용 권장 날짜

Desktop/EXE Viewer를 이용한 발표 시연에서는 전체 **132개 Eligible 운영일** 중에서, Entropy 기반 배치의 특성이 화면에서 명확하게 나타나는 날짜를 선택하여 사용할 수 있습니다.

시연용 날짜 선정은 논문의 통계적 검증과는 별개이며, **시각적 설명을 위한 사례 선택**입니다. 따라서 특정 날짜의 우수한 결과를 전체 실험 결과로 일반화하지 않습니다.

#### 처리시간 개선 효과 시연

아래 날짜들은 `simulation_end_seconds` 기준으로 Entropy가 비교 방법보다 전체 작업을 더 빨리 종료하는 사례입니다.

| 비교 | 권장 날짜 | 비교 방법 종료시간 | Entropy 종료시간 | 단축시간 | 개선율 |
|---|---|---:|---:|---:|---:|
| Baseline vs Entropy | **2023-03-29** | 5,446.88 s | 2,102.91 s | 3,343.97 s | **61.39%** |
| Equal vs Entropy | **2023-08-28** | 4,192.42 s | 2,236.83 s | 1,955.59 s | **46.65%** |
| Random vs Entropy | **2023-09-01** | 3,713.28 s | 2,062.95 s | 1,650.32 s | **44.44%** |

예를 들어 Baseline 대비 `2023-03-29`는 전체 작업 종료시간이 약 **61.39% 단축**되어 애니메이션에서 처리시간 차이를 가장 극명하게 보여줄 수 있습니다.

단, 이 날짜는 다음과 같이 혼잡 지표가 증가합니다.

```text
2023-03-29

Baseline Conflicts : 194
Entropy Conflicts  : 367

Baseline Wait      : 263.52 s
Entropy Wait       : 484.17 s
```

따라서 `2023-03-29`는 **처리시간 개선 사례**로 사용하며, Entropy가 모든 KPI를 동시에 개선하는 사례로 해석하지 않습니다.

#### 혼잡 개선 효과 시연

Baseline 대비 Entropy의 Conflicts 및 Congestion Wait 감소가 큰 날짜는 다음과 같습니다.

| 날짜 | Conflicts | Conflicts 개선 | Wait | Wait 개선 | 종료시간 변화 |
|---|---|---:|---|---:|---:|
| **2023-01-25** | 26 → 13 | **50.00% 감소** | 52.21 → 18.83 s | **63.93% 감소** | **1.66% 단축** |
| **2023-08-17** | 232 → 159 | **31.47% 감소** | 636.43 → 454.22 s | **28.63% 감소** | **10.62% 단축** |
| 2023-03-17 | 325 → 242 | 25.54% 감소 | 1,044.88 → 677.24 s | 35.18% 감소 | **80.47% 증가** |

`2023-01-25`는 Conflicts와 Wait의 감소율이 가장 큰 대표 사례입니다.

다만 `2023-03-17`은 혼잡은 개선되지만 전체 작업 종료시간이 크게 증가하므로 발표용 대표 사례로는 권장하지 않습니다.

#### 종합 시연 권장 사례

**2023-08-17**은 처리시간과 혼잡이 동시에 개선되는 가장 설명하기 쉬운 사례입니다.

```text
Baseline → Entropy

Conflicts
232 → 159
31.47% 감소

Congestion Wait
636.43 s → 454.22 s
28.63% 감소

Simulation End
1965.76 s → 1756.93 s
10.62% 단축
```

따라서 발표 시 다음과 같이 구분하여 시연하는 것을 권장합니다.

```text
[처리시간 개선 사례]

Baseline → Entropy : 2023-03-29
Equal    → Entropy : 2023-08-28
Random   → Entropy : 2023-09-01


[혼잡 개선 사례]

Baseline → Entropy : 2023-01-25


[처리시간 + 혼잡 동시 개선 대표 사례]

Baseline → Entropy : 2023-08-17
```

특히 `2023-08-17`은 Entropy 기반 작업자 배치가 **Conflicts, Congestion Wait, 전체 작업 종료시간을 동시에 개선하는 사례**이므로 논문의 핵심 개념인 **처리효율과 혼잡 간 trade-off 및 균형적 작업자 배치**를 시각적으로 설명하는 데 적합합니다.

> **해석 주의:** 위 날짜들은 전체 132개 Eligible 운영일 중 시각화 시연에 적합한 대표 사례입니다. 논문의 성능 주장은 Phase 4 Calibration 및 Phase 5 Frozen Holdout 전체 결과와 paired 통계에 기반하며, 특정 날짜의 시연 결과만으로 전체 성능을 주장하지 않습니다.


---

## 12. 프로젝트 구조

```text
entropy-thesis/
│
├─ README.md                  # 프로젝트 전체 안내서
├─ AGENTS.md                  # AI/코딩 에이전트용 프로젝트 규칙
├─ docs/
│  └─ phases/
│     ├─ README_PHASE1.md
│     ├─ README_PHASE2.md
│     ├─ README_PHASE3.md
│     ├─ README_PHASE4.md
│     ├─ README_PHASE5.md
│     └─ README_PHASE6.md
│
├─ pyproject.toml
├─ requirements.txt
├─ configs/
├─ data/
│  ├─ raw_original/             # Mendeley Data 원본 자료
│  ├─ raw/                      # 본 연구의 시뮬레이션 입력 데이터
│  ├─ raw_analysis/             # 원본 데이터 분석·검증 자료
│  └─ processed/                # 전처리·생성 데이터
│
├─ src/entropy_thesis/
│  ├─ allocation/
│  ├─ simulation/
│  │  ├─ phase1.py
│  │  ├─ phase2.py
│  │  ├─ phase3.py
│  │  ├─ phase4.py
│  │  ├─ phase5.py
│  │  └─ phase6.py
│  └─ visualization/
│     ├─ picking_animation.py
│     ├─ picking_animation_actual.py
│     ├─ picking_animation_desktop.py
│     ├─ PickingSimulation.spec
│     ├─ build_PickingSimulation.bat
│     └─ README.md
│
├─ results/
│  ├─ phase2/
│  ├─ phase3/
│  ├─ phase4/
│  ├─ phase5/
│  ├─ phase6/
│  └─ figures/
│
├─ notebooks/
└─ tests/
```

루트에는 전체 안내서인 `README.md`와 에이전트 규칙인 `AGENTS.md`만 두고, Phase별 상세 연구 문서는 `docs/phases/`에 모읍니다. 과거 모델 정정 기록과 중간 결과 문서는 최종 Phase 문서에 통합하여 중복 문서를 유지하지 않습니다.

---

## 12.1 Phase 8 검토용 소스 압축

`.git`, `data/raw`, `dist`, 대용량 시각화 JSON을 제외하고 Phase 8 검토에 필요한 파일만 묶습니다. 프로젝트 루트의 PowerShell에서 실행합니다.

```powershell
$items=@("src","tests","docs","results/phase4","results/phase5","results/phase6","results/phase8","README.md","pyproject.toml","requirements.txt","requirements-ai.txt","requirements-lock.txt","requirements-pip.txt","environment.yml","environment-full.yml") | Where-Object { Test-Path $_ }; tar -a -c -f ("entropy-thesis_phase8_upload_" + (Get-Date -Format "yyyyMMdd_HHmm") + ".zip") $items
```

동일 명령은 `pack_phase8_upload.ps1`로도 제공됩니다.

---

## 13. 상세 문서

루트 README는 프로젝트 전체 안내서이며, 각 Phase의 상세 모델링·실행 옵션·출력 파일·최종 결과는 아래 문서에 분리합니다.

- [Phase 1 - 실제 데이터 검증 및 Warehouse Graph](docs/phases/README_PHASE1.md)
- [Phase 2 - Observed Baseline DES](docs/phases/README_PHASE2.md)
- [Phase 3 - 기존 작업자 배치 전략 비교](docs/phases/README_PHASE3.md)
- [Phase 4 - Entropy 정수 배치 및 λ Calibration](docs/phases/README_PHASE4.md)
- [Phase 5 - Frozen Holdout Validation](docs/phases/README_PHASE5.md)
- [Phase 6 - Trade-off / Mechanism / Robustness](docs/phases/README_PHASE6.md)
- [Phase 8 - AI-Adaptive Entropy Workforce Allocation](docs/phases/README_PHASE8.md)
- [Picking Animation Visualization](src/entropy_thesis/visualization/README.md)

문서 관리 원칙은 **`README.md = 전체 안내`, `docs/phases = 연구 단계별 상세`, `visualization/README.md = 시각화 실행`**입니다. `MODEL_REVISION_*`, `PHASE4_*`, `PHASE6_RESULTS_*`, `README_PHASE*_Old.md`와 같은 중간 문서는 최종 Phase 문서에 내용을 통합한 뒤 제거했습니다.

---

## 14. 재현성 및 해석 원칙

최종 논문 실험에서는 다음 원칙을 유지합니다.

1. Phase 4 이전에 Calibration / Holdout을 분리합니다.
2. Holdout은 λ 선택에 사용하지 않습니다.
3. λ*는 Calibration에서 한 번만 선택합니다.
4. Phase 5에서 λ를 다시 선택하지 않습니다.
5. Holdout 날짜를 다시 샘플링하지 않습니다.
6. Phase 6에서도 λ 또는 Holdout을 재조정하지 않습니다.
7. Phase 8의 AI 학습은 Phase 4 Calibration 날짜만 사용하며, 동일 날짜의 λ 후보가 Train/Validation에 동시에 들어가지 않도록 날짜 단위 chronological split을 사용합니다.
8. Phase 8의 Frozen Holdout에서 Adaptive λ 선택은 XGBoost 예측 KPI만 사용하며, 선택이 끝난 뒤 선택된 정수배치만 실제 DES로 평가합니다. 기존 Phase 5와 동일한 배치는 저장된 실제 결과를 재사용합니다.
9. Entropy와 Volume의 배치가 달라진 날짜만을 이용한 분석은 **메커니즘 설명용**이며 전체 Holdout 통계를 대체하지 않습니다.

현재 모델의 congestion은 통로 및 pick node의 capacity-based contention을 표현합니다. 실제 사람 간 회피행동, 안전거리, 연속 보행밀도, 작업자별 보행속도 차이 등의 물리적 행동까지 직접 예측하는 모델은 아닙니다.

또한 40일 Holdout 전체의 Holm 보정 검정에서는 4개 Pareto KPI가 0.05 수준의 유의성을 통과하지 않았습니다. 따라서 논문 결론은 **Entropy가 모든 상황에서 Volume보다 우월하다**가 아니라, **수요 집중도를 고려하여 효율성과 혼잡 사이의 대안적 Pareto 작업자 배치 정책을 구성할 수 있다**는 수준으로 해석합니다.

---

## 15. 권장 최종 실행 순서

```powershell
conda activate thesis-env

# Phase 1
python -m entropy_thesis.simulation.phase1 --data-dir data/raw

# Test
python -m pytest

# Phase 2 sanity check
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05

# Phase 3 sanity check
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05

# Phase 4 Calibration
python -m entropy_thesis.simulation.phase4 --data-dir data/raw

# Phase 5 Frozen Holdout
python -m entropy_thesis.simulation.phase5 --data-dir data/raw

# Phase 6 Final Analysis
python -m entropy_thesis.simulation.phase6

# Phase 8 AI dependencies
python -m pip install -e .[ai]

# Phase 8 quick training / internal validation
python -m entropy_thesis.simulation.phase8 --train-only

# Phase 8 final frozen-holdout evaluation
python -m entropy_thesis.simulation.phase8 --data-dir data/raw

# Visualization JSON / HTML
python -m entropy_thesis.visualization.picking_animation_actual --all-dates

# Desktop Viewer
python -m entropy_thesis.visualization.picking_animation_desktop
```

---

## 16. 핵심 결론

현재 최종 실험에서 Entropy 기반 작업자 배치는 Volume Proportional 대비 Holdout 평균 **Mean Flow Time이 약 2.84% 증가**했지만, **Conflicts는 약 4.50%, Congestion Wait는 약 11.31%, Congestion Ratio는 약 5.85% 감소**했습니다.

이는 제안 방법이 단순히 가장 빠른 작업자 배치를 찾는 것이 아니라, **수요량과 수요의 공간적 집중도를 함께 고려하여 처리효율과 혼잡 사이의 균형점을 선택하는 작업자 배치 정책**임을 보여줍니다.
