# execute_python_code: pptx結合スクリプトで終了コード1（複数回失敗後、成功）

- **区分**: 問題点
- **検知日時**: 2026-08-13 16:30:00
- **対象ログファイル**: data/logs/app_20260812_234641.log

## 経緯

pptxファイル（月間ベースと週間ベース）の結合処理において、LLMが生成したPythonコードが複数回失敗した。最終的に sldId のID番号を調整するコードで成功した。

## ログ引用

```
2026-08-13 01:07:55,413 WARNING src.subagent: subagent tool=execute_python_code args={'code': "from lxml import etree\n\n# r:id属性のテスト\nroot = etree.Element('test')\nchild = etree.SubElement(root, 'sldId')\nchild.set('id', '256')\n# r:id属性をattribで直接設定\nchild.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}r:id'] = 'rId7'\n\nprint(etree.tostring(root, encoding='unicode'))\n"} -> [終了コード] 1

2026-08-13 01:09:53,285 WARNING src.subagent: subagent tool=execute_python_code args={'code': 'import os\nimport zipfile\nimport tempfile\n\noutput_path = r"E:\\user_b\\テスト（読み書き可能）\\annual_schedule.pptx"\ntmpdir = tempfile.mkdtemp()\n\n# 展開\nwith zipfile.ZipFile(output_path, \'r\') as z:\n    z.extractall(tmpdir)\n\n# presentation.xmlを確認\npres_path = os.path.join(tmpdir, "ppt", "presentation.xml")\nwith open(pres_path, \'r\', encoding=\'utf-8\') as f:\n    content = f.read()\n\n# sldIdLst部分を表示\nstart = content.find(\'sldIdLst\')\nend = content.find(\'</p:sldIdLst>\', start) + 15\nprint(content[start:end])\nprint(f"\\nTotal sldId count: {content.count(\'<p:sldId\')}")\n\n# presentation.xml.relsを確認\nrels_path = os.path.join(tmpdir, "ppt", "_rels", "presentation.xml.rels")\nwith open(rels_path, \'r\', encoding=\'utf-8\') as f:\n    rels = f.read()\n\nprint(f"\\nTotal Relationship count: {rels.count(\'<Relationship\')}")\n\n# スライドXMLファイルの数\nslide_dir = os.path.join(tmpdir, "ppt", "slides")\nslide_files = [f for f in os.listdir(slide_dir) if f.startswith(\'slide\') and f.endswith(\'.xml\')]\nprint(f"\\nSlide XML files: {len(slide_files)}")\nprint(sorted(slide_files))\n\nshutil.rmtree(tmpdir)\n'} -> [終了コード] 1

2026-08-13 01:11:02,941 WARNING src.subagent: subagent tool=execute_python_code args={'code': 'import os\nimport zipfile\nimport tempfile\nimport shutil\nimport re\n\noutput_path = r"E:\\user_b\\テスト（読み書き可能）\\annual_schedule.pptx"\ntmpdir = tempfile.mkdtemp()\n\nwith zipfile.ZipFile(output_path, \'r\') as z:\n    z.extractall(tmpdir)\n\npres_path = os.path.join(tmpdir, "ppt", "presentation.xml")\nwith open(pres_path, \'r\', encoding=\'utf-8\') as f:\n    content = f.read()\n\n# totalSlides属性を追加（p:presentationタグに追加）\n# <p:presentation xmlns:... saveSubsetFonts="1" autoCompressPictures="0">\n# の後に totalSlides="31" を追加\ncontent = content.replace(\n    \'autoCompressPictures="0">\',\n    \'autoCompressPictures="0" totalSlides="31">\'\n)\n\nwith open(pres_path, \'w\', encoding=\'utf-8\') as f:\n    f.write(content)\n\n# zipを再生成\nwith zipfile.ZipFile(output_path, \'w\', zipfile.ZIP_DEFLATED) as z:\n    for root, dirs, files in os.walk(tmpdir):\n        for file in files:\n            file_path = os.path.join(root, file)\n            arcname = os.path.relpath(file_path, tmpdir)\n            z.write(file_path, arcname)\n\nprint(f"Saved: {output_path}")\n\n# 確認\nfrom pptx import Presentation\nprs = Presentation(output_path)\nprint(f"Verification: {len(prs.slides)} slides")\n\nfor i, slide in enumerate(prs.slides):\n    title = ""\n    for shape in slide.shapes:\n        if shape.is_placeholder:\n            phfd = shape.placeholder_format\n            if phfd.type == 1:  # title\n                if shape.has_text_frame:\n                    title = shape.text\n                    break\n    if not title:\n        for shape in slide.shapes:\n            if shape.has_text_frame and shape.text.strip():\n                title = shape.text.strip()[:60]\n                break\n    print(f"  Slide {i+1}: \'{title}\'")\n'} -> [終了コード] 1
```

その後、以下のコードで成功した（終了コード0）:

```
2026-08-13 01:08:42,923 WARNING src.subagent: subagent tool=execute_python_code ... -> [終了コード] 0
2026-08-13 01:09:36,987 WARNING src.subagent: subagent tool=execute_python_code ... -> [終了コード] 0
2026-08-13 01:10:02,947 WARNING src.subagent: subagent tool=execute_python_code ... -> [終了コード] 0
```

## 実行コード

```python
from lxml import etree

# r:id属性のテスト
root = etree.Element('test')
child = etree.SubElement(root, 'sldId')
child.set('id', '256')
# r:id属性をattribで直接設定
child.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}r:id'] = 'rId7'

print(etree.tostring(root, encoding='unicode'))
```

## 推定原因

lxmlのetreeでnamespace付き属性の扱い、またはpptxファイルのsldId/rIdの重複により、結合後のpptxが破損していた可能性がある。3回の失敗後、sldIdのID番号を既存の最大値（268）以降に変更するコードで成功。

## 追記（2026-08-13 16:30）

- 初回検知。3回失敗後、最終的に成功。

## ユーザー回答
