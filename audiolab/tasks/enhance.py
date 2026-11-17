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
from ..context import Progress, TaskContext
from ..errors import AudioLabError, Cancelled
from ..media import export
from ..media.verification import PEAK_TOLERANCE_DB, measure_export
from ..previews import make_previews
from ..processing import report_details
from ..recipes.base import ADVANCED_CHOICES, QUALITIES, automatic_compressor, eq_bands, section_eq
from ..recipes.chain import render
from ..settings import SETTINGS
from ..util import sanitize_filename
from . import common, preview

SAMPLE_RATES = (44100, 48000, 96000)
CHOICES = ADVANCED_CHOICES
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
            hints=['Enable AI features in Settings (one-time download), or use Tone and volume processing.'],
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


def _short(file: dict[str, Any]) -> str:
    """'MP3', 'AAC', 'FLAC' … for a sentence ("the MP3")."""
    spec = export.FORMATS.get(file.get('format') or '')
    return spec.short if spec else file['label']


def _check_message(
    file: dict[str, Any], check: dict[str, Any], trim_db: float, inherited_trim_db: float = 0.0
) -> str | None:
    """The warning for a saved file whose check failed or found its peaks over its limit (the peak ceiling, or
    for a lossy file the limit just under full scale: export.peak_limit), in the shape of the summary lines
    (a plain sentence; the figures in one trailing bracket)."""
    track = file.get('group') == 'tracks'
    name = f'{_short(file)} track “{file["name"]}”' if track else f'{_short(file)} file'
    if check['status'] == 'unverified':
        return f'The {name} was saved, but its loudness and peaks could not be verified.'
    if check['status'] != 'warning':
        return None
    spec = export.FORMATS.get(file.get('format') or '')
    lossy = bool(spec and spec.kind == 'lossy')
    limit, word, room = (
        ('the limit for lossy files', 'limit', 'a lower peak ceiling or a lossless format')
        if lossy
        else ('the peak ceiling', 'ceiling', 'a lower peak ceiling')
    )
    retried = trim_db > inherited_trim_db
    still = ', even after it was turned down' if retried else ''
    trimmed = f', turned down {trim_db:.1f} dB' if trim_db else ''
    reason = (
        f' The estimated turn-down needed would take it past {export.TRIM_MAX_DB:g} dB below the master.'
        if track and not retried
        else ''
    )
    return (
        f'The {name} goes over {limit}{still}.{reason} For more room, use {room} '
        f'({check["true_peak_dbtp"]:+.2f} dBTP after saving{trimmed}; {word} {check["ceiling_dbtp"]:+.2f} dBTP).'
    )


