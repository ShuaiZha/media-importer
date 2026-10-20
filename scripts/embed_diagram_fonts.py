"""Embed a small, offline font subset in each documentation diagram.

Only needed after editing diagram labels; not a runtime dependency.
In a development environment with ``fonttools[woff]`` installed:
    python scripts/embed_diagram_fonts.py /path/to/LXGWWenKai-Regular.ttf

Font source: https://github.com/lxgw/LxgwWenKai
License: docs/assets/LXGW-WenKai-OFL.txt
"""
import argparse
import base64
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def embed(path, source):
    from fontTools import subset
    from fontTools.ttLib import TTFont

    content = path.read_text(encoding='utf-8')
    tree = ET.fromstring(content)
    labels = ''.join(''.join(node.itertext()) for node in tree.iter('{http://www.w3.org/2000/svg}text'))
    font = TTFont(source, recalcTimestamp=False)
    missing = set(map(ord, labels)) - set(font.getBestCmap())
    if missing:
        raise ValueError('Font is missing characters: ' + ''.join(map(chr, sorted(missing))))
    options = subset.Options()
    options.name_IDs = ['*']  # Preserve copyright and license records.
    options.name_legacy = True
    options.name_languages = ['*']
    tool = subset.Subsetter(options=options)
    tool.populate(text=labels)
    tool.subset(font)
    names = {1: 'Media Importer Sketch', 2: 'Regular', 3: 'MediaImporterSketch-Regular',
             4: 'Media Importer Sketch', 6: 'MediaImporterSketch-Regular',
             16: 'Media Importer Sketch', 17: 'Regular'}
    for entry in font['name'].names:
        if entry.nameID in names:
            entry.string = names[entry.nameID].encode(entry.getEncoding())
    font.flavor = 'woff2'
    output = io.BytesIO()
    font.save(output)
    font.close()
    payload = base64.b64encode(output.getvalue()).decode('ascii')
    css = ("@font-face { font-family: 'Media Importer Sketch'; "
           "src: url(data:font/woff2;base64," + payload + ") format('woff2'); }")
    content, count = re.subn(r'/\* embedded-font:start \*/.*?/\* embedded-font:end \*/',
        '/* embedded-font:start */\n' + css + '\n/* embedded-font:end */', content, flags=re.S)
    if count != 1:
        raise ValueError('Expected one font placeholder in ' + str(path))
    path.write_text(content, encoding='utf-8')
    print(f'{path.name}: {len(set(labels))} characters, {len(output.getvalue())} font bytes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('font', type=Path)
    args = parser.parse_args()
    for diagram in sorted((Path(__file__).resolve().parents[1] / 'docs/assets').glob('*.svg')):
        if not diagram.name.startswith('.'):
            embed(diagram, args.font)
