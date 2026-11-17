"""The small editor document saved with an Enhance version. Incomplete input is plain text."""

import hashlib
import json
import re

from .recipes.base import ADVANCED_CHOICES, EQ_LIMITS, EQ_MAX_BANDS, SECTION_MAX

_NUMBERS = {'target_lufs', 'tp_ceiling_db', 'bass_db'}
_CHOICES = {key: ADVANCED_CHOICES[key] for key in ('adaptive_lowmid', 'deess', 'harmonics')}
_CHOICES['glue'] = ('auto', *ADVANCED_CHOICES['glue'])


def _text(s, limit=128):
    return isinstance(s, str) and len(s) <= limit


def _id(s):
    return isinstance(s, str) and re.fullmatch(r'0|[1-9][0-9]{0,8}', s) is not None


def _bands(rows):
    # A neutral (0 dB) band is editable even though the DSP omits it.
    return (
        isinstance(rows, list)
        and len(rows) <= EQ_MAX_BANDS
        and all(
            isinstance(b, dict)
            and b.keys() == EQ_LIMITS.keys()
            and all(type(b[k]) in (int, float) and lo <= b[k] <= hi for k, (lo, hi) in EQ_LIMITS.items())
            for b in rows
        )
    )


def _graph(g):
    if not isinstance(g, dict) or g.keys() != {'bands', 'bypassed', 'text', 'textOpen', 'numbers', 'invalid'}:
        return False
    rows = g['numbers']
    return (
        _bands(g['bands'])
        and _text(g['text'], 4096)
        and all(type(g[k]) is bool for k in ('bypassed', 'textOpen', 'invalid'))
        and (
            rows is None
            or (
                isinstance(rows, list)
                and len(rows) == len(g['bands'])
                and all(
                    isinstance(row, dict) and row.keys() == {'hz', 'db', 'q'} and all(_text(s) for s in row.values())
                    for row in rows
                )
            )
        )
    )


def valid(data):
    if not isinstance(data, dict) or data.keys() != {'from', 'length', 'scope', 'sections', 'values', 'graphs'}:
        return False
    values, sections, graphs = data['values'], data['sections'], data['graphs']
    if not isinstance(values, dict) or values.keys() - (_NUMBERS | _CHOICES.keys()):
        return False
    for k, v in values.items():
        if k in _NUMBERS:
            if v is not None and not _text(v):
                return False
        elif v not in _CHOICES[k]:
            return False
    if not isinstance(sections, list) or len(sections) > SECTION_MAX:
        return False
    ids = {'global'}
    for row in sections:
        if not isinstance(row, dict) or row.keys() != {'id', 'from', 'to', 'fade'}:
            return False
        if not _id(row['id']) or row['id'] in ids or not all(_text(row[k]) for k in ('from', 'to', 'fade')):
            return False
        ids.add(row['id'])
    return (
        _text(data['from'])
        and _text(data['length'])
        and isinstance(data['scope'], str)
        and data['scope'] in ids
        and isinstance(graphs, dict)
        and graphs.keys() <= ids
        and all(_graph(g) for g in graphs.values())
    )


def read(entry):
    """A changed original or processing record invalidates any previous draft, including its revision."""
    source = entry.get('original') or {}
    basis = [1, entry['id'], source.get('sha1'), source.get('file'), entry.get('duration_s'), entry.get('processing')]
    key = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    record = entry.get('adjust_draft')
    if (
        isinstance(record, dict)
        and record.keys() == {'basis', 'revision', 'data'}
        and record['basis'] == key
        and type(record['revision']) is int
        and record['revision'] >= 0
    ):
        if record.get('data') is None or valid(record['data']):
            return record
    return {'basis': key, 'revision': 0, 'data': None}
