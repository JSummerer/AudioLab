"""Enhance (the "upscaler"): figure out what the audio is, pick a matching
recipe, master it, and export hi-res files plus before/after excerpts for comparing.

Every automatic choice is journaled with its reason so the result screen can
say *what* was done and *why* in plain words.
"""

import math
from pathlib import Path
from typing import Any

from .. import recipes
from ..analysis import classify
from ..analysis import profile as prof
from ..context import TaskContext
from ..errors import AudioLabError, Cancelled
from ..media import export
from ..media.verification import PEAK_TOLERANCE_DB, measure_export
from ..previews import make_previews
from ..processing import report_details
from ..recipes.base import eq_bands, section_eq
from ..recipes.chain import render
from ..settings import SETTINGS
from ..util import sanitize_filename
from . import common, preview

QUALITIES = ('light', 'standard', 'best')  # light = volume only (recipes.base)
SAMPLE_RATES = (44100, 48000, 96000)
CHOICES = {
    'glue': ('on', 'off'),
    'hf_restore': ('on', 'off'),
    'mono_bass': ('on', 'off'),
    'denoise': ('off', 'light', 'strong'),
    'deess': ('auto', 'off', 'strong'),
    'adaptive_lowmid': ('auto', 'off', 'strong'),
    'harmonics': ('on', 'off'),
}
RANGES = {'target_lufs': (-30.0, -5.0), 'tp_ceiling_db': (-3.0, -0.1), 'bass_db': (0.0, 6.0)}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an integer beyond any float
        return None
    return f if math.isfinite(f) else None


