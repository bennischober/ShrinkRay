"""Self-contained HTML report generated from the sqlite database."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _fmt_gb(size: int | float | None) -> str:
    """Formats a byte count as a GB string."""
    if size is None or size <= 0:
        return "—"
    return f"{size / 1024**3:.2f} GB"


def _fmt_percent(value: float | None) -> str:
    """Formats a saved percentage."""
    if value is None:
        return "—"
    if value <= 0:
        return f"+{abs(value):.1f}%"
    return f"{value:.1f}%"


def _esc(text: object) -> str:
    """HTML-escapes a value."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_CSS = """
:root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3;
    --dim: #8b949e; --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --blue: #58a6ff;
}
* { box-sizing: border-box; }
body {
    margin: 0; padding: 2rem; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .2rem; }
.sub { color: var(--dim); margin-bottom: 1.5rem; }
.cards { display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
.card {
    flex: 1 1 160px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 1rem 1.25rem;
}
.card .num { font-size: 1.7rem; font-weight: 700; }
.card .lbl { color: var(--dim); font-size: .85rem; }
.card.green .num { color: var(--green); }
.card.blue .num { color: var(--blue); }
.card.red .num { color: var(--red); }
table {
    width: 100%; border-collapse: collapse; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
    font-size: .92rem;
}
th, td { padding: .55rem .8rem; text-align: left; border-bottom: 1px solid var(--border); }
th { background: #1c2129; color: var(--dim); font-weight: 600; white-space: nowrap; }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; white-space: nowrap; }
.pill {
    display: inline-block; padding: .1rem .55rem; border-radius: 999px;
    font-size: .8rem; font-weight: 600;
}
.pill.ok { background: rgba(63,185,80,.15); color: var(--green); }
.pill.skip { background: rgba(210,153,34,.15); color: var(--yellow); }
.pill.missing { background: rgba(248,81,73,.15); color: var(--red); }
.pill.err { background: rgba(248,81,73,.15); color: var(--red); }
.pill.enc { background: rgba(88,166,255,.15); color: var(--blue); }
.note { color: var(--dim); font-size: .85rem; }
footer { margin-top: 1.5rem; color: var(--dim); font-size: .8rem; }
"""


def _bars_note(scan_bars: int | None, plan_crop: str | None) -> str:
    """Explains the two bar measurements when they disagree (issue #131).

    The scan stores a 1/0 flag and the encode stores a crop rectangle, and they are
    deliberately separate measurements: the flag is a cheap pre-filter, the rectangle is the
    only value that reaches an encode. A file flagged "has bars" that then yields no crop is
    therefore not a contradiction - but it is invisible without this line, which is the whole
    reason the issue exists.

    Args:
        scan_bars (int | None): The scan's 1/0 flag from files.crop.
        plan_crop (str | None): The plan's crop rectangle, 'top,bottom,left,right'.

    Returns:
        str: A short explanation, or '' when there is nothing to reconcile.
    """
    if not scan_bars or plan_crop:
        return ""
    return (
        "bars flagged by scan, no crop in the plan "
        "(the flag is a cheap pre-filter; the encode measures its own rectangle)"
    )


def _summary_cards(rows: list[dict]) -> str:
    """Renders the summary cards from all latest-encode rows."""
    total = len(rows)
    encodes = [r for r in rows if r["decision"] == "encode"]
    skips = [r for r in rows if r["decision"] == "skip"]
    saved_bytes = sum((r["input_size"] or 0) - (r["output_size"] or 0) for r in encodes)
    total_input = sum(r["input_size"] or 0 for r in encodes)
    saved_percent = (saved_bytes / total_input * 100) if total_input else 0
    missing = [r for r in rows if r["decision"] == "missing"]
    missing_card = (
        f'<div class="card red"><div class="num">{len(missing)}</div>'
        '<div class="lbl">Missing on disk</div></div>'
        if missing
        else ""
    )
    return (
        '<div class="cards">'
        f'<div class="card"><div class="num">{total}</div><div class="lbl">Movies in database</div></div>'
        f'<div class="card blue"><div class="num">{len(encodes)}</div><div class="lbl">Encoded</div></div>'
        f'<div class="card"><div class="num">{len(skips)}</div><div class="lbl">Skipped</div></div>'
        f"{missing_card}"
        f'<div class="card green"><div class="num">{_fmt_gb(saved_bytes)}</div>'
        f'<div class="lbl">Disk space saved ({saved_percent:.1f}%)</div></div>'
        "</div>"
    )


def _movie_table(rows: list[dict]) -> str:
    """Renders the per-movie table."""
    head = (
        "<tr><th>Movie</th><th>Category</th><th>Resolution</th><th>Codec</th>"
        "<th>Duration</th><th class='num'>Size</th><th>Decision</th>"
        "<th class='num'>New size</th><th class='num'>Saved</th><th class='num'>CRF</th>"
        "<th class='num'>VMAF</th><th class='num'>Verified</th><th>Note</th></tr>"
    )
    body_rows = []
    for r in rows:
        decision = r["decision"]
        if decision == "missing":
            pill, cls = "missing", "missing"
        elif decision == "encode":
            pill, cls = "encoded", "ok"
        elif decision == "skip":
            pill, cls = "skipped", "skip"
        else:
            pill, cls = "not encoded", "enc"

        resolution = f"{r['width']}x{r['height']}" if r["width"] and r["height"] else "-"
        saved = _fmt_percent(r["saved_percent"]) if decision == "encode" else "—"
        vmaf = f"{r['achieved_vmaf']:.2f}" if r["achieved_vmaf"] is not None else "—"
        # full-file verification (B5): shown with the sub-sample divisor when
        # one was used (1/10 = every 10th frame measured)
        verified = r["verified_vmaf"]
        if verified is not None:
            sub = r["verified_subsample"]
            verified_cell = f"{verified:.2f}" + (
                f" <span class='note'>(1/{sub})</span>" if sub else ""
            )
        else:
            verified_cell = "—"
        note = r["note"] or ""
        body_rows.append(
            f"<tr>"
            f"<td><strong>{_esc(r['name'])}</strong>"
            f"<div class='note'>{_esc(r['path'])}</div></td>"
            f"<td>{_esc(r['category'])}</td>"
            f"<td>{resolution}</td>"
            f"<td>{_esc(r['codec'] or '—')}</td>"
            f"<td>{_esc(r['duration'] or '—')}</td>"
            f"<td class='num'>{_fmt_gb(r['input_size'])}</td>"
            f"<td><span class='pill {cls}'>{pill}</span></td>"
            f"<td class='num'>{_fmt_gb(r['output_size']) if decision == 'encode' else '—'}</td>"
            f"<td class='num'>{saved}</td>"
            f"<td class='num'>{r['chosen_crf'] if r['chosen_crf'] is not None else '—'}</td>"
            f"<td class='num'>{vmaf}</td>"
            f"<td class='num'>{verified_cell}</td>"
            f"<td class='note'>{_esc(note)}</td>"
            f"</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(body_rows)}</tbody></table>"


def _category_table(rows: list[dict]) -> str:
    """Renders the per-category summary table."""
    categories: dict[str, dict] = {}
    for r in rows:
        cat = r["category"] or "—"
        stats = categories.setdefault(
            cat, {"count": 0, "encoded": 0, "skipped": 0, "total": 0, "saved": 0}
        )
        stats["count"] += 1
        if r["decision"] == "encode":
            stats["encoded"] += 1
            stats["saved"] += (r["input_size"] or 0) - (r["output_size"] or 0)
        elif r["decision"] == "skip":
            stats["skipped"] += 1
        stats["total"] += r["input_size"] or 0

    head = (
        "<tr><th>Category</th><th class='num'>Movies</th><th class='num'>Encoded</th>"
        "<th class='num'>Skipped</th><th class='num'>Total size</th>"
        "<th class='num'>Saved</th><th class='num'>Saved %</th></tr>"
    )
    body_rows = []
    for cat, s in categories.items():
        saved_percent = (s["saved"] / s["total"] * 100) if s["total"] else None
        body_rows.append(
            f"<tr>"
            f"<td>{_esc(cat)}</td>"
            f"<td class='num'>{s['count']}</td>"
            f"<td class='num'>{s['encoded']}</td>"
            f"<td class='num'>{s['skipped']}</td>"
            f"<td class='num'>{_fmt_gb(s['total'])}</td>"
            f"<td class='num'>{_fmt_gb(s['saved'])}</td>"
            f"<td class='num'>{_fmt_percent(saved_percent)}</td>"
            f"</tr>"
        )
    return f"<table><thead>{head}</thead><tbody>{''.join(body_rows)}</tbody></table>"


def generate_report(db_path: Path, output: Path) -> None:
    """Generates a self-contained HTML report from the database.

    Args:
        db_path (Path): Path of the sqlite database.
        output (Path): Path of the HTML file to write.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row

    # latest decision per file (encode or skip), newest id wins
    rows = connection.execute(
        """
        SELECT f.path, f.name, f.category, f.duration, f.codec,
               f.width, f.height, f.missing_scans, f.crop AS scan_bars,
               p.crop AS plan_crop,
               e.input_size, e.output_size, e.saved_percent,
               e.chosen_crf, e.achieved_vmaf, e.preset, e.encoded_at,
               e.verified_vmaf, e.verified_subsample
        FROM files f
        LEFT JOIN plans p ON p.file_id = f.id
        LEFT JOIN encodes e ON e.id = (
            SELECT id FROM encodes WHERE file_id = f.id ORDER BY id DESC LIMIT 1
        )
        ORDER BY f.category, f.name
        """
    ).fetchall()
    connection.close()

    enriched = []
    for r in rows:
        preset = r["preset"] or ""
        if r["missing_scans"]:
            decision, note = "missing", "flagged missing on disk"
        elif preset.startswith("skipped"):
            decision, note = "skip", preset.removeprefix("skipped ").strip("()")
        elif preset.startswith("VMAF>"):
            decision, note = "encode", f"quality mode ({preset})"
        elif preset:
            decision, note = "encode", f"fixed preset ({preset})"
        else:
            decision, note = "none", ""

        bars_note = _bars_note(r["scan_bars"], r["plan_crop"])
        if bars_note:
            note = f"{note}; {bars_note}" if note else bars_note

        # for fixed-mode rows the saved_percent column already holds the value
        row_dict = dict(r)
        row_dict["decision"] = decision
        row_dict["note"] = note
        enriched.append(row_dict)

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ShrinkRay Report</title>"
        f'<style>{_CSS}</style></head><body><div class="wrap">'
        "<h1>ShrinkRay Report</h1>"
        f'<div class="sub">Generated {generated} from {_esc(db_path.name)}</div>'
        f"{_summary_cards(enriched)}"
        f"{_category_table(enriched)}"
        f"{_movie_table(enriched)}"
        f"<footer>ShrinkRay · quality-measured re-encoding · "
        f'<a style="color:var(--blue)" href="https://github.com/bennischober/ShrinkRay">GitHub</a></footer>'
        "</div></body></html>"
    )
    output.write_text(html, encoding="utf-8")
