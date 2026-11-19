# periodic-kernel-leak-ml

Linux kernel fast-loop telemetry module, static-buffer C poller, and Python Machine Learning viewer designed to isolate **fast periodic memory allocations** early enough to spot slow-burn RAM exhaustion (days or weeks out) before memory fills up.

## Architecture

1. **Kernel Module (`kernel/abb_leak_mod.c`)**: Registers `/dev/abb_leak` as a Linux `miscdevice` and runs `ABB_FAST_LOOP_LEAK_MACRO` inside a fast loop on read, logging allocation samples into a static power-of-two ring buffer (`& RING_MASK`, zero `%` division) with cache-line-isolated producer/consumer indices (`__attribute__((aligned(64)))`).
2. **Fast Poller (`poller/abb_poller.c`)**: Uses static storage only (no `malloc`) and polls `/dev/abb_leak` on a fast periodic cadence, streaming `ts_ns,bytes,iter` CSV lines to `stdout`.
3. **Python ML Viewer (`viewer/abb_ml_viewer.py`)**: Extracts windowed spectral and periodicity features (Hann-windowed FFT fast-band ratio, peak-to-median ratio, short-lag autocorrelation, and allocation byte rate) gated with `IsolationForest` so **only fast periodic allocation patterns** trigger alerts while ignoring steady baselines, isolated spikes, and slow ramps, alongside a days/weeks RAM-full projection from `/proc/meminfo` (`MemAvailable`).

## Project Layout

```text
.
├── kernel/
│   ├── abb_leak_mod.c
│   ├── Kbuild
│   └── Makefile
├── poller/
│   └── abb_poller.c
└── viewer/
    └── abb_ml_viewer.py
Build & Run
1. Run Synthetic Self-Test (--self-test)
Bash
python3 viewer/abb_ml_viewer.py --self-test
2. Build Kernel Module & C Poller
Bash
make -C kernel clean && make -C kernel
gcc -O2 -Wall -Wextra poller/abb_poller.c -o poller/abb_poller
3. Load Module, Stream Telemetry, & Unload
Bash
sudo insmod kernel/abb_leak_mod.ko
sudo ./poller/abb_poller | python3 viewer/abb_ml_viewer.py
When finished testing, unload the module to stop test allocations and inspect dmesg:

Bash
sudo rmmod abb_leak_mod
sudo dmesg | grep -i "abb_leak" | tail -n 10
Attribution
Created by Natsagnyam Namkhai (Natska) — periodic-kernel-leak-ml.
