#!/usr/bin/env python3
"""Forecast GTA VI launch-day Twitch viewers hour by hour.

Reads every case file (*.in) in the input directory and writes one
forecast file (*.out) per case to the output directory.

Method (a platform-aware kernel quantile model, fitted at run time from
the case's own comparables -- past big game launches):

  * For each hour h = 2..24, the forecast quantiles are the weighted
    empirical quantiles of that hour's viewer counts across the
    comparable launches, weighted by similarity to this case:
      - a Gaussian kernel on log(hour-1 viewers) with bandwidth 0.5,
        so launches whose opening hour looked like this one count most;
      - a platform weight: launches on the same platform class count
        fully, launches on the other class count one quarter. GTA VI
        is a console-only launch with rolling midnight unlocks, and
        console-only launches rise much more steeply through the day
        than PC launches with a simultaneous global unlock, so the
        console peers matter most here.
  * Quantile curves are smoothed across neighbouring hours, and the
    implied log-spread is floored so the 5%-95% band never collapses
    when the kernel's effective sample gets small.

No network, no randomness, standard library only: two runs on the
same input produce byte-identical output.
"""

import json
import math
import os
import sys
from datetime import datetime

TAUS = (0.05, 0.25, 0.5, 0.75, 0.95)
# Standard normal quantiles at TAUS.
Z = (-1.6448536269514722, -0.6744897501960817, 0.0,
     0.6744897501960817, 1.6448536269514722)
# Gaussian kernel bandwidth in log(hour-1 viewers).
BW = 0.5
# Weight of a comparable on the other platform class.
OTHER_W = 0.25
# Weight of a comparable whose platforms are unknown.
UNKNOWN_W = 0.5
# Minimum implied log-spread (standard deviations) of the 5%-95% band.
FLOOR_SD = 0.30
# GTA VI releases on PlayStation 5 and Xbox Series X|S: console-only,
# with rolling midnight unlocks (see the competition rules).
TARGET_CLASS = "CON"


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def platform_class(platforms):
    if not platforms:
        return "UNK"
    if "pc" in platforms.lower():
        return "PC"
    return "CON"


def release_curve(comp):
    """Hourly viewers on the comparable's release UTC day.

    Returns {offset: viewers} with offset 0 = the day's first hour.
    """
    try:
        day_start = parse_time(comp["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    curve = {}
    for point in comp.get("hourly") or []:
        try:
            viewers = point["viewers"]
            at = parse_time(point["from"])
        except (KeyError, TypeError, ValueError):
            continue
        if viewers is None or viewers <= 0:
            continue
        offset = int((at - day_start).total_seconds() // 3600)
        if 0 <= offset < 24:
            curve[offset] = viewers
    return curve


def weighted_quantile(pairs, probs):
    """Weighted quantiles of (value, weight) pairs.

    Values are sorted; the quantile is the value whose cumulative
    weight first reaches the target probability, linearly interpolated
    with the previous value so the result moves smoothly as the
    kernel weights shift.
    """
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0 or not ordered:
        return None
    cumulative = []
    running = 0.0
    for value, weight in ordered:
        running += weight
        cumulative.append((running, value))
    out = []
    for prob in probs:
        target = prob * total
        idx = 0
        while idx < len(cumulative) - 1 and cumulative[idx][0] < target:
            idx += 1
        if idx == 0:
            out.append(cumulative[0][1])
            continue
        prev_cum, prev_val = cumulative[idx - 1]
        cum, val = cumulative[idx]
        if cum <= prev_cum:
            out.append(val)
        else:
            frac = (target - prev_cum) / (cum - prev_cum)
            out.append(prev_val + (val - prev_val) * frac)
    return out


def smooth3(values):
    """Three-point centred moving average over the sorted keys."""
    keys = sorted(values)
    out = {}
    for i, key in enumerate(keys):
        neighbours = [values[k] for k in keys[max(0, i - 1):i + 2]]
        out[key] = sum(neighbours) / len(neighbours)
    return out


def baseline_row(hour, first_print):
    """House-baseline row: flat median, spread widening with the hour."""
    return [first_print * math.exp(Z[i] * 0.15 * math.sqrt(hour - 1))
            for i in range(len(TAUS))]


def forecast_case(case):
    known = case.get("known") or []
    first_print = 1.0
    for row in known:
        if row.get("hour") == 1:
            first_print = max(float(row.get("viewers") or 1), 1.0)
            break
    else:
        if known:
            first_print = max(float(known[0].get("viewers") or 1), 1.0)

    comparables = []
    for comp in case.get("comparables") or []:
        curve = release_curve(comp)
        if 0 in curve:
            comparables.append((curve, platform_class(comp.get("platforms"))))

    log_first = math.log(first_print)
    raw = {}
    for hour in range(2, 25):
        pairs = []
        for curve, cls in comparables:
            if hour - 1 not in curve:
                continue
            if cls == TARGET_CLASS:
                class_w = 1.0
            elif cls == "UNK":
                class_w = UNKNOWN_W
            else:
                class_w = OTHER_W
            kernel_w = math.exp(-0.5 * ((math.log(curve[0]) - log_first) / BW) ** 2)
            pairs.append((math.log(curve[hour - 1]), kernel_w * class_w))
        if len(pairs) < 3:
            raw[hour] = None
        else:
            raw[hour] = weighted_quantile(pairs, TAUS)

    # Smooth each quantile across neighbouring hours (log space).
    smoothed = {i: {} for i in range(len(TAUS))}
    for i in range(len(TAUS)):
        column = {}
        for hour in range(2, 25):
            if raw[hour] is not None:
                column[hour] = raw[hour][i]
        for hour, value in smooth3(column).items():
            smoothed[i][hour] = value

    hours = []
    for hour in range(2, 25):
        if raw[hour] is None or hour not in smoothed[2]:
            row = baseline_row(hour, first_print)
        else:
            row = [smoothed[i][hour] for i in range(len(TAUS))]
            # Floor the implied log-spread so the band never collapses.
            median = row[2]
            span = (row[4] - row[0]) / (2.0 * 1.6448536269514722)
            if span < FLOOR_SD:
                factor = FLOOR_SD / span if span > 1e-9 else 1.0
                row = [median + (x - median) * factor for x in row]
            row = [math.exp(x) for x in row]
        # Round, clamp to the allowed range, keep non-decreasing.
        row = [round(max(min(x, 1e9), 0.01), 2) for x in row]
        for i in range(1, len(row)):
            row[i] = max(row[i], row[i - 1])
        hours.append({"hour": hour, "q": row})
    return {"hours": hours}


def main():
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(output_dir, exist_ok=True)
    names = sorted(n for n in os.listdir(input_dir) if n.endswith(".in"))
    for name in names:
        with open(os.path.join(input_dir, name), "r", encoding="utf-8") as handle:
            case = json.load(handle)
        result = forecast_case(case)
        out_name = name[:-3] + ".out"
        with open(os.path.join(output_dir, out_name), "w", encoding="utf-8") as handle:
            json.dump(result, handle)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
