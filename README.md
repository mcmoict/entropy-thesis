# Entropy Thesis

Shannon entropy로 물류센터 피킹 작업자의 공간적 집중과 혼잡을 측정하고,
네 가지 작업자 배치 방식을 이산 사건 시뮬레이션으로 비교하는 석사 논문 연구 프로젝트입니다.

## 비교 방법

- **Random Allocation**: seed가 고정된 균등 무작위 배치
- **Equal Allocation**: 구역별 균등 배치
- **Volume Proportional Allocation**: 구역별 물동량 비중에 따른 배치
- **Entropy-based Allocation**: 수요 적합도와 작업자 분포 엔트로피를 함께 고려한 배치

엔트로피 기반 방법은 연속 작업자 비율 `p`와 정규화된 수요 `d`에 대해 다음 목적을
최소화합니다.

```text
KL(p || d) - lambda * H(p)
p_i ∝ d_i ** (1 / (1 + lambda))
```

`entropy_weight`가 `0`이면 물동량 비례 방식과 같고, 값이 커질수록 양의 수요가 있는
구역 사이에서 균등한 분포에 가까워집니다. `minimum_per_zone`은 실제 하한으로 적용한
뒤 water-filling과 largest-remainder 방식으로 정수화합니다. 소수부 동률은 설정의 구역
순서로 결정되므로 결과가 재현 가능합니다.

## 환경 구성

Python 3.13이 필요합니다. 프로젝트 메타데이터와 런타임 의존성의 기준 파일은
`pyproject.toml`입니다.

### venv와 pip

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,notebook]"
```

Linux 또는 macOS:

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,notebook]"
```

런타임 패키지만 필요하면 `python -m pip install -r requirements.txt`를 사용합니다.
검증 환경의 직접 의존성 버전까지 맞추려면 다음 명령을 사용합니다.

```bash
python -m pip install -r requirements-lock.txt
```

### Conda

```bash
conda env create -f environment.yml
conda activate entropy-thesis
```

`environment-full.yml`은 검증한 직접 의존성 버전을 고정한 휴대 가능한 스냅숏입니다.
`requirements-pip.txt`는 `requirements-lock.txt`를 가리키는 호환 파일입니다. 두 파일
모두 로컬 절대 경로나 운영체제별 빌드 번호를 포함하지 않습니다. 운영체제별 전이
의존성은 설치 시 해당 패키지 인덱스에서 해석됩니다.

## 실험 실행

기본 설정으로 네 가지 배치 전략을 실행합니다.

```bash
entropy-thesis --config configs/baseline.yaml
# 또는
python -m entropy_thesis --config configs/baseline.yaml
```

같은 `experiment.seed`와 설정을 사용하면 동일한 난수 조건으로 실험을 재현할 수 있습니다.
결과는 기본적으로 `results/`에 저장됩니다.

- `experiment_runs.csv`: 전략·반복별 창고 전체 지표
- `experiment_zones.csv`: 전략·반복·구역별 지표
- `experiment_summary.csv`: 전략별 평균과 표본 표준편차
- `experiment_metadata.json`: 설정, 엔트로피 목적함수, 측정 모집단 정의

## 기본 설정

`configs/baseline.yaml`의 주요 항목은 다음과 같습니다.

- `experiment`: 난수 seed와 반복 횟수
- `warehouse`: 전체 작업자 수와 구역별 물량 비중·서비스율
- `simulation`: 실행 시간, 워밍업 시간, 전체 도착률
- `allocation`: 비교 전략과 엔트로피 배치 파라미터
- `output`: 결과 저장 디렉터리

`arrival_rate`는 시뮬레이션 시간 단위당 도착 작업 수이고, 각 구역의 `service_rate`는
작업자 한 명이 시간 단위당 처리하는 작업 수입니다. 모든 구역의 `volume_share` 합은
1이어야 하며 `warm_up`은 `duration`보다 작아야 합니다. 같은 구조의 JSON Schema는
`data/schema.json`에서 확인할 수 있습니다. 알 수 없는 설정 키는 오타로 간주해 실행 전에
거부하며, 개별 구역의 수요 비중과 전체 도착률은 0일 수 있습니다.

## 시뮬레이션과 측정 정의

각 구역은 독립적인 `M/M/c` 대기열입니다. 전체 작업은 Poisson 과정으로 도착하고
`volume_share`에 따라 구역으로 나뉘며, 서비스 시간은 구역별 `service_rate`를 모수로 한
지수분포를 따릅니다. 한 반복 안에서는 모든 전략이 동일한 도착·서비스 난수 흐름을
사용하는 common random numbers 방식으로 비교됩니다. 무작위 배치용 난수 흐름은
시뮬레이션 흐름과 분리됩니다.

관측 구간은 `[warm_up, duration)`입니다.

- `observation_arrivals`, `observation_completions`: 관측 구간의 도착·완료 사건 수
- `throughput`: 모든 관측 구간 완료 수를 관측 시간으로 나눈 값. warm-up backlog 완료도 포함
- `cohort_*`: 관측 구간에 도착한 작업 cohort 기준 지표
- `cohort_service_level`: cohort 중 종료 시각 전 완료된 비율
- `cohort_*_wait`, `cohort_*_system_time`: 종료 전 완료된 cohort만의 조건부 통계
- `wip_start`, `wip_end`: 관측 시작·종료 시 대기 또는 처리 중인 작업 수
- `queue_length_end`: 종료 시 처리 중인 작업을 제외한 대기 작업 수
- `utilization`: 관측 구간의 busy worker-time / available worker-time

