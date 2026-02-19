"""
Patch existing NaN per-house reports: fix Efficiency > 100% and formula that doesn't sum to 100%.
Also renames titles to indicate these are NaN-filled reports.

Root cause: NaN minutes were counted in background calculation, inflating background_power
for dead phases, causing efficiency > 100% and percentages != 100%.

This script recalculates: Unmatched = 100 - Explained - Background, caps efficiency.

Usage (on the server):
    python patch_efficiency_cap.py /home/hilakese/nilm-experiment-results/house_reports_nan/
    python patch_efficiency_cap.py --dry-run /path/to/reports/
"""
import re
import sys
import glob
import os


def _parse_float(text: str) -> float:
    """Extract float from text like '43.2%' or '43.2'."""
    m = re.search(r'([\d.]+)', text)
    return float(m.group(1)) if m else 0.0


def patch_report(html: str) -> str:
    """Patch a single dynamic_report HTML: fix formula bar and efficiency hero card."""

    # ── 1. Fix the formula bar ──
    # Pattern: Explained (X%) + Background (Y%) + Unmatched (Z%) = 100%
    formula_pattern = (
        r'(<span style="color:[^"]*;">Explained \()([\d.]+)(%\)</span> \+\s*'
        r'<span style="color:[^"]*;">Background \()([\d.]+)(%\)</span> \+\s*'
        r'<span style="color:[^"]*;">Unmatched \()([\d.]+)(%\)</span>\s*= 100%)'
    )

    formula_match = re.search(formula_pattern, html)
    if not formula_match:
        return html

    explained_pct = float(formula_match.group(2))
    background_pct = float(formula_match.group(4))
    old_unmatched_pct = float(formula_match.group(6))

    # Check if percentages already sum to ~100%
    total = explained_pct + background_pct + old_unmatched_pct
    if abs(total - 100.0) < 0.5:
        return html  # Already correct

    # Recalculate unmatched
    new_unmatched_pct = round(100.0 - explained_pct - background_pct, 1)
    if new_unmatched_pct < 0:
        new_unmatched_pct = 0.0

    # Fix formula bar — replace old unmatched value
    html = html.replace(
        f'Unmatched ({old_unmatched_pct:.1f}%)',
        f'Unmatched ({new_unmatched_pct:.1f}%)',
    )

    # ── 2. Fix the efficiency formula in the same bar ──
    # Pattern: Efficiency = X% / Y% = <strong>Z%</strong>
    eff_formula_pattern = (
        r'<strong>Efficiency</strong> = ([\d.]+)% / ([\d.]+)% = '
        r'<strong style="color:[^"]*;">([\d.]+)%</strong>'
    )
    eff_match = re.search(eff_formula_pattern, html)
    if eff_match:
        old_eff_in_formula = float(eff_match.group(3))
        targetable_pct = float(eff_match.group(2))
        if targetable_pct > 0:
            new_eff = round(explained_pct / targetable_pct * 100, 1)
            new_eff = min(new_eff, 100.0)
        else:
            new_eff = 0.0

        # Replace the efficiency value in the formula
        old_eff_str = f'{old_eff_in_formula:.1f}%</strong>'
        new_eff_str = f'{new_eff:.1f}%</strong>'
        # Only replace in the formula context
        html = html.replace(
            eff_match.group(0),
            eff_match.group(0).replace(old_eff_str, new_eff_str),
        )

    # ── 3. Fix the hero card ──
    # Pattern: <div style="font-size: 2.8em; ...">X%</div> followed by "Detection Efficiency"
    hero_pattern = (
        r'(<div style="font-size: 2\.8em; font-weight: bold; color: [^"]*;">)'
        r'([\d.]+)(%</div>\s*<div[^>]*>Detection Efficiency</div>)'
    )
    hero_match = re.search(hero_pattern, html)
    if hero_match:
        old_hero_eff = float(hero_match.group(2))
        if old_hero_eff > 100.0:
            new_hero_eff = min(old_hero_eff, 100.0)
            html = html.replace(
                hero_match.group(0),
                f'{hero_match.group(1)}{new_hero_eff:.1f}{hero_match.group(3)}',
            )

    # ── 4. Fix the explanation text under hero card ──
    # Pattern: "Of non-background power (X%), <strong>Y%</strong> matched to devices."
    expl_pattern = (
        r'(Of non-background power \([\d.]+%\), <strong>)([\d.]+)'
        r'(%</strong> matched to devices\.)'
    )
    expl_match = re.search(expl_pattern, html)
    if expl_match:
        old_expl_eff = float(expl_match.group(2))
        if old_expl_eff > 100.0:
            html = html.replace(
                expl_match.group(0),
                f'{expl_match.group(1)}{min(old_expl_eff, 100.0):.1f}{expl_match.group(3)}',
            )

    # ── 5. Fix per-phase donut charts (efficiency > 100 breaks the donut) ──
    # Pattern: 'values': [EFF, REMAINING] in donut chart data
    # These are in JSON inside <script> blocks for the efficiency gauge
    def fix_donut(match):
        eff_val = float(match.group(1))
        if eff_val > 100.0:
            return f'"values": [100.0, 0]'
        return match.group(0)

    html = re.sub(
        r'"values": \[([\d.]+), [\d.]+\]',
        fix_donut,
        html,
    )

    # Fix per-phase donut annotation text (e.g., "111%" → "100%")
    def fix_donut_annotation(match):
        prefix = match.group(1)
        eff_val = float(match.group(2))
        suffix = match.group(3)
        if eff_val > 100.0:
            return f'{prefix}100{suffix}'
        return match.group(0)

    html = re.sub(
        r'("text": ")([\d]+)(%")',
        fix_donut_annotation,
        html,
    )

    return html


