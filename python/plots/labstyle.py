"""Power Transformation Lab figure style (matplotlib).

Usage:
    from labstyle import set_lab_style, WIDTH, SCENARIO, TECH, panel_label, save
    set_lab_style()

See SKILL.md for the full guidelines this module implements.
"""
import json
import os

import matplotlib as mpl
import matplotlib.pyplot as plt

# ---------------------------------------------------------------- sizes
MM = 1 / 25.4
WIDTH = {
    "nature_1col": 89 * MM, "nature_15col": 120 * MM, "nature_2col": 183 * MM,
    "elsevier_1col": 90 * MM, "elsevier_15col": 140 * MM, "elsevier_2col": 190 * MM,
    "cell_1col": 85 * MM, "cell_15col": 114 * MM, "cell_2col": 174 * MM,
}

# ---------------------------------------------------------------- palettes
SCENARIO = {
    "S1": "#8C8C8C",          # baseline / fragmented / status quo
    "S2": "#0072B2",          # reform A
    "S3": "#E69F00",          # reform B
    "S4": "#009E73",          # full reform
    "interaction": "#CC79A7",
    "total": "#000000",
}

SCENARIO_GROUP = {"group_a": "#C9A227", "group_b": "#1F5FA8", "group_c": "#5A9E3A"}
HISTORICAL = "#000000"

TECH = {
    "coal": "#4D4D4D", "coal_ccs": "#7A7A7A", "gas": "#CC79A7", "oil": "#8C564B",
    "nuclear": "#F4836B", "hydro": "#56B4E9",
    "wind_onshore": "#009E73", "wind_offshore": "#7FD1C7",
    "solar_utility": "#E69F00", "solar_distributed": "#F0E442",
    "geothermal": "#A6761D", "biomass": "#8FBC5A",
    "battery": "#9467BD", "long_duration": "#B8A9D9", "pumped_hydro": "#3A7DBF",
    "grid_import": "#0072B2", "nse": "#D62728", "demand": "#000000",
}

# Recommended bottom-to-top stack order for dispatch / capacity stacks
STACK_ORDER = ["nuclear", "coal", "coal_ccs", "gas", "oil", "hydro", "geothermal",
               "biomass", "wind_onshore", "wind_offshore", "solar_utility",
               "solar_distributed", "pumped_hydro", "battery", "long_duration",
               "grid_import", "nse"]

CATEGORICAL = ["#D55E00", "#0072B2", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#F0E442"]
OTHER = "#999999"

ORDINAL = {"HIGH": "#F5E642", "MEDIUM": "#3DBB75", "LOW": "#2E6D8E", "NEGATIVE": "#440154"}

SCHEMATIC = {"exog_varying": "#1F5FA8", "exog_fixed": "#B5B5B0",
             "endogenous": "#E8B52E", "output": "#C38A1A"}

NEUTRAL = {"text": "#333333", "muted": "#555555", "land": "#E6E6E6",
           "grid": "#E5E5E5", "reference": "#D85C9A"}

MINUS = "\u2212"


# ---------------------------------------------------------------- style
def set_lab_style():
    """Apply lab rcParams. Sizes are for FINAL print size."""
    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 7, "axes.labelsize": 7, "axes.titlesize": 7,
        "xtick.labelsize": 6, "ytick.labelsize": 6, "legend.fontsize": 6,
        "legend.frameon": False, "legend.handlelength": 1.2,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.linewidth": 0.6, "axes.grid": False,
        "axes.edgecolor": "#000000", "axes.labelcolor": "#000000",
        "xtick.major.width": 0.6, "ytick.major.width": 0.6,
        "xtick.major.size": 2.5, "ytick.major.size": 2.5,
        "lines.linewidth": 1.2, "lines.markersize": 4,
        "patch.linewidth": 0.4,
        "mathtext.default": "regular",
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
        "figure.dpi": 150, "savefig.dpi": 600,
        "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
        "figure.facecolor": "white", "axes.facecolor": "white",
    })


# ---------------------------------------------------------------- helpers
def tint(hex_color, amount=0.5):
    """Lighter tint of a color (e.g., emissions bar paired with a cost bar)."""
    r, g, b = mpl.colors.to_rgb(hex_color)
    return (r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount)


def panel_label(ax, letter, x=-0.14, y=1.03):
    """Bold lowercase panel letter, top-left outside the axes."""
    ax.text(x, y, letter, transform=ax.transAxes, fontsize=8,
            fontweight="bold", va="bottom", ha="left")


def pct(value, decimals=1, signed=True):
    """Format a % change with a true minus sign: -24.4 -> '−24.4%'."""
    s = f"{abs(value):.{decimals}f}%"
    if value < 0:
        return MINUS + s
    return ("+" + s) if (signed and value > 0) else s


def direct_label(ax, x, y, text, color, dx=3, dy=0, ha="left", va="center", **kw):
    """Label a series in its own color next to a point or line end."""
    return ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points",
                       color=color, fontsize=6, ha=ha, va=va, **kw)


def ref_line(ax, value, label=None, orient="h", color=None):
    """Labeled dashed reference (horizontal) or event (vertical) line."""
    if orient == "h":
        color = color or NEUTRAL["reference"]
        ax.axhline(value, ls="--", lw=0.8, color=color, zorder=1)
        if label:
            ax.annotate(label, (1, value), xycoords=("axes fraction", "data"),
                        xytext=(-2, 2), textcoords="offset points",
                        ha="right", va="bottom", fontsize=6, color=color)
    else:
        color = color or NEUTRAL["muted"]
        ax.axvline(value, ls="--", lw=0.8, color=color, zorder=1)
        if label:
            ax.annotate(label, (value, 1), xycoords=("data", "axes fraction"),
                        xytext=(-3, -2), textcoords="offset points",
                        ha="right", va="top", fontsize=6, color=color)


def bar_change_labels(ax, xs, tops, values, decimals=1, color="#000000", dy=2):
    """Put % change labels (vs. baseline) above bars."""
    for x, top, v in zip(xs, tops, values):
        if v is None:
            continue
        ax.annotate(pct(v, decimals), (x, top), xytext=(0, dy), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6, color=color)


def save(fig, name, outdir="figures"):
    """Export PDF + SVG (vector, embedded fonts) and a 300 dpi PNG."""
    os.makedirs(outdir, exist_ok=True)
    fig.savefig(os.path.join(outdir, f"{name}.pdf"))
    fig.savefig(os.path.join(outdir, f"{name}.svg"))
    fig.savefig(os.path.join(outdir, f"{name}.png"), dpi=300)


def save_numbers(name, numbers, outdir="figures"):
    """Write every number annotated on a figure to a JSON sidecar for text cross-checks."""
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, f"{name}_numbers.json"), "w") as f:
        json.dump(numbers, f, indent=2)