완료되지 않은 cohort 작업은 우측 검열됩니다. 따라서 대기·체류시간 통계는 반드시
`cohort_service_level`과 함께 해석해야 합니다. 표본이 없는 서비스 수준과 도착·완료
엔트로피는 `NaN`으로 기록됩니다. 각 구역에는 다음 흐름 보존식이 적용됩니다.

```text
wip_end = wip_start + observation_arrivals - observation_completions
```

## 부하 시나리오

세 설정은 구역별 서비스율을 동일하게 두어 배치 효과와 생산성 차이를 분리합니다.

- `configs/low_load.yaml`: 낮은 부하, `arrival_rate=1.0`
- `configs/baseline.yaml`: near-capacity 기준선, `arrival_rate=2.7`
- `configs/overload.yaml`: 과부하, `arrival_rate=3.3`

```bash
entropy-thesis --config configs/low_load.yaml
entropy-thesis --config configs/baseline.yaml
entropy-thesis --config configs/overload.yaml
```

엔트로피 민감도 분석은 같은 시나리오에서 `entropy_weight`만 바꿔 별도 출력 디렉터리로
실행합니다. 설정과 seed가 같으면 결과가 동일합니다.

## 실제 데이터 Phase 1 / Phase 2 / Phase 3 / Phase 4

실제 CSV 기반 창고 graph 검증은 다음 명령으로 실행합니다.

```bash
python -m entropy_thesis.simulation.phase1 --data-dir data/raw
```

Phase 2의 실제 picking schedule 기반 이동거리·혼잡·공간 엔트로피 계산은 다음과 같습니다.

```bash
python -m entropy_thesis.simulation.phase2 --data-dir data/raw --date 2023-01-05
```

Phase 3의 실제 데이터 기반 Random / Equal / Volume Proportional 작업자 배치 비교는 다음과 같습니다.

```bash
python -m entropy_thesis.simulation.phase3 --data-dir data/raw --date 2023-01-05
```

Phase 4의 실제 데이터 기반 Entropy-based Allocation λ 탐색은 다음과 같습니다.

```bash
python -m entropy_thesis.simulation.phase4 --data-dir data/raw --date 2023-01-05
```

기본 λ 후보는 `0, 0.25, 0.5, 1, 2, 4, 8`이며, 동일한 정수 작업자 배치가 반복되면 DES는 한 번만 실행합니다. 기본 λ 선택 KPI는 `mean_flow_time_seconds`입니다.

세부 모델링 정의와 출력 파일은 `README_PHASE1.md`, `README_PHASE2.md`, `README_PHASE3.md`, `README_PHASE4.md`를 참고합니다.

## 모델 범위와 한계

현재 구현은 논문 실험의 최소 기준 모델입니다. Shannon 엔트로피는 **구역 간 작업자
배치의 공간적 집중도**를 측정하고, 대기열 지표는 그 배치가 처리 혼잡에 미치는 영향을
측정합니다. 실제 좌표, 이동 거리, 통로 용량, 작업자 간 물리적 간섭, 밀도에 따른
생산성 저하는 아직 모델링하지 않습니다. 따라서 현재 결과를 실제 통로 충돌이나 안전상
혼잡의 직접 추정치로 해석해서는 안 됩니다. 해당 결론이 필요하면 aisle resource,
이동 네트워크와 밀도별 서비스율 저하를 후속 모델에 추가해야 합니다.

(2026-08-15 추가)
프로젝트에는 초기의 단순 구역별 `M/M/c` 기준 모델과 Phase 1~3의 실제 데이터 모델이 함께 존재합니다. 실제 데이터 모델은 Storage/Support 좌표로 만든 이동 graph, edge/pick-node capacity, 이동거리, resource contention 대기 및 작업자 공간 엔트로피를 계산합니다.

다만 Phase 2~3의 `congestion conflict`는 **실제 사람끼리 물리적으로 충돌한 횟수**가 아니라 capacity-limited edge 또는 pick node에 즉시 진입하지 못해 발생한 simulated waiting event입니다. 또한 작업자 간 회피행동, 통로 폭에 따른 연속 밀도 효과, 보행속도 저하, 안전거리, order consolidation 등은 아직 모델링하지 않습니다. 따라서 결과는 배치 전략 간 상대 비교용으로 해석하고, 실제 안전 충돌 건수의 직접 추정치로 사용하지 않습니다.

## 테스트

```bash
pytest
```

GitHub Actions는 Linux와 Windows의 Python 3.13에서 테스트, 기본 CLI 실행, wheel 빌드를
검증합니다.

## 디렉터리 구조

```text
configs/             실험 설정
data/raw/            원본 데이터
data/processed/      전처리 데이터
notebooks/           탐색 및 환경 확인 노트북
results/             생성된 실험 결과
results/figures/     생성된 시각화
src/entropy_thesis/  핵심 계산·배치·시뮬레이션 코드
tests/               단위 및 통합 테스트
```

`results/figures/`는 후속 분석 그래프를 위한 예약 경로이며 현재 CLI는 표·메타데이터를
생성합니다. 원본·전처리 데이터와 생성 결과는 `.gitignore`로 제외하고 디렉터리 골격만
`.gitkeep`으로 유지합니다.
