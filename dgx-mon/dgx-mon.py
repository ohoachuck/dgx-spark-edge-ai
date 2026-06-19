#!/usr/bin/env python3
"""dgx-mon — moniteur ressources temps réel du DGX Spark (CPU · GPU · mémoire · Ollama · conteneurs).

À lancer SUR le DGX (a besoin de /proc, nvidia-smi, docker, et de l'API Ollama locale).
  python3 dgx-mon.py            # live, rafraîchi toutes les 2 s (Ctrl-C pour quitter)
  python3 dgx-mon.py -i 1       # intervalle 1 s
  python3 dgx-mon.py --once     # une seule capture (utile en SSH non interactif)

Spécificités GB10 : la mémoire est UNIFIÉE (CPU+GPU partagent les ~125 Gio).
nvidia-smi ne rapporte pas memory.used (N/A) → la « VRAM » est dérivée de la somme
des process GPU (--query-compute-apps). Le runner d'un modèle Ollama apparaît comme
`llama-server`, et `ollama /api/ps` donne le nom du modèle chargé.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

def _ollama_url() -> str:
    """URL de l'API Ollama /api/ps. Par défaut le port standard d'Ollama
    (127.0.0.1:11434 — install systemd standard sur DGX Spark). Surchargeable via
    la variable d'env DGXMON_OLLAMA si Ollama écoute ailleurs — ex. dans ton
    ~/.bashrc : export DGXMON_OLLAMA=127.0.0.1:12001 (ou "http://hote:port").
    NB : on N'utilise PAS OLLAMA_HOST, réservée au serveur/CLI Ollama lui-même."""
    env = os.environ.get("DGXMON_OLLAMA", "").strip()
    if not env:
        return "http://127.0.0.1:11434/api/ps"
    base = env.rstrip("/") if env.startswith(("http://", "https://")) else "http://" + env
    return base + "/api/ps"


OLLAMA_URL = _ollama_url()
GIB = 1024 ** 2  # meminfo est en kB → /GIB = Gio


# ----------------------------- collecte -----------------------------
def run(cmd: list[str], timeout: float = 4.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def read_meminfo() -> dict[str, int]:
    out = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            out[k.strip()] = int(v.strip().split()[0])  # kB
    except Exception:
        pass
    return out


def read_cpu() -> tuple[int, int]:
    """Retourne (busy, total) cumulés depuis /proc/stat."""
    try:
        parts = open("/proc/stat").readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        total = sum(vals)
        return total - idle, total
    except Exception:
        return 0, 0


def loadavg() -> str:
    try:
        a = open("/proc/loadavg").read().split()
        return f"{a[0]} {a[1]} {a[2]}"
    except Exception:
        return "n/a"


def gpu_info() -> dict:
    out = run(["nvidia-smi",
               "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw",
               "--format=csv,noheader,nounits"])
    d = {"name": "GPU", "util": None, "temp": None, "power": None}
    if out.strip():
        f = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        if len(f) >= 4:
            d["name"] = f[0]
            d["util"] = _num(f[1]); d["temp"] = _num(f[2]); d["power"] = _num(f[3])
    return d


def gpu_procs() -> list[tuple[str, str, int]]:
    """[(pid, name, mem_MiB)] des process GPU."""
    out = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
               "--format=csv,noheader,nounits"])
    res = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 3 and f[0]:
            res.append((f[0], f[1].split("/")[-1], _int(f[2])))
    return res


def ollama_ps() -> list[dict]:
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=3) as r:
            return json.load(r).get("models", [])
    except Exception:
        return []


def docker_stats() -> list[dict]:
    out = run(["docker", "stats", "--no-stream", "--format",
               "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"], timeout=8)
    rows = []
    for line in out.strip().splitlines():
        p = line.split("|")
        if len(p) == 4:
            rows.append({"name": p[0], "cpu": p[1], "mem": p[2].split("/")[0].strip(),
                         "memperc": _num(p[3])})
    rows.sort(key=lambda r: r["memperc"] or 0, reverse=True)
    return rows


