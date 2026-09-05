# Phase 8 - AI-Adaptive Entropy Workforce Allocation (AI-EWA)

## 1. 목적

Phase 4에서는 Calibration 92일 전체를 기준으로 Pareto-knee를 계산해 **고정 λ*=0.25**를 선택했고, Phase 5에서는 이를 Frozen Holdout 40일에 그대로 적용했습니다.

Phase 8의 목적은 기존 EWA 수식과 정수 작업자 배치 알고리즘을 대체하는 것이 아니라, 날짜별 운영상황에 따라 λ를 동적으로 선택하도록 AI를 결합하는 것입니다.

```text
운영상태 + λ별 후보 정수배치
          ↓
     XGBoost KPI 예측
          ↓
Flow / Conflicts / Wait / Congestion
          ↓
Fixed-EWA Flow Guardrail
          ↓
예측 Flow가 λ*=0.25보다 나쁘지 않은 후보만 허용
          ↓
예측 혼잡지수 최소 후보 선택
          ↓
      Adaptive λ_t
          ↓
기존 EWA 목적함수 J(n;λ_t)
          ↓
    최종 정수 작업자 배치
```

AI 의사결정 규칙은 다음과 같습니다.

\[
\lambda_t^{AI}
=
\arg\min_{\lambda\in\Lambda}
\widehat{C}_{t}(\lambda)
\]

subject to

\[
\widehat{F}_{t}(\lambda)
\le
\widehat{F}_{t}(\lambda^*)
\]

여기서 `λ*=0.25`는 Phase 4에서 이미 결정된 global Pareto-knee이고, `F`는 Mean Flow Time, `C`는 Conflicts / Wait / Congestion Ratio의 λ=0 상대비율 평균입니다.

즉 **AI는 고정 EWA보다 예측 처리효율을 악화시키지 않는 범위에서 혼잡이 더 낮을 것으로 예상되는 λ를 선택하고, 실제 작업자 배치는 기존 EWA가 수행**합니다.

---

## 2. 왜 Tabular AI인가

Phase 8의 입력은 이미지·자연어가 아니라 다음과 같은 구조화된 수치형 변수입니다.

- 당일 Picking List 수
- Pick Task / Pick Unit 수
- 작업자 수
- Z01~Z04 workload share
- Z01~Z04 micro-zone concentration
- λ가 생성한 후보 정수 작업자 수 `n_z`
- 수요-인력 불일치 `D(n)`
- 혼잡위험 proxy `R(n)`
- Volume 대비 이동 작업자 수
- 작업자당 task / unit
- macro-zone workload entropy

따라서 본 문제는 전형적인 **structured tabular regression** 문제로 정의합니다.

`entropy_weight(λ)` 자체는 XGBoost 입력 feature에서 제외합니다. λ는 정수 작업자 배치를 만드는 제어 파라미터이며, 같은 작업자 배치를 만드는 서로 다른 λ가 DES 성능 자체를 다르게 만들지는 않기 때문입니다. AI는 λ 숫자를 직접 학습하는 대신 **λ가 만든 실제 후보 배치 특성**을 학습합니다.

---

## 3. 예측 KPI

XGBoost는 각 `(date, candidate allocation)`에 대해 다음 네 KPI를 각각 회귀 예측합니다.

1. `mean_flow_time_seconds`
2. `congestion_conflicts`
3. `congestion_wait_seconds`
4. `congestion_delay_ratio`

이 중 Mean Flow Time은 처리효율 guardrail에 사용하고, 나머지 세 지표는 예측 혼잡지수를 계산하는 데 사용합니다.

---

## 4. 학습 데이터와 데이터 누수 방지

Phase 4 Calibration 결과는 92일 × 10개 λ 후보 = 920개 row입니다. 다만 같은 날짜의 여러 λ row는 동일한 운영상황을 공유하므로 row 단위 random split을 사용하면 데이터 누수 가능성이 있습니다.

