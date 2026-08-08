# 프로젝트 개요

이 프로젝트는 석사 논문 연구용 Python 프로젝트이다.

연구 주제:
엔트로피 기반 물류센터 피킹 작업자 배치 최적화

## 연구 목표

작업자의 공간적 집중과 혼잡을 Shannon 엔트로피로 측정하고
기존 작업자 배치 방식과 엔트로피 기반 배치 방식을 비교한다.

## 시뮬레이션

이산 사건 시뮬레이션(Discrete Event Simulation) 기반으로 구현한다.

Python 3.13을 사용한다.

주요 라이브러리:

- NumPy
- Pandas
- SciPy
- SimPy
- Matplotlib

## 비교 기준 기법

1. Random Allocation
2. Equal Allocation
3. Volume Proportional Allocation
4. Entropy-based Allocation

## 코딩 규칙

- 실험 재현성을 위해 random seed를 고정할 수 있어야 한다.
- 모든 실험 파라미터는 config 파일로 분리한다.
- 결과 데이터는 results 디렉터리에 저장한다.
- 핵심 계산 로직은 src에 작성한다.
- 테스트 코드는 tests에 작성한다.
