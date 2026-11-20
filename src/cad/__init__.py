"""3D ビューア（show_cad_viewer ツール）用の CAD 処理。

形（CadQuery コードの結果・STEP/IGES/BREP/DXF/STL ファイル）を、ブラウザの 3D ビューアが描ける
三角形・線・点と、要素ごとの幾何情報（LLM へ返す内容）を持つ JSON にする。
cadquery（OCP）は重いため、この処理はアプリ本体ではなくサブプロセス（run_cad.py）で動かす。
"""
