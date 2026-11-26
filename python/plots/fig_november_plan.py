"""Figures of the November bid plan (lab style).

fig01_stress_window: Nov 16-21, the stress-window plan.
figS01_november_plan: the whole month.

Reads the per-period plan CSV written by `python -m bessbid generate` (`--plan-out`, default
`<work-dir>/<out-stem>.plan.csv`) and writes PDF, SVG and PNG files plus a `*_numbers.json`
sidecar per figure to the output dir. The plan derives from EMA's confidential data, so keep
the output dir outside the repository (e.g. `<work-dir>/figures`).

Usage: python plots/fig_november_plan.py <plan.csv> <output dir>
Needs matplotlib (requirements-figures.txt).
"""
import argparse

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from labstyle import set_lab_style, WIDTH, TECH, NEUTRAL, panel_label, direct_label, save, save_numbers

BATT = TECH["battery"]
SHADE = "#EFEFEF"
TIGHT, LOOSE = "soc_c1.0000_d0.8500", "soc_c0.8500_d1.0000"
DT = pd.Timedelta(minutes=30)


def event_spans(df):
    spans, start = [], None
    for t, e in zip(df.ts, df.event.astype(bool)):
        if e and start is None:
            start = t
        if not e and start is not None:
            spans.append((start, t)); start = None
    if start is not None:
        spans.append((start, df.ts.iloc[-1] + DT))
    return spans


def draw(df, name, day_fmt, day_step, annotate_event):
    fig, axes = plt.subplots(3, 1, figsize=(WIDTH["nature_2col"], 120 / 25.4), sharex=True,
                             gridspec_kw={"hspace": 0.28})
    a, b, c = axes
    for ax in axes:
        for s, e in event_spans(df):
            ax.axvspan(s, e, color=SHADE, lw=0, zorder=0)
    # a: price forecast
    a.plot(df.ts + DT / 2, df.fc_day_ahead_E, color=NEUTRAL["muted"], lw=1.0)
    a.set_ylabel("Price forecast\n(S$/MWh)")
    # b: planned energy offers
    mw = df.energy_mw.to_numpy()
    dis, ch = mw > 0, mw < 0
    b.bar(df.ts[dis], mw[dis], width=DT * 0.9, align="edge", color=BATT, lw=0)
    b.bar(df.ts[ch], mw[ch], width=DT * 0.9, align="edge", color=BATT, alpha=0.6, lw=0)
    b.axhline(0, color="#000000", lw=0.6)
    b.set_ylabel("Energy offer\n(MW)"); b.set_ylim(-105, 105); b.set_yticks([-100, -50, 0, 50, 100])
    b.set_yticklabels(["−100", "−50", "0", "50", "100"])
    # c: stored energy
    c.step(df.ts + DT, df[TIGHT], where="pre", color=BATT, lw=1.2, label="All loss on discharge")
    c.step(df.ts + DT, df[LOOSE], where="pre", color=NEUTRAL["muted"], lw=0.6, label="All loss on charge")
    for y, lab in [(95, "95% limit"), (5, "5% limit")]:
        c.axhline(y, ls="--", lw=0.8, color=NEUTRAL["muted"], zorder=1)
        c.annotate(lab, (1, y), xycoords=("axes fraction", "data"), xytext=(2, 0), textcoords="offset points",
                   ha="left", va="center", fontsize=6, color=NEUTRAL["muted"])
    c.set_ylabel("Stored energy\n(MWh)"); c.set_ylim(0, 100); c.set_yticks([0, 25, 50, 75, 100])
    c.xaxis.set_major_locator(mdates.DayLocator(interval=day_step))
    c.xaxis.set_major_formatter(mdates.DateFormatter(day_fmt))
    c.set_xlim(df.ts.iloc[0], df.ts.iloc[-1] + DT)
    if annotate_event:
        c.legend(loc="center", bbox_to_anchor=(0.62, 0.5))
    else:
        c.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=2, borderaxespad=0.2)
    for ax, letter in zip(axes, "abc"):
        panel_label(ax, letter, x=-0.075)
    numbers = {}
    if annotate_event:
        ev = event_spans(df)[0]
        a.annotate("Outage-event window", (ev[0], 1), xycoords=("data", "axes fraction"), xytext=(3, -3),
                   textcoords="offset points", ha="left", va="top", fontsize=6, color=NEUTRAL["muted"])
        d19 = df[(df.ts.dt.day == 19) & (df.energy_mw > 0)]
        sold = float((d19.energy_mw * 0.5).sum()); pk = d19.loc[d19.energy_mw.idxmax()]
        b.annotate(f"{sold:.0f} MWh sold", (pk.ts + DT, pk.energy_mw), xytext=(4, 0), textcoords="offset points",
                   ha="left", va="center", fontsize=6, color=BATT)
        direct_label(b, df.ts.iloc[0], 70, "Discharge", BATT, dx=2, ha="left")
        direct_label(b, df.ts.iloc[0], -70, "Charge", BATT, dx=2, ha="left", alpha=0.8)
        numbers = {"nov19_sold_mwh": round(sold, 1),
                   "nov19_price_at_sales_sgd_mwh": [round(float(v)) for v in d19.fc_day_ahead_E],
                   "nov19_max_forecast_sgd_mwh": round(float(df[df.ts.dt.day == 19].fc_day_ahead_E.max()), 1)}
    normal = df[~df.event.astype(bool)]
    numbers.update({
        "charge_mwh_per_normal_day": round(float(-(normal.energy_mw.clip(upper=0)).sum() * 0.5 / normal.ts.dt.date.nunique()), 1),
        "discharge_mwh_per_normal_day": round(float((normal.energy_mw.clip(lower=0)).sum() * 0.5 / normal.ts.dt.date.nunique()), 1),
        "mean_charge_price_sgd_mwh": round(float(np.average(normal.fc_day_ahead_E[normal.energy_mw < 0], weights=-normal.energy_mw[normal.energy_mw < 0])), 1),
        "mean_discharge_price_sgd_mwh": round(float(np.average(normal.fc_day_ahead_E[normal.energy_mw > 0], weights=normal.energy_mw[normal.energy_mw > 0])), 1),
        "soc_min_mwh": round(float(df[TIGHT].min()), 1), "soc_max_mwh": round(float(df[TIGHT].max()), 1),
    })
    return fig, numbers


def main(plan_path, out_dir):
    set_lab_style()
    p = pd.read_csv(plan_path, parse_dates=["ts"])
    w = p[(p.ts >= "2026-11-16") & (p.ts < "2026-11-22")].reset_index(drop=True)
    fig, nums = draw(w, "fig01", "%d %b", 1, True)
    save(fig, "fig01_stress_window", out_dir); save_numbers("fig01_stress_window", nums, out_dir)
    fig, nums = draw(p, "figS01", "%d %b", 3, False)
    save(fig, "figS01_november_plan", out_dir); save_numbers("figS01_november_plan", nums, out_dir)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Figures of the November bid plan")
    ap.add_argument("plan", help="plan CSV written by `python -m bessbid generate`")
    ap.add_argument("out_dir", help="output folder for the figures (outside the repository)")
    args = ap.parse_args()
    main(args.plan, args.out_dir)