def _num(s):
    try:
        return float(str(s).replace("%", "").strip())
    except Exception:
        return None


def _int(s):
    try:
        return int(float(s))
    except Exception:
        return 0


# ----------------------------- rendu -----------------------------
def bar(frac: float, width: int = 28) -> str:
    frac = max(0.0, min(1.0, frac))
    fill = int(round(frac * width))
    color = "green" if frac < 0.70 else ("yellow" if frac < 0.85 else "red")
    return f"[{color}]{'█' * fill}[/][grey37]{'░' * (width - fill)}[/]"


def fmt_until(expires_at: str) -> str:
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        secs = (exp - datetime.now(timezone.utc)).total_seconds()
        if secs <= 0:
            return "expire…"
        if secs < 90:
            return f"{int(secs)} s"
        if secs < 5400:
            return f"{int(secs / 60)} min"
        return f"{secs / 3600:.1f} h"
    except Exception:
        return "—"


def panel_cpu(cpu_pct, ncpu) -> Panel:
    t = Text()
    t.append(f"Utilisation : {cpu_pct:5.1f} %\n", style="bold")
    t.append_text(Text.from_markup(bar(cpu_pct / 100) + "\n"))
    t.append(f"Cœurs : {ncpu}   ", style="grey70")
    t.append(f"load (1·5·15m) : {loadavg()}", style="grey70")
    return Panel(t, title="[bold cyan]CPU (Grace, Arm)[/]", border_style="cyan")


def panel_mem(mi, gpu_used_gib) -> Panel:
    total = mi.get("MemTotal", 0) / GIB
    avail = mi.get("MemAvailable", 0) / GIB
    used = total - avail
    cached = mi.get("Cached", 0) / GIB
    swt = mi.get("SwapTotal", 0) / GIB
    swf = mi.get("SwapFree", 0) / GIB
    frac = used / total if total else 0
    t = Text()
    t.append(f"Utilisée  : {used:6.1f} / {total:.1f} Gio  ({frac*100:.0f} %)\n", style="bold")
    t.append_text(Text.from_markup(bar(frac) + "\n"))
    t.append(f"Disponible: {avail:6.1f} Gio    Cache : {cached:5.1f} Gio\n", style="grey70")
    t.append(f"dont GPU  : {gpu_used_gib:6.1f} Gio (mémoire UNIFIÉE)\n", style="magenta")
    t.append(f"Swap      : {swt-swf:5.1f} / {swt:.1f} Gio", style="grey70")
    return Panel(t, title="[bold green]Mémoire unifiée[/]", border_style="green")


def panel_gpu(g, gpu_used_gib, total_gib) -> Panel:
    t = Text()
    util = g["util"] or 0
    t.append(f"{g['name']}\n", style="bold orange1")
    t.append(f"Calcul : {util:5.1f} %\n", style="bold")
    t.append_text(Text.from_markup(bar(util / 100) + "\n"))
    frac = gpu_used_gib / total_gib if total_gib else 0
    t.append(f"VRAM*  : {gpu_used_gib:5.1f} Gio  ({frac*100:.0f} % du pool)\n", style="magenta")
    temp = f"{g['temp']:.0f}°C" if g["temp"] is not None else "n/a"
    pw = f"{g['power']:.1f} W" if g["power"] is not None else "n/a"
    t.append(f"Temp : {temp}    Conso : {pw}\n", style="grey70")
    t.append("*dérivée des process GPU (mémoire unifiée)", style="grey50 italic")
    return Panel(t, title="[bold orange1]GPU GB10 (Blackwell)[/]", border_style="orange1")