def validate(options: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    recipe = str(options.get('recipe') or 'auto').lower()
    if recipe != 'auto' and recipe not in recipes.RECIPES:
        raise AudioLabError(f'Unknown type "{recipe}".')
    quality = str(options.get('quality') or 'standard').lower()
    if quality not in QUALITIES:
        quality = 'standard'
    if quality == 'best' and not common.ai_device():
        raise AudioLabError(
            'AI stems processing needs the AI add-on, which is not installed yet.',
            hints=['Enable AI features in Settings (one-time download), or use Adaptive processing.'],
            fix='install_ai',
        )
    formats = options.get('formats') or SETTINGS.get().enhance_formats
    if isinstance(formats, str):
        formats = [formats]
    formats = (
        [f for f in formats if isinstance(f, str) and f in export.FORMATS] if isinstance(formats, (list, tuple)) else []
    )
    if not formats:
        raise AudioLabError('Pick at least one format to save.')

    adv_in = options.get('advanced') if isinstance(options.get('advanced'), dict) else {}
    adv: dict[str, Any] = {}
    for key, (lo, hi) in RANGES.items():
        v = _num(adv_in.get(key))
        if v is not None:
            adv[key] = round(min(hi, max(lo, v)), 2)
    for key, allowed in CHOICES.items():
        v = str(adv_in.get(key) or '').lower()
        if v in allowed:
            adv[key] = v
    model = adv_in.get('model')
    if isinstance(model, str) and model != 'auto':
        from ..ai import MODELS

        if model in MODELS:
            adv['model'] = model
    sr = _num(adv_in.get('sample_rate'))
    if sr is not None and int(sr) in SAMPLE_RATES:
        adv['sample_rate'] = int(sr)
    eq = eq_bands(adv_in.get('eq'))
    if eq is not None:
        adv['eq'] = eq
    sections = section_eq(adv_in.get('section_eq'))
    if sections is not None:
        adv['section_eq'] = sections
    tracklist = common.clean_tracklist(options.get('tracklist'), source)
    return {
        'recipe': recipe,
        'quality': quality,
        'formats': list(dict.fromkeys(formats)),
        'advanced': adv,
        'split_tracks': bool(options.get('split_tracks')),
        'embed_cover': bool(options.get('embed_cover', SETTINGS.get().embed_cover)),
        'save_cover': bool(options.get('save_cover')),
        **({'tracklist': tracklist} if tracklist else {}),
    }


def stages(options: dict[str, Any], source: dict[str, Any]):
    if options.get('_preview'):
        return preview.stages()
    quality = options.get('quality')
    return [
        ('acquire', 'Getting audio', 20 if source.get('kind') == 'url' else 4),
        ('decode', 'Preparing', 6),
        ('analyze', 'Listening', 6),
        (
            'process',
            'Adjusting the volume' if quality == 'light' else 'Enhancing',
            {'best': 75, 'light': 10}.get(quality, 40),
        ),
        ('preview', 'Making a preview', 4),
        ('export', 'Saving files', 14),
    ]


def _summary(kind, recipe, chosen_by_user, before, after, report, folder, files) -> list[str]:
    lines = []
    if chosen_by_user:
        lines.append(f'Processed as {recipe.name} (your choice).')
    else:
        lines.append(
            f'Detected {kind["label"].lower()} ({kind.get("confidence_label", "medium")} confidence) '
            f'— used the {recipe.name} recipe.'
        )
    volume_only = bool((report.get('params') or {}).get('volume_only'))
    limiter = report.get('limiter') or {}
    if volume_only:
        lines.append(
            'Volume only, plus a safety limiter on a few peaks.'
            if limiter.get('engaged')
            else 'Volume only — the sound itself was not changed.'
        )
    b, a = before.get('integrated_lufs'), after.get('integrated_lufs')
    if b is not None and a is not None:
        delta = a - b
        if abs(delta) < 0.5:
            lines.append(f'Kept the overall volume about the same ({a:.1f} LUFS).')
        else:
            lines.append(
                f'Made it {abs(delta):.1f} dB {"louder" if delta > 0 else "quieter"} ({b:.1f} → {a:.1f} LUFS).'
            )
    tb, ta = before.get('true_peak_dbtp'), after.get('true_peak_dbtp')
    if tb is not None and ta is not None:
        if tb > -0.5 and ta < tb:
            lines.append(f"Reduced the master's high peaks ({tb:+.1f} → {ta:+.1f} dBTP).")
        elif volume_only and limiter.get('engaged'):
            lines.append(
                f'The limiter took at most {limiter.get("max_gr_db", 0):.1f} dB off the highest peaks '
                f'(now {ta:+.1f} dBTP).'
            )
        else:
            lines.append(f'Master peaks measured {ta:+.1f} dBTP; saved formats have their own checks.')
    # The loudness/peak lines above already cover the chain's level steps.
    lines += [s for s in (report.get('steps') or []) if 'LUFS' not in s and 'dBTP' not in s][:5]
    ai = report.get('ai')
    if ai and not ai.get('fallback') and not any('AI' in s for s in report.get('steps') or []):
        lines.append(
            f'Used AI ({ai.get("model")} on {str(ai.get("device", "")).upper()}) to treat parts of the mix separately.'
        )
    lines.append(f'Saved {len(files)} file{"s" if len(files) != 1 else ""} to {folder}.')
    return lines


def run(ctx: TaskContext, source: dict[str, Any]) -> dict[str, Any]:
    opts = ctx.options
    if opts.get('_preview'):
        return preview.run(ctx, source)
    quality = opts['quality']
    acq = common.acquire(ctx, source, ctx.stage('acquire'), task='enhance', quality=quality)
    if not opts.get('tracklist') and source.get('_markers'):
        acq.metadata.update(tracks=source['_markers'], tracks_source=source.get('_markers_source'))
    common.decode(ctx, acq, ctx.stage('decode'))
    ref = common.reference_path(ctx)

    # --- listen: measure + classify ------------------------------------------
    p = ctx.stage('analyze', 'Listening to the recording…')
    analysis, x, sr = prof.analyze_file(ref, progress=p.sub(0, 70), **common.source_format(acq))
    features = classify.audio_features(x, sr, check_cancel=p.check_cancel, known_lra=analysis.get('lra'))
    kind = classify.classify(features, acq.duration_s or 0, acq.metadata)
    del x
    analysis['bpm'] = kind.get('bpm')
    common.describe_source(analysis, acq)
    ctx.note('decision', f'Sounds like: {kind["label"]} ({kind.get("confidence_label")} confidence)', kind)

    chosen_by_user = opts['recipe'] != 'auto'
    recipe_key = opts['recipe'] if chosen_by_user else recipes.recipe_for_type(kind['type'])
    recipe = recipes.RECIPES[recipe_key]
    params, decisions = recipes.resolve(recipe_key, analysis, quality, opts['advanced'])
    decisions.insert(
        0,
        {
            'key': 'recipe',
            'label': 'Recipe',
            'value': recipe.name,
            'source': 'user' if chosen_by_user else 'auto',
            'why': 'chosen by you'
            if chosen_by_user
            else f'the audio sounds like {kind["label"].lower()}: ' + '; '.join(kind.get('reasons', [])[:3]),
        },
    )
    for d in decisions:
        ctx.note('decision', f'{d["label"]}: {d["value"]} — {d.get("why", "")}', d)
    p(100)

    # --- master ----------------------------------------------------------------
    p = ctx.stage('process', f'Enhancing with the {recipe.name} recipe…')
    master = common.scratch_dir(ctx) / 'master.flac'
    report = render(ref, master, params, analysis, common.scratch_dir(ctx), progress=p)
    for w in report.get('warnings') or []:
        ctx.note('warning', w)
    ctx.note(
        'verdict',
        f'Master: {report.get("integrated_lufs")} LUFS, {report.get("true_peak_dbtp")} dBTP, '
        f'LRA {report.get("lra")} LU',
        {k: v for k, v in report.items() if k != 'steps'},
    )

    # --- preview ---------------------------------------------------------------
    # Excerpts at their true levels, for comparing when the original isn't kept (the player matches the volume).
    p = ctx.stage('preview', 'Making a before/after excerpt…')
    media: dict[str, str] = {}
    previews = []
    excerpt = None
    try:
        pv = make_previews(ref, master, common.previews_dir(ctx), check_cancel=p.check_cancel)
        for key, label in (('original', 'Original'), ('enhanced', 'Enhanced')):
            media[key] = str(pv[key])
            previews.append({'key': key, 'label': label})
        excerpt = {'start_s': pv['start_s'], 'duration_s': pv['duration_s']}
    except Cancelled:
        raise
    except Exception as e:  # preview is a convenience — never fail the job for it
        ctx.note('warning', f'Preview skipped: {e}')

    # --- export ----------------------------------------------------------------
    p = ctx.stage('export', 'Saving your files…')
    title = sanitize_filename(acq.title)
    folder = common.output_folder(f'{title} (Enhanced)', parent=common.output_root(acq))
    # Tags say which version this is ("(Enhanced 2)") and how it was made; file names stay as they are.
    version = common.next_version(source, 'enhance')
    tags = common.tags_for(
        acq, f' {common.version_label("Enhanced", version)}', common.made_by(recipe.name, params.target_lufs)
    )
    cover = common.cover_art(ctx, acq)
    rate = params.sample_rate if params.sample_rate != 48000 else None
    split = bool(opts.get('split_tracks'))
    outputs = export.export(
        master,
        folder,
        f'{title} (Enhanced)',
        opts['formats'],
        tags=tags,
        cover=cover,
        progress=p.sub(0, 45 if split else 70),
        sample_rate=rate,
    )
    files = common.file_entries(outputs)
    verification_end = 65 if split else 100
    verification_start = 45 if split else 70
    for i, file in enumerate(files):
        start = verification_start + (verification_end - verification_start) * i / len(files)
        stop = verification_start + (verification_end - verification_start) * (i + 1) / len(files)
        vp = p.sub(start, stop)
        vp(0, f'Checking the saved {file["label"]}…')
        try:
            measurement = measure_export(
                Path(file['path']),
                ceiling_dbtp=params.tp_ceiling_db,
                progress=vp,
                duration_s=report.get('duration_s'),
            )
        except Cancelled:
            raise
        except AudioLabError:
            measurement = {
                'status': 'unverified',
                'integrated_lufs': None,
                'true_peak_dbtp': None,
                'ceiling_dbtp': params.tp_ceiling_db,
                'ceiling_met': None,
                'tolerance_db': PEAK_TOLERANCE_DB,
                'message': f'{file["label"]} was saved, but its loudness and peaks could not be verified.',
            }
        file['verification'] = measurement
        if measurement.get('message'):
            warning = measurement['message']
            if measurement['status'] == 'warning':
                warning += ' Try a lower peak ceiling or a lossless format if you need more headroom.'
            report.setdefault('warnings', []).append(warning)
            ctx.note('warning', warning)
        vp(100)
    track_files, tracks_dir, tracks_lines = [], None, []
    if split:  # cut the finished master, in the first selected format, into "<folder>/Tracks"
        track_files, tracks_dir = common.split_into_tracks(
            acq,
            master,
            folder,
            'Tracks',
            opts['formats'][0],
            tags,
            cover,
            p.sub(65, 100),
            start=len(files),
            sample_rate=rate,
        )
        tracks_lines = [common.tracks_summary(acq, track_files, tracks_dir)]
    track_files += common.cover_file(ctx, source, acq, folder, start=len(files) + len(track_files))

    before = report.get('input') or {k: analysis.get(k) for k in ('integrated_lufs', 'true_peak_dbtp', 'lra')}
    after = {k: report.get(k) for k in ('integrated_lufs', 'true_peak_dbtp', 'lra')}
    metrics = {
        'before': {
            'lufs': before.get('integrated_lufs'),
            'true_peak_dbtp': before.get('true_peak_dbtp'),
            'lra': before.get('lra'),
        },
        'after': {
            'lufs': after.get('integrated_lufs'),
            'true_peak_dbtp': after.get('true_peak_dbtp'),
            'lra': after.get('lra'),
        },
    }
    return {
        'headline': ('Volume evened out' if opts.get('quality') == 'light' else f'Enhanced as {recipe.name}'),
        'output_dir': str(folder),
        'files': files + track_files,
        'cover_source': common.cover_source(source),
        **({'tracks_dir': str(tracks_dir)} if tracks_dir else {}),
        'previews': previews,
        **({'excerpt': excerpt} if excerpt else {}),
        'summary': _summary(kind, recipe, chosen_by_user, before, after, report, folder, files) + tracks_lines,
        'classification': kind,
        'recipe': {'key': recipe.key, 'name': recipe.name},
        'decisions': decisions,
        'metrics': metrics,
        'processing': report_details(report),
        'analysis': analysis,
        'spectrogram': None,
        'warnings': list(report.get('warnings') or []),
        'markers': common.chapters(acq),
        **common.link_result(acq),
        '_media': media,
        '_library': {**common.library_hints(acq, keep_original=True), **({'version': version} if version else {})},
    }