def _summary(kind, recipe, chosen_by_user, before, after, report, folder, files, trimmed=()) -> list[str]:
    """The "What we did" lines. Each is a plain sentence a novice understands, optionally followed by the
    technical detail in one trailing bracket (no brackets inside it): the page hides that bracket until
    "Show details". recipes.chain's steps follow the same shape."""
    lines = []
    if chosen_by_user:
        lines.append(f'Processed it as {recipe.name}, as you chose.')
    else:
        lines.append(
            f'Sounds like {kind["label"].lower()}, so it was processed as {recipe.name} '
            f'({kind.get("confidence_label", "medium")} confidence).'
        )
    volume_only = bool((report.get('params') or {}).get('volume_only'))
    limiter = report.get('limiter') or {}
    if volume_only:
        lines.append(
            'Changed only the volume, and kept a few peaks from distorting (a safety limiter).'
            if limiter.get('engaged')
            else 'Changed only the volume; the sound itself is untouched.'
        )
    b, a = before.get('integrated_lufs'), after.get('integrated_lufs')
    if b is not None and a is not None:
        delta = a - b
        if abs(delta) < 0.5:
            lines.append(f'Kept the overall volume about the same ({a:.1f} LUFS).')
        else:
            lines.append(f'Made it {"louder" if delta > 0 else "quieter"} ({delta:+.1f} dB: {b:.1f} → {a:.1f} LUFS).')
    tb, ta = before.get('true_peak_dbtp'), after.get('true_peak_dbtp')
    if tb is not None and ta is not None:
        if tb > -0.5 and ta < tb:
            lines.append(f'Brought down the highest peaks ({tb:+.1f} → {ta:+.1f} dBTP in the master).')
        elif volume_only and limiter.get('engaged'):
            lines.append(
                f'Turned down a few of the highest peaks (at most {limiter.get("max_gr_db", 0):.1f} dB '
                f'by the limiter; now {ta:+.1f} dBTP).'
            )
        else:
            lines.append(f'Checked the peaks ({ta:+.1f} dBTP in the master; each saved format has its own check).')
    # The loudness/peak lines above already cover the chain's level steps.
    lines += [s for s in (report.get('steps') or []) if 'LUFS' not in s and 'dBTP' not in s][:5]
    ai = report.get('ai')
    if ai and not ai.get('fallback') and not any('AI' in s for s in report.get('steps') or []):
        lines.append(
            f'Used AI to treat parts of the mix separately ({ai.get("model")} on {str(ai.get("device", "")).upper()}).'
        )
    for name, trim_db in trimmed:  # (format, dB) of lossy files re-encoded under their limit (media.export)
        lines.append(
            f'Turned the {name} down slightly so its peaks stay under the limit (-{trim_db:.1f} dB; encoding raises peaks).'
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
    analysis, x, sr = prof.analyze_file(
        ref, progress=p.sub(0, 70), include_overview=quality != 'best', **common.source_format(acq)
    )
    overview = analysis.pop('overview', None)
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
    # The analysis measured this very reference: the renderer need not measure the whole recording again.
    levels = {k: analysis.get(k) for k in ('integrated_lufs', 'true_peak_dbtp', 'lra')}
    measured = None if analysis.get('is_silent') or None in levels.values() else levels
    report = render(ref, master, params, analysis, common.scratch_dir(ctx), progress=p, input_metrics=measured)
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

    def check(path: Path, progress: Progress, limit: float, duration_s: float | None) -> dict:
        """The decoded export's loudness and peaks, measured before the file gets its name, against `limit`
        (export.peak_limit: the master's ceiling, or a lossy format's limit)."""
        try:
            return measure_export(path, ceiling_dbtp=limit, progress=progress, duration_s=duration_s)
        except Cancelled:
            raise
        except AudioLabError:
            return {
                'status': 'unverified',
                'integrated_lufs': None,
                'true_peak_dbtp': None,
                'ceiling_dbtp': limit,
                'ceiling_met': None,
                'tolerance_db': PEAK_TOLERANCE_DB,
                'message': None,
            }

    def note_check(file: dict[str, Any], trim_db: float, inherited_trim_db: float = 0.0) -> None:
        measurement = file['verification']
        measurement['message'] = _check_message(file, measurement, trim_db, inherited_trim_db)
        if measurement['message']:
            report.setdefault('warnings', []).append(measurement['message'])
            ctx.note('warning', measurement['message'])

    outputs = export.export(
        master,
        folder,
        f'{title} (Enhanced)',
        opts['formats'],
        tags=tags,
        cover=cover,
        progress=p.sub(0, 65 if split else 100),
        sample_rate=rate,
        check=check,
        ceiling_dbtp=params.tp_ceiling_db,
    )
    files = common.file_entries(outputs)
    trimmed = []
    for file, out in zip(files, outputs):
        note_check(file, out['trim_db'])
        if out['trim_db'] and file['verification']['status'] == 'verified':
            trimmed.append((_short(file), out['trim_db']))
    track_files, tracks_dir, tracks_lines = [], None, []
    if split:  # cut the finished master, in the first selected format, into "<folder>/Tracks"
        inherited_trim_db = outputs[0]['trim_db']
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
            trim_db=inherited_trim_db,
            check=check,
            ceiling_dbtp=params.tp_ceiling_db,
        )
        tracks_lines = [common.tracks_summary(acq, track_files, tracks_dir)]
        additional_trim = 0
        for file in track_files:
            if measurement := file.get('verification'):
                note_check(file, measurement['trim_db'], inherited_trim_db)
                additional_trim += measurement['trim_db'] > inherited_trim_db
        if additional_trim:
            noun = 'track was' if additional_trim == 1 else 'tracks were'
            tracks_lines.append(f'{additional_trim} {noun} turned down further during export.')
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
        'summary': _summary(kind, recipe, chosen_by_user, before, after, report, folder, files, trimmed) + tracks_lines,
        'classification': kind,
        'recipe': {'key': recipe.key, 'name': recipe.name},
        'decisions': decisions,
        'metrics': metrics,
        'processing': {
            **report_details(report, keep_calibration=quality != 'best'),
            **({'auto_compressor': automatic_compressor(recipe_key, analysis)} if quality == 'standard' else {}),
        },
        'analysis': analysis,
        'spectrogram': None,
        'warnings': list(report.get('warnings') or []),
        'markers': common.chapters(acq),
        **common.link_result(acq),
        '_media': media,
        **({'_overview': overview} if overview is not None else {}),
        '_library': {**common.library_hints(acq, keep_original=True), **({'version': version} if version else {})},
    }