def table_ollama(models) -> Table:
    tb = Table(title="[bold]Modèles Ollama chargés[/]", title_style="bold",
               border_style="orange3", expand=True, header_style="bold orange1")
    tb.add_column("Modèle"); tb.add_column("Taille", justify="right")
    tb.add_column("Sur", justify="right"); tb.add_column("Contexte", justify="right")
    tb.add_column("Expire dans", justify="right")
    if not models:
        tb.add_row("[grey50]— aucun modèle en mémoire —[/]", "", "", "", "")
    for m in models:
        size = m.get("size", 0); vram = m.get("size_vram", 0)
        on = "100 % GPU" if vram >= size and size else (f"{int(vram/size*100)} % GPU" if size else "—")
        ctx = m.get("context") or (m.get("details") or {}).get("context") or ""
        tb.add_row(f"[bold]{m.get('name','?')}[/]", f"{size/1e9:.1f} GB",
                   on, str(ctx), fmt_until(m.get("expires_at", "")))
    return tb


def table_gpu_procs(procs) -> Table:
    tb = Table(title="[bold]Process GPU[/]", title_style="bold",
               border_style="magenta", expand=True, header_style="bold magenta")
    tb.add_column("PID", justify="right"); tb.add_column("Process")
    tb.add_column("Mém. GPU", justify="right")
    if not procs:
        tb.add_row("", "[grey50]— aucun —[/]", "")
    for pid, name, mib in sorted(procs, key=lambda x: x[2], reverse=True):
        tb.add_row(pid, name, f"{mib/1024:.2f} Gio")
    return tb


def table_containers(rows, top=12) -> Table:
    tb = Table(title="[bold]Conteneurs Docker (par mémoire)[/]", title_style="bold",
               border_style="blue", expand=True, header_style="bold blue")
    tb.add_column("Conteneur"); tb.add_column("CPU %", justify="right")
    tb.add_column("Mémoire", justify="right"); tb.add_column("Mém %", justify="right")
    for r in rows[:top]:
        cpu = r["cpu"]; cstyle = "red" if (_num(cpu) or 0) > 50 else ""
        tb.add_row(r["name"], f"[{cstyle}]{cpu}[/]" if cstyle else cpu,
                   r["mem"], f"{r['memperc']:.1f} %" if r["memperc"] is not None else "—")
    return tb


def build(prev) -> tuple[Group, tuple]:
    mi = read_meminfo()
    busy, total = read_cpu()
    pb, pt = prev
    cpu_pct = (busy - pb) / (total - pt) * 100 if (total - pt) > 0 else 0.0
    ncpu = sum(1 for l in open("/proc/stat") if l.startswith("cpu") and l[3].isdigit())
    g = gpu_info()
    procs = gpu_procs()
    gpu_used_gib = sum(p[2] for p in procs) / 1024
    total_gib = mi.get("MemTotal", 0) / GIB
    models = ollama_ps()
    conts = docker_stats()

    header = Text.from_markup(
        f"[bold orange1]🖥  DGX Spark — moniteur ressources[/]   "
        f"[grey62]{datetime.now().strftime('%H:%M:%S')} · rafraîchi en continu · Ctrl-C pour quitter[/]")

    top = Table.grid(expand=True)
    top.add_column(ratio=1); top.add_column(ratio=1); top.add_column(ratio=1)
    top.add_row(panel_cpu(cpu_pct, ncpu), panel_mem(mi, gpu_used_gib),
                panel_gpu(g, gpu_used_gib, total_gib))

    group = Group(header, top, table_ollama(models),
                  table_gpu_procs(procs), table_containers(conts))
    return group, (busy, total)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="intervalle en s (défaut 2)")
    ap.add_argument("--once", action="store_true", help="une seule capture puis quitte")
    args = ap.parse_args()
    console = Console()

    prev = read_cpu()
    if args.once:
        time.sleep(0.3)
        group, _ = build(prev)
        console.print(group)
        return 0
    try:
        with Live(console=console, screen=True, refresh_per_second=4) as live:
            while True:
                time.sleep(args.interval)
                group, prev = build(prev)
                live.update(group)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