Phase 8은 다음 원칙을 사용합니다.

```text
Calibration 92일
├─ 앞쪽 80% 날짜: Internal Train 73일
└─ 뒤쪽 20% 날짜: Internal Validation 19일

Frozen Holdout 40일
└─ 최종 AI-EWA out-of-sample 평가 전용
```

같은 날짜의 λ 후보들은 반드시 동일 split에 들어갑니다.

---

## 5. XGBoost 선택 근거와 비교모델

Phase 8에서는 XGBoost를 주 모델로 사용하지만, 내부 날짜 검증에서 다음 모델을 함께 비교합니다.

- Linear Regression
- Random Forest
- XGBoost

각 KPI별로 MAE, RMSE, R²를 기록합니다.

```text
results/phase8/phase8_model_benchmark.csv
```

현재 Calibration 내부검증에서 XGBoost는 Mean Flow Time, Conflicts, Wait에서 경쟁력이 높았으며, Congestion Ratio는 Random Forest가 더 나은 결과를 보였습니다. 따라서 논문에서는 “XGBoost가 모든 KPI에서 절대적으로 우수하다”가 아니라, **Tabular 비선형 관계를 학습하기 위한 주 모델로 XGBoost를 적용하고 비교모델 성능을 함께 제시**하는 방식으로 해석합니다.

---

## 6. 날짜별 Adaptive λ 선택

각 날짜에 대해 Phase 4와 동일한 λ 후보 집합을 사용합니다.

```text
0, 0.05, 0.1, 0.25, 0.5, 0.75, 1, 2, 4, 8
```

각 λ는 먼저 기존 EWA 목적함수에 의해 정수 작업자 배치를 만듭니다.

```text
λ
↓
J(n;λ)=D(n)+λR(n)
↓
후보 worker vector
↓
XGBoost KPI prediction
```

그 뒤 다음 순서로 선택합니다.

```text
1. λ*=0.25 후보의 predicted Flow를 기준값으로 설정
2. predicted Flow <= fixed EWA predicted Flow 후보만 통과
3. 통과 후보의 predicted congestion index 계산
4. predicted congestion index가 가장 낮은 λ 선택
5. 동일하면 더 작은 λ 선택
```

별도의 임의 허용률 5%, 10% 같은 값을 추가하지 않고 **기존 fixed EWA 자체를 non-inferiority 기준**으로 사용한다는 점이 핵심입니다.

---

## 7. Frozen Holdout 누수 방지

최종 Holdout에서는 10개 λ 후보를 모두 실제 DES로 돌린 뒤 좋은 λ를 고르지 않습니다.

Phase 5가 이미 저장한 다음 **사전 정보**만 사용하여 10개 후보의 Tabular feature를 재구성합니다.

- 날짜별 workload
- workload share
- micro-zone concentration
- worker count
- Picking List / Task / Unit 수

그 다음 XGBoost 예측으로 Adaptive λ를 먼저 결정합니다.

```text
Phase 5 Frozen Holdout input
          ↓
10개 λ 후보 정수배치 생성
          ↓
XGBoost 예측
          ↓
Adaptive λ 확정
          ↓
선택된 배치만 실제 DES
```

선택된 정수배치가 Phase 5의 Fixed EWA 또는 Volume 배치와 동일한 경우 기존 Phase 5 실제 결과를 재사용합니다. 둘과 다른 **새로운 정수배치일 때만 추가 DES를 실행**합니다.

이 구조는 Holdout 실제 KPI를 AI 선택 전에 참조하지 않으므로 out-of-sample 평가 원칙을 유지합니다.

---

## 8. 현재 Selection-only 결과

최신 Phase 4/5 결과를 기준으로 `--selection-only`를 실행하면 다음과 같습니다.

```text
Adaptive λ 분포
λ=0.00 : 31일
λ=0.05 :  2일
λ=0.10 :  1일
λ=0.25 :  5일
λ=2.00 :  1일
```

정수 작업자 배치를 비교하면:

```text
Phase 5 Fixed EWA 결과 재사용 : 39일
Phase 5 Volume 결과 재사용    :  0일
새로운 Phase 8 DES 필요       :  1일
```

새로운 배치가 필요한 날짜는 현재 **2023-08-03**이며, AI 선택은 다음과 같습니다.

```text
Adaptive λ = 2.0
AI worker vector = 3|0|1|2
기존 Fixed EWA   = 4|0|1|1
```

따라서 전체 Holdout 40일을 10개 λ씩 다시 시뮬레이션할 필요가 없고, 현재 결과에서는 사실상 이 한 날짜의 새로운 배치만 추가 DES하면 최종 Holdout 비교를 완성할 수 있습니다.

---

## 9. 내부 날짜 검증 결과

Calibration의 마지막 19일을 Internal Validation으로 사용했을 때 AI-Adaptive EWA는 Fixed EWA 대비 다음 결과를 보였습니다.

```text
Mean Flow Time       : +0.08%
Conflicts            : -1.11%
Congestion Wait      : -3.17%
Congestion Ratio     : -4.24%
Makespan             : +0.03%
```

이는 **처리효율을 거의 유지하면서 혼잡 지표를 추가로 낮추는 방향**으로 AI 적용 가능성이 있음을 보여주는 내부 검증 결과입니다. 최종 논문 주장은 반드시 Frozen Holdout의 selected-only DES 완료 결과를 기준으로 합니다.

---

## 10. 설치

Phase 8은 추가 AI package가 필요합니다.

```powershell
conda activate thesis-env
python -m pip install -e .[ai]
```

또는:

```powershell
python -m pip install -r requirements-ai.txt
```

---

## 11. 실행

### 11.1 학습 / 내부검증만

```powershell
python -m entropy_thesis.simulation.phase8 --train-only
```

### 11.2 Holdout λ 선택까지만 확인

새로운 Holdout DES는 실행하지 않습니다.

```powershell
python -m entropy_thesis.simulation.phase8 --selection-only
```

### 11.3 최종 Phase 8

AI가 선택한 새로운 정수배치가 있을 때만 해당 날짜의 DES를 추가 실행합니다.

```powershell
python -m entropy_thesis.simulation.phase8 --data-dir data/raw
```

Phase 8은 `phase4_metadata.json`의 기존 DES 조건을 자동으로 읽어 그대로 사용합니다.

---

## 12. 주요 결과 파일

```text
results/phase8/
├─ phase8_training_dataset.csv
├─ phase8_model_benchmark.csv
├─ phase8_feature_importance.csv
├─ phase8_internal_validation_selection.csv
├─ phase8_internal_validation_summary.csv
├─ phase8_holdout_candidate_inputs.csv
├─ phase8_holdout_candidate_allocations.csv
├─ phase8_holdout_predictions.csv
├─ phase8_holdout_selection.csv
├─ phase8_holdout_selected_actual.csv      # 최종 실행 시
├─ phase8_holdout_comparison.csv           # 최종 실행 시
├─ phase8_metadata.json
└─ models/
   ├─ xgboost_mean_flow_time_seconds.json
   ├─ xgboost_congestion_conflicts.json
   ├─ xgboost_congestion_wait_seconds.json
   └─ xgboost_congestion_delay_ratio.json
```

---

## 13. 논문 해석 원칙

Phase 8의 기여는 “AI가 작업자를 직접 배치한다”가 아닙니다.

> **기존 Entropy 기반 최적화 모형의 고정 λ 한계를 XGBoost 기반 KPI 예측으로 보완하고, Fixed EWA의 예측 Flow를 효율성 guardrail로 유지하면서 날짜별 혼잡 최소 λ를 선택하는 hybrid AI-optimization 구조**입니다.

따라서 Phase 4~6에서 정립한 `D(n)`, `R(n)`, `J(n;λ)`의 이론적 의미와 DES 검증 구조는 유지됩니다.