NAN_TITLE_PREFIX = 'Dynamic Threshold NaN Filled'


def rename_titles(html: str, filename: str) -> str:
    """Rename <title> and <h1> to indicate this is a NaN-filled report."""
    house_match = re.search(r'dynamic_report_(\d+)', filename)

    if house_match:
        house_id = house_match.group(1)
        # Per-house report
        html = html.replace(
            f'<title>Dynamic Threshold Report - House {house_id}</title>',
            f'<title>{NAN_TITLE_PREFIX} - House {house_id}</title>',
        )
        html = html.replace(
            f'<h1>Dynamic Threshold Analysis - House {house_id}</h1>',
            f'<h1>{NAN_TITLE_PREFIX} - House {house_id}</h1>',
        )
    else:
        # Aggregate report
        html = html.replace(
            '<title>Dynamic Threshold - Aggregate Report</title>',
            f'<title>{NAN_TITLE_PREFIX} - Aggregate Report</title>',
        )
        html = html.replace(
            '<h1>Dynamic Threshold - Aggregate Report</h1>',
            f'<h1>{NAN_TITLE_PREFIX} - Aggregate Report</h1>',
        )

    return html


def process_directory(directory: str, dry_run: bool = False) -> int:
    """Process all dynamic_report_*.html and aggregate report files in a directory."""

    # ── Per-house reports: efficiency fix + title rename ──
    per_house_pattern = os.path.join(directory, 'dynamic_report_*.html')
    files = glob.glob(per_house_pattern)

    modified = 0
    for filepath in sorted(files):
        with open(filepath, 'r', encoding='utf-8') as f:
            original = f.read()

        patched = original
        if 'Detection Efficiency' in original:
            patched = patch_report(patched)
        patched = rename_titles(patched, os.path.basename(filepath))

        if patched == original:
            continue

        house_match = re.search(r'dynamic_report_(\d+)', os.path.basename(filepath))
        house_id = house_match.group(1) if house_match else '?'

        if dry_run:
            print(f"  [DRY RUN] house {house_id}: would patch")
        else:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(patched)
            print(f"  house {house_id}: patched")

        modified += 1

    # ── Aggregate reports: title rename only ──
    for name in ['report.html', 'nan_comparison.html']:
        filepath = os.path.join(directory, name)
        if not os.path.isfile(filepath):
            continue

        with open(filepath, 'r', encoding='utf-8') as f:
            original = f.read()

        patched = rename_titles(original, name)

        if patched == original:
            continue

        if dry_run:
            print(f"  [DRY RUN] {name}: would rename title")
        else:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(patched)
            print(f"  {name}: renamed title")

        modified += 1

    if modified == 0:
        print(f"  No changes needed in {directory}")

    return modified


def main():
    if len(sys.argv) < 2:
        print("Usage: python patch_efficiency_cap.py [--dry-run] <dir1> [dir2] ...")
        sys.exit(1)

    dry_run = '--dry-run' in sys.argv
    dirs = [a for a in sys.argv[1:] if a != '--dry-run']

    total = 0
    for d in dirs:
        if not os.path.isdir(d):
            print(f"Warning: {d} is not a directory, skipping")
            continue
        print(f"Processing: {d}")
        total += process_directory(d, dry_run=dry_run)

    print(f"\nDone. Patched {total} files." + (" (dry run)" if dry_run else ""))


if __name__ == '__main__':
    main()
