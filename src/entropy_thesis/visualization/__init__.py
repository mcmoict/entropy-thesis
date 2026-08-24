"""Visualization helpers for the entropy thesis project."""

"""
python -m entropy_thesis.visualization.picking_animation --data-dir data/raw --layout-svg data/raw_original/Layout_Z1.0.svg --all-dates --output-html results/figures/picking_animation.html --serve

생성이 끝난 뒤 자동으로:
http://127.0.0.1:8000/picking_animation.html
형태로 로컬 웹서버를 실행하고 브라우저도 엽니다.

이미 데이터를 다 만들어놓은 뒤라면 직접:
cd results/figures
python -m http.server 8000
후 브라우저에서:
http://localhost:8000/picking_animation.html
을 열어도 됩니다.
"""