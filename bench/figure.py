"""Draw a results table as an SVG (and a PNG beside it when rsvg-convert is installed): mean judged score against
tokens into Claude, log scale.

  uv run python -m bench.figure bench/results-public/report-all.json docs/benchmark-public.svg "mean judged score (89 questions)"

Okabe-Ito colours picked so each family differs in lightness (L* 0, 46, 71, 89), plus a marker shape and a text code
per method, so the figure reads without colour. Hollow marks are vault-memory without memory cards, filled marks the
same mode with them. Methods not listed in STYLE are left out of the figure (they stay in the table)."""
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

STYLE = {  # method -> (code shown at the point, legend name, colour, shape, filled)
    "grep_agent": ("G", "agent with Grep, Glob and Read", "#000000", "circle", True),
    "ctx2": ("C2", "context-mode search, 2 hits", "#F0E442", "square", True),
    "ctx5": ("C5", "context-mode search, 5 hits", "#F0E442", "square", True),
    "vm_fast": ("F", "vault-memory fast, no cards", "#E69F00", "diamond", False),
    "vm_fast_cards": ("F+", "vault-memory fast, with cards", "#E69F00", "diamond", True),
    "vm_agent": ("A", "vault-memory agent, no cards", "#0072B2", "star", False),
    "vm_agent_cards": ("A+", "vault-memory agent, with cards", "#0072B2", "star", True),
}
EDGE = {"#F0E442": "#6b5f00"}  # the yellow needs a dark outline on white
LABEL_AT = {"vm_agent_cards": (-13, -8, "end"), "vm_fast": (-13, 18, "end"), "ctx5": (11, 20, "start")}  # crowded points
W, H, L, R, T, B = 900, 440, 70, 360, 30, 60


def marker(shape: str, x: float, y: float, c: str, filled: bool = True) -> str:
    edge = f' stroke="{EDGE[c]}" stroke-width="1.5"' if c in EDGE else ""
    return _marker(shape, x, y).replace("FILL", f'fill="{c}"{edge}' if filled else f'fill="#ffffff" stroke="{c}" stroke-width="2.5"')


def _marker(shape: str, x: float, y: float) -> str:
    s = 7
    if shape == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{s}" FILL/>'
    if shape == "square":
        return f'<rect x="{x - s:.1f}" y="{y - s:.1f}" width="{2 * s}" height="{2 * s}" FILL/>'
    if shape == "diamond":
        return f'<polygon points="{x},{y - s - 2} {x + s + 2},{y} {x},{y + s + 2} {x - s - 2},{y}" FILL/>'
    if shape == "triangle":
        return f'<polygon points="{x},{y - s - 1} {x + s + 1},{y + s} {x - s - 1},{y + s}" FILL/>'
    pts = []
    for i in range(10):
        r = s + 3 if i % 2 == 0 else (s + 3) / 2.3
        a = math.pi / 2 + i * math.pi / 5
        pts.append(f"{x + r * math.cos(a):.1f},{y - r * math.sin(a):.1f}")
    return f'<polygon points="{" ".join(pts)}" FILL/>'


def main(table: str, out: str, ylabel: str = "mean judged score (held-out test)") -> None:
    rows = [r for r in json.loads(Path(table).read_text()) if r["method"] in STYLE]
    xmin = 500
    xmax = 5000 if max(r["tokens"] for r in rows) < 4000 else 30000
    px = lambda t: L + (math.log10(t) - math.log10(xmin)) / (math.log10(xmax) - math.log10(xmin)) * (W - L - R)
    py = lambda c: T + (1 - (c - 0.5) / 0.5) * (H - T - B)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="-apple-system, Helvetica, Arial, sans-serif" font-size="13">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>']
    for c in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        svg.append(f'<line x1="{L}" x2="{W - R}" y1="{py(c):.1f}" y2="{py(c):.1f}" stroke="#e5e5e5"/>')
        svg.append(f'<text x="{L - 8}" y="{py(c) + 4:.1f}" text-anchor="end" fill="#444">{c:.1f}</text>')
    for t in [t for t in (500, 1000, 2000, 3000, 5000, 10000, 30000) if t <= xmax and (xmax < 30000 or t != 2000 and t != 5000)]:
        svg.append(f'<line x1="{px(t):.1f}" x2="{px(t):.1f}" y1="{T}" y2="{H - B}" stroke="#e5e5e5"/>')
        svg.append(f'<text x="{px(t):.1f}" y="{H - B + 18}" text-anchor="middle" fill="#444">{t:,}</text>')
    svg.append(f'<text x="{(L + W - R) / 2}" y="{H - 14}" text-anchor="middle" fill="#222">tokens into Claude per question (log scale)</text>')
    svg.append(f'<text transform="translate(18,{(T + H - B) / 2}) rotate(-90)" text-anchor="middle" fill="#222">{ylabel}</text>')
    for k, r in enumerate(rows):
        code, name, colour, shape, filled = STYLE[r["method"]]
        x, y = px(max(xmin, r["tokens"])), py(r["correct"])
        svg.append(marker(shape, x, y, colour, filled))
        dx, dy, anchor = LABEL_AT.get(r["method"], (11, -8, "start"))
        svg.append(f'<text x="{x + dx:.1f}" y="{y + dy:.1f}" font-weight="600" fill="#111" text-anchor="{anchor}">{code}</text>')
        ly = T + 18 + k * 34
        svg.append(marker(shape, W - R + 30, ly, colour, filled))
        svg.append(f'<text x="{W - R + 46}" y="{ly + 4}" fill="#111"><tspan font-weight="600">{code}</tspan> {name}</text>')
        svg.append(f'<text x="{W - R + 46}" y="{ly + 20}" fill="#555">{r["correct"]:.2f} correct, {r["tokens"]:,} tokens, {r["seconds_p50"]:.{2 if r["seconds_p50"] >= 0.1 else 3}f} s</text>')
    svg.append(f'<text x="{W - R + 22}" y="{T + 18 + len(rows) * 34 + 6}" fill="#555">Hollow: without memory cards. Filled: with them.</text>')
    svg.append("</svg>")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(svg))
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-z", "2", "-o", str(Path(out).with_suffix(".png")), out], check=True)
    print(f"wrote {out}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
