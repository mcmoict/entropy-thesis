# Phase 8-RF - Random Forest Adaptive EWA 비교 실험

## 1. 목적

기존 Phase 8의 XGBoost와 **동일한 입력 feature, 동일한 chronological split, 동일한 Frozen Holdout, 동일한 Adaptive λ 선택 규칙**을 유지하고 예측 모델만 Random Forest로 교체합니다.

따라서 이번 비교의 핵심은 다음과 같습니다.

```text
동일한 EWA 후보 배치
        ↓
동일한 Tabular Feature
        ↓
Random Forest KPI 예측
        ↓
Fixed-EWA Flow Guardrail
        ↓
Adaptive λ 선택
        ↓
Frozen Holdout 비교
```

XGBoost와 Random Forest 사이에서 바뀌는 것은 **KPI 예측기**뿐이며, EWA 수식·λ 후보·작업자 정수배치 생성·평가 기준은 바뀌지 않습니다.

## 2. Random Forest 설정

각 KPI별로 독립적인 회귀 모델 1개를 학습합니다.

- `n_estimators = 400`
- `min_samples_leaf = 2`
- `max_features = 0.8`
- `random_state = 42`
- `n_jobs = -1`

예측 대상은 XGBoost와 동일합니다.

1. `mean_flow_time_seconds`
2. `congestion_conflicts`
3. `congestion_wait_seconds`
4. `congestion_delay_ratio`

`entropy_weight(λ)` 자체는 feature에 넣지 않고, λ가 생성한 **실제 작업자 배치 특성**을 입력으로 사용합니다.

## 3. 실행

### 빠른 내부검증

```powershell
python -m entropy_thesis.simulation.phase8_rf --train-only
```

### Frozen Holdout 선택까지만 확인

```powershell
python -m entropy_thesis.simulation.phase8_rf --selection-only
```

### 최종 실행

```powershell
python -m entropy_thesis.simulation.phase8_rf --data-dir data/raw
```

결과는 기본적으로 `results/phase8_rf`에 저장됩니다.

기존 통합 모듈에서도 같은 실험을 실행할 수 있습니다.

```powershell
python -m entropy_thesis.simulation.phase8 --ai-model random_forest --output-dir results/phase8_rf --data-dir data/raw
```

## 4. XGBoost와 비교할 때 볼 항목

교수님 보고용으로는 다음 세 가지를 비교하면 됩니다.

| 비교 항목 | 의미 |
|---|---|
| MAE / RMSE / R² | KPI 예측 자체의 정확도 |
| Adaptive λ 분포 | 모델이 날짜별 운영상황을 얼마나 다르게 판단했는지 |
| Holdout 실제 KPI | 최종 작업자 배치가 Flow·Conflict·Wait·Congestion을 실제로 개선했는지 |

특히 Random Forest의 예측오차가 일부 KPI에서 XGBoost보다 작더라도, **정수 작업자 배치가 Fixed EWA와 동일하게 귀결되면 최종 DES 성능은 동일할 수 있습니다.** 이는 예측 정확도와 최종 의사결정 성능을 분리해 해석해야 한다는 의미입니다.


## 5. 이번 실행 결과

동일한 92일 Calibration, 73일 Internal Train, 19일 Internal Validation, 40일 Frozen Holdout 조건에서 실행한 결과입니다.

### 5.1 KPI 예측 성능

| KPI | RF MAE | XGB MAE | RF RMSE | XGB RMSE | RF R² | XGB R² | 해석 |
|---|---:|---:|---:|---:|---:|---:|---|
| Mean Flow Time | 131.716 | 129.595 | 172.161 | 169.722 | 0.663 | 0.672 | XGBoost 근소 우위 |
| Conflicts | 29.215 | 26.584 | 46.766 | 38.890 | 0.634 | 0.747 | XGBoost 우위 |
| Wait | 77.510 | 77.592 | 127.298 | 122.280 | 0.498 | 0.537 | MAE는 RF 근소 우위, 전체적으로 XGBoost 우위 |
| Congestion Ratio | 0.0222 | 0.0261 | 0.0325 | 0.0387 | 0.277 | -0.024 | Random Forest 우위 |

Random Forest가 모든 KPI에서 열세인 것은 아닙니다. 특히 `congestion_delay_ratio` 예측은 RF가 더 좋았습니다. 반면 Flow, Conflict, Wait의 RMSE/R² 관점에서는 XGBoost가 더 안정적이었습니다.

### 5.2 Internal Validation의 실제 Adaptive 결과

Fixed EWA 대비 변화율은 다음과 같습니다.

| 지표 | XGBoost Adaptive | Random Forest Adaptive |
|---|---:|---:|
| Mean Flow Time | +0.084% | **-0.319%** |
| Conflicts | **-1.111%** | +1.794% |
| Wait | **-3.171%** | +1.620% |
| Congestion Ratio | **-4.239%** | +1.020% |
| Makespan | +0.025% | -0.135% |
| Mean Release Delay | +0.212% | -0.485% |

RF는 Flow를 소폭 줄였지만 혼잡 관련 세 지표가 모두 악화되었습니다. 반면 XGBoost는 Flow를 거의 유지하면서 Conflict / Wait / Congestion Ratio를 낮추는 방향으로 선택했습니다.

### 5.3 Frozen Holdout 40일 결과

Random Forest가 선택한 λ 분포는 다음과 같습니다.

```text
λ=0.00 : 32일
λ=0.05 :  2일
λ=0.10 :  1일
λ=0.25 :  5일
```

하지만 λ 값이 달라도 정수 작업자 배치가 동일해질 수 있습니다. 실제 worker vector를 비교한 결과:

```text
Phase 5 Fixed EWA와 동일한 배치 : 40일
Phase 5 Volume과 동일한 배치    :  0일
새로운 Random Forest 배치        :  0일
```

따라서 RF-Adaptive EWA의 Frozen Holdout 실제 KPI는 Fixed EWA와 **모든 지표에서 동일(변화율 0.000%)**했습니다.

이는 중요한 비교 결과입니다. **Random Forest가 λ를 다르게 예측하더라도 정수 배치 단계에서 동일한 worker vector로 수렴하여 최종 의사결정 차이를 만들지 못했습니다.** 반면 동일 조건에서 XGBoost는 40일 중 1일에 새로운 정수 배치를 선택했습니다.

## 6. 교수님께 설명할 핵심

이번 비교에서는 “XGBoost가 항상 예측 정확도가 가장 높다”라고 주장하지 않습니다. Random Forest는 Congestion Ratio 예측에서는 더 좋은 결과를 보였습니다. 그러나 연구의 최종 목적은 KPI 예측 자체가 아니라 **예측을 이용해 실제 작업자 배치를 개선하는 것**입니다.

Random Forest는 Holdout에서 40일 모두 기존 Fixed EWA와 같은 정수 배치로 귀결되어 최종 성능 변화가 없었습니다. 반면 XGBoost는 내부검증에서 혼잡 지표를 낮추는 방향의 Adaptive 선택을 보였고, Holdout에서도 최소한 한 날짜에서 기존과 다른 배치를 생성했습니다. 따라서 현재 결과는 **예측 성능 + 최종 배치 의사결정 효과를 함께 볼 때 XGBoost를 주 모델로 유지할 근거**가 됩니다.
