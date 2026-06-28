# `dgx-mon` — a terminal resource monitor for the DGX Spark

A small, dependency-light Python TUI to watch, **in real time**, what your DGX Spark is doing —
CPU, the GB10's **unified memory**, GPU, which Ollama model is loaded, and which containers eat
what. Handy to *see* a model spin up and the power draw climb when inference starts.

![dgx-mon running on a DGX Spark](dgx-mon.png)

## What each panel shows

- **CPU (Grace, Arm)** — utilisation, core count, 1/5/15-min load average.
- **Unified memory** — used / total, available, cache, *how much the GPU holds* (it's the **same
  pool** on GB10), and swap. This is the metric that matters most on a Spark.
- **GPU GB10 (Blackwell)** — compute %, VRAM (**derived from GPU processes**, because `nvidia-smi`
  reports N/A on unified memory), temperature, power draw.
- **Loaded Ollama models** — name, size, CPU/GPU split, context, time-to-unload.
- **GPU processes** — PID, process, GPU memory.
- **Docker containers** — sorted by memory (CPU %, memory, mem %).

## Run it (on the DGX)

```bash
pip install rich          # only dependency
python3 dgx-mon.py        # live view, refreshes continuously (Ctrl-C to quit)
python3 dgx-mon.py --once # single snapshot (good for logs/SSH)
python3 dgx-mon.py -i 2   # custom refresh interval (seconds)
```

**Requirements**: Python 3.10+, `rich`, plus `nvidia-smi`, `docker` and a local Ollama API on
the machine.

**Ollama endpoint**: defaults to the standard `127.0.0.1:11434` (the systemd install used by the
[NVIDIA DGX Spark playbooks](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/ollama)).
If your Ollama listens elsewhere (e.g. running in a container on another port), point the monitor
at it with an environment variable — no code edit needed:

```bash
export DGXMON_OLLAMA=127.0.0.1:15000   # host:port, or a full http://host:port
```

> Tailored to the **DGX Spark (GB10, unified memory)**. On a discrete-GPU DGX the memory panel
> wouldn't apply as-is.

**License**: MIT — see [`LICENSE`](LICENSE).
