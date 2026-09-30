"""Isolated outfall flap-gate parser correction for the pinned SWMM source.

The optional route-to subcatchment adds a token after the flap-gate value.
The reference reader checks the gate only when no route-to token is present.
This patch reads the gate whenever its token is present. It does not change
hydraulic equations, routing destinations, revision IDs or installed libraries.
Formal adoption still requires a new numerical identity and release checks.
"""
import argparse
from pathlib import Path


def patch_node(text):
    anchor = '    if ( ntoks == n )\n    {\n        m = findmatch(tok[n-1], NoYesWords);'
    if text.count(anchor) != 1:
        raise ValueError('Expected exactly one unmodified outfall gate reader')
    return text.replace(anchor, anchor.replace('ntoks == n', 'ntoks >= n'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    updated = patch_node(args.source.read_text(encoding='utf-8'))
    with args.destination.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(updated)
