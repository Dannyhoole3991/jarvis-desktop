"""
Static leakage lint for generated strategy source.

Freqtrade's lookahead-analysis is a dynamic test and has blind spots (it cannot
see a one-candle leak through an informative timeframe, for example, because
the higher-timeframe candle on the truncation boundary is loaded complete).
Because every strategy in this lab is generated from templates, the source can
also be checked structurally for any construct that reads the future. Both
checks must pass; this one is cheap and runs first.
"""

import re

_PATTERNS = [
    (r"\.shift\(\s*-", "negative shift (reads a later candle)"),
    (r"center\s*=\s*True", "centred rolling window (uses later candles)"),
    (r"\.(bfill|backfill)\(", "backward fill (copies a later value backwards)"),
    (r"\.interpolate\(", "interpolation (uses later values)"),
    (r"\.iloc\[[^\]]*\+", "forward iloc offset"),
    (r"\.(pct_change)\(\s*-", "negative pct_change period"),
    (r"\.diff\(\s*-", "negative diff period"),
    (r"\.rolling\([^)]*\)\s*\.[a-z]+\(\)\s*\.shift\(\s*-", "rolling then shifted backwards"),
    (r"\b(fut|future|next_)\w*\s*=", "variable named as future data"),
]


def future_reference_findings(source):
    """Returns a list of human-readable findings (empty = clean). Comment lines are ignored."""
    findings = []
    for number, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0]
        for pattern, message in _PATTERNS:
            if re.search(pattern, code):
                findings.append(f"line {number}: {message}: {code.strip()[:100]}")
    return findings
