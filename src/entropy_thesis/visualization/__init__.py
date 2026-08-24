"""Visualization helpers for the entropy thesis project."""

"""
picking_animation.py 파일 기준 명령어 셋트

HTML 파일 & JSON 데이터 생성 & 서버 기동
python -m entropy_thesis.visualization.picking_animation --data-dir data/raw --layout-svg data/raw_original/Layout_Z1.0.svg --all-dates --output-html results/figures/picking_animation.html --serve

HTML 파일 & 서버 기동
python -m entropy_thesis.visualization.picking_animation --data-dir data/raw --layout-svg data/raw_original/Layout_Z1.0.svg --output-html results/figures/picking_animation.html --html-only --serve
python -m entropy_thesis.visualization.picking_animation --html-only --output-html results/figures/picking_animation.html --serve

HTML 파일만 생성
python -m entropy_thesis.visualization.picking_animation --data-dir data/raw --layout-svg data/raw_original/Layout_Z1.0.svg --output-html results/figures/picking_animation.html --html-only

생성이 끝난 뒤 자동으로:
http://127.0.0.1:8000/picking_animation.html
형태로 로컬 웹서버를 실행하고 브라우저도 엽니다.

이미 데이터를 다 만들어놓은 뒤라면 직접:
cd results/figures
python -m http.server 8000
후 브라우저에서:
http://localhost:8000/picking_animation.html
을 열어도 됩니다.




picking_animation_actual.py 파일 기준 명령어 셋트

이제 picking_animation_actual.py 기준으로 아래 3개 명령어 세트만 기억하시면 됩니다.

1. HTML 재생성 + JSON 생성 + 서버 기동

시뮬레이션을 전체 날짜에 대해 다시 실행하고, 월별 JSON과 HTML을 모두 새로 만든 뒤 서버까지 실행합니다.

python -m entropy_thesis.visualization.picking_animation_actual --all-dates --serve

생성 결과:

results\figures\picking_animation_actual.html
results\figures\picking_animation_actual_data\
    2023-01.json
    2023-02.json
    ...

실제 congestion_conflicts 이벤트를 JSON에 새로 넣어야 할 때는 이 명령을 사용하면 됩니다.

2. HTML만 재생성 + 서버 기동

기존 JSON은 그대로 유지하고 HTML만 다시 만든 뒤 바로 서버를 실행합니다.

python -m entropy_thesis.visualization.picking_animation_actual --html-only --serve

이 경우:

JSON : 기존 파일 그대로 유지
HTML : 재생성
서버 : 실행

HTML 디자인, 문구, JavaScript 표시 방식 등을 수정했을 때 가장 자주 사용할 명령입니다.

3. HTML만 재생성

기존 JSON은 그대로 유지하고 HTML만 다시 만듭니다. 서버는 실행하지 않습니다.

python -m entropy_thesis.visualization.picking_animation_actual --html-only

정리하면:

목적	명령
① HTML + JSON 새로 생성 + 서버	
python -m entropy_thesis.visualization.picking_animation_actual --all-dates --serve

② HTML만 재생성 + 서버	
python -m entropy_thesis.visualization.picking_animation_actual --html-only --serve

③ HTML만 재생성	
python -m entropy_thesis.visualization.picking_animation_actual --html-only

그리고 가장 중요한 구분은 이것입니다.

① --all-dates
   → 시뮬레이션 다시 실행
   → JSON 새로 생성
   → HTML 새로 생성

②, ③ --html-only
   → 시뮬레이션 실행 안 함
   → 기존 JSON 유지
   → HTML만 새로 생성

앞으로 Python에서 실제 충돌 데이터 생성 로직을 수정하면 ①, 화면 디자인이나 JavaScript만 수정하면 ② 또는 ③을 사용하시면 됩니다.

http://localhost:8000/picking_animation_actual.html

"""