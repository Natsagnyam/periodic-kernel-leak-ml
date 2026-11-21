import collections
import math
import os
import struct
import sys
import tkinter as tk
import numpy as np

DEV_PATH = "/dev/macro_loop"
SAMPLE_STRUCT = struct.Struct("<qqqII")  # ts_ns, state_val, rate_val, loop_running, panic_alarm
PANIC_LIMIT = 5_000_000_000.0


def read_memory_stats():
    mem_total_kb = 1
    mem_avail_kb = 1
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = max(int(line.split()[1]), 1)
                elif line.startswith("MemAvailable:"):
                    mem_avail_kb = max(int(line.split()[1]), 1)
    except OSError:
        pass

    used_mb = (mem_total_kb - mem_avail_kb) / 1024.0
    total_mb = mem_total_kb / 1024.0
    used_pct = max(0.0, min(100.0, (used_mb / total_mb) * 100.0))

    rss_mb = 0.0
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as f:
            pages = int(f.read().split()[1])
            rss_mb = (pages * os.sysconf("SC_PAGE_SIZE")) / (1024.0 * 1024.0)
    except OSError:
        pass

    return used_mb, total_mb, used_pct, rss_mb


class SmartMacroGaugeApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Smart Macro Loop & Memory Gauge — KERNEL PANIC Guard")
        self.root.configure(bg="#0f172a")
        self.root.geometry("700x555")

        self.dev_fd = None
        self.running = False
        self.sim_state = 1000.0
        self.sim_prev = 1000.0
        self.sim_lag = 1000.0
        self.sim_int = 0.0
        self.sim_tick = 0
        self.panic_flag = False
        self.auto_stopped_latched = False

        self.hist = collections.deque(maxlen=128)
        self.periodic_runaway = False

        self.auto_stop_var = tk.BooleanVar(value=True)
        self.threshold_var = tk.DoubleVar(value=75.0)

        self._open_device()
        self._build_ui()
        self._schedule_tick()

    def _open_device(self):
        try:
            self.dev_fd = os.open(DEV_PATH, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            self.dev_fd = None

    def _send_cmd(self, cmd: str):
        if self.dev_fd is not None:
            try:
                os.write(self.dev_fd, (cmd + "\n").encode("ascii"))
            except OSError:
                pass

    def _build_ui(self):
        top = tk.Frame(self.root, bg="#0f172a")
        top.pack(fill=tk.X, padx=16, pady=(12, 6))

        self.status_lbl = tk.Label(
            top,
            text="MODE: /dev/macro_loop" if self.dev_fd is not None else "MODE: LOCAL SIM (no /dev/macro_loop)",
            fg="#38bdf8",
            bg="#0f172a",
            font=("Consolas", 10, "bold"),
        )
        self.status_lbl.pack(side=tk.LEFT)

        self.panic_lbl = tk.Label(
            top,
            text="SAFE",
            fg="#22c55e",
            bg="#0f172a",
            font=("Consolas", 11, "bold"),
        )
        self.panic_lbl.pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(
            self.root, width=660, height=295, bg="#1e293b", highlightthickness=0
        )
        self.canvas.pack(padx=16, pady=6)

        # Optional pre-panic auto-STOP controls
        guard_bar = tk.Frame(self.root, bg="#1e293b", padx=12, pady=8)
        guard_bar.pack(fill=tk.X, padx=16, pady=(2, 6))

        tk.Checkbutton(
            guard_bar,
            text="Auto-STOP before PANIC",
            variable=self.auto_stop_var,
            bg="#1e293b",
            fg="#f8fafc",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#f8fafc",
            font=("Consolas", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 14))

        tk.Label(
            guard_bar,
            text="Threshold (%):",
            bg="#1e293b",
            fg="#cbd5e1",
            font=("Consolas", 10),
        ).pack(side=tk.LEFT)

        tk.Scale(
            guard_bar,
            from_=10.0,
            to=95.0,
            resolution=1.0,
            orient=tk.HORIZONTAL,
            variable=self.threshold_var,
            length=220,
            bg="#1e293b",
            fg="#38bdf8",
            highlightthickness=0,
            troughcolor="#0f172a",
        ).pack(side=tk.LEFT, padx=8)

        self.metrics_lbl = tk.Label(
            self.root,
            text="Total RAM: -- | Proc RSS: -- | Macro Integrator: 0 | Fast Periodic: NO",
            fg="#e2e8f0",
            bg="#0f172a",
            font=("Consolas", 10),
        )
        self.metrics_lbl.pack(pady=(4, 6))

        btns = tk.Frame(self.root, bg="#0f172a")
        btns.pack(pady=6)

        tk.Button(
            btns,
            text="START Fast Loop",
            command=self.on_start,
            bg="#16a34a",
            fg="white",
            font=("Sans", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            btns,
            text="RESET",
            command=self.on_reset,
            bg="#ca8a04",
            fg="white",
            font=("Sans", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            btns,
            text="STOP & EXIT to CLI",
            command=self.on_stop_and_exit,
            bg="#dc2626",
            fg="white",
            font=("Sans", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT, padx=6)

    def on_start(self):
        self.panic_flag = False
        self.auto_stopped_latched = False
        self.running = True
        self._send_cmd("START")

    def on_reset(self):
        self.running = False
        self.panic_flag = False
        self.auto_stopped_latched = False
        self.sim_lag = 1000.0
        self.sim_int = 0.0
        self.sim_prev = 1000.0
        self.sim_tick = 0
        self.hist.clear()
        self._send_cmd("RESET")

    def on_stop_and_exit(self):
        self.running = False
        self._send_cmd("STOP")
        used_mb, total_mb, used_pct, rss_mb = read_memory_stats()
        print(
            f"[STOPPED BY USER] Total RAM Used: {used_mb:.1f}/{total_mb:.1f} MB "
            f"({used_pct:.1f}%) | Viewer RSS: {rss_mb:.1f} MB | "
            f"Integrator: {self.sim_int:.0f} | Panic: {self.panic_flag} | "
            f"AutoStopped: {self.auto_stopped_latched}",
            flush=True,
        )
        if self.dev_fd is not None:
            try:
                os.close(self.dev_fd)
            except OSError:
                pass
        self.root.destroy()

    def _trigger_auto_stop(self, panic_pct: float, thresh_pct: float):
        self.running = False
        self.auto_stopped_latched = True
        self._send_cmd("STOP")
        print(
            f"[AUTO-STOP] Panic risk {panic_pct:.1f}% crossed threshold {thresh_pct:.1f}% "
            f"(integrator={self.sim_int:.0f}) — sent STOP before KERNEL PANIC!",
            flush=True,
        )

    def _poll_kernel_or_sim(self):
        if self.dev_fd is not None:
            try:
                raw = os.read(self.dev_fd, SAMPLE_STRUCT.size)
                if len(raw) == SAMPLE_STRUCT.size:
                    _, state_val, _, loop_run, panic_al = SAMPLE_STRUCT.unpack(raw)
                    self.sim_int = float(state_val)
                    self.running = bool(loop_run)
                    self.panic_flag = bool(panic_al)
            except BlockingIOError:
                pass
            except OSError:
                pass
        elif self.running and not self.panic_flag:
            T = 4.0
            for _ in range(16):
                cur_in = self.sim_state + (self.sim_tick & 63)
                self.sim_lag += (self.sim_lag - cur_in) / T
                self.sim_int += self.sim_lag / T
                _ = (cur_in - self.sim_prev) / T
                self.sim_prev = cur_in
                self.sim_tick += 1
                if abs(self.sim_int) >= PANIC_LIMIT:
                    self.panic_flag = True
                    self.running = False
                    break

        self.hist.append(abs(self.sim_int))
        self._detect_fast_periodic_runaway()

    def _detect_fast_periodic_runaway(self):
        if len(self.hist) < 32:
            self.periodic_runaway = False
            return
        arr = np.fromiter(self.hist, dtype=np.float64)
        diffs = np.diff(arr)
        if np.std(diffs) < 1e-9:
            self.periodic_runaway = False
            return
        centered = diffs - np.mean(diffs)
        spec = np.abs(np.fft.rfft(centered)) ** 2
        med = float(np.median(spec)) + 1e-12
        peak = float(np.max(spec[1:])) if len(spec) > 1 else 0.0
        self.periodic_runaway = bool(self.running and (peak / med) > 6.0 and arr[-1] > arr[0])

    def _draw_gauge(self, used_pct: float, panic_pct: float, thresh_pct: float):
        c = self.canvas
        c.delete("all")

        cx, cy, r = 330, 225, 140
        c.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=0, extent=180, style=tk.ARC, outline="#334155", width=26
        )

        smart_val = max(used_pct, panic_pct)
        clamped = max(0.0, min(100.0, smart_val))
        color = "#22c55e" if clamped < 60 else ("#f59e0b" if clamped < 85 else "#ef4444")

        c.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=180 - (180.0 * clamped / 100.0),
            extent=(180.0 * clamped / 100.0),
            style=tk.ARC, outline=color, width=26
        )

        # Threshold marker tick on gauge arc
        if self.auto_stop_var.get():
            t_ang = math.radians(180.0 - (180.0 * max(0.0, min(100.0, thresh_pct)) / 100.0))
            tx1 = cx + (r - 16) * math.cos(t_ang)
            ty1 = cy - (r - 16) * math.sin(t_ang)
            tx2 = cx + (r + 16) * math.cos(t_ang)
            ty2 = cy - (r + 16) * math.sin(t_ang)
            c.create_line(tx1, ty1, tx2, ty2, fill="#38bdf8", width=3)

        angle_rad = math.radians(180.0 - (180.0 * clamped / 100.0))
        nx = cx + (r - 18) * math.cos(angle_rad)
        ny = cy - (r - 18) * math.sin(angle_rad)
        c.create_line(cx, cy, nx, ny, fill="#f8fafc", width=4)
        c.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, fill="#f8fafc", outline="")

        c.create_text(
            cx, cy - 45,
            text=f"RAM {used_pct:.1f}%  |  PANIC RISK {panic_pct:.1f}%",
            fill="#f8fafc", font=("Consolas", 13, "bold")
        )
        status_msg = (
            f"AUTO-STOPPED AT {thresh_pct:.0f}% THRESHOLD (PRE-PANIC)"
            if self.auto_stopped_latched
            else (
                "FAST PERIODIC RUNAWAY DETECTED — STOP BEFORE PANIC!"
                if self.periodic_runaway
                else "Monitoring fast loop (static storage)"
            )
        )
        c.create_text(
            cx, cy + 28,
            text=status_msg,
            fill="#38bdf8" if self.auto_stopped_latched else ("#fca5a5" if self.periodic_runaway else "#94a3b8"),
            font=("Consolas", 10, "bold")
        )

    def _schedule_tick(self):
        self._poll_kernel_or_sim()
        used_mb, total_mb, used_pct, rss_mb = read_memory_stats()
        panic_pct = min(100.0, (abs(self.sim_int) / PANIC_LIMIT) * 100.0)
        thresh_pct = float(self.threshold_var.get())

        if (
            self.auto_stop_var.get()
            and self.running
            and not self.panic_flag
            and not self.auto_stopped_latched
            and panic_pct >= thresh_pct
        ):
            self._trigger_auto_stop(panic_pct, thresh_pct)

        if self.panic_flag or panic_pct >= 99.0:
            self.panic_lbl.config(text="KERNEL PANIC!", fg="#ef4444")
        elif self.auto_stopped_latched:
            self.panic_lbl.config(text=f"AUTO-STOPPED (>={thresh_pct:.0f}%)", fg="#38bdf8")
        elif self.periodic_runaway:
            self.panic_lbl.config(text="FAST PERIODIC ALERT", fg="#f59e0b")
        else:
            self.panic_lbl.config(text="SAFE", fg="#22c55e")

        self.metrics_lbl.config(
            text=(
                f"Total RAM: {used_mb:.0f}/{total_mb:.0f} MB ({used_pct:.1f}%) | "
                f"Viewer RSS: {rss_mb:.1f} MB | Integrator: {self.sim_int:.0f} | "
                f"Auto-Stop: {'ON @ ' + f'{thresh_pct:.0f}%' if self.auto_stop_var.get() else 'OFF'}"
            )
        )
        self._draw_gauge(used_pct, panic_pct, thresh_pct)
        self.root.after(80, self._schedule_tick)


def main():
    if "--check-only" in sys.argv[1:]:
        u_mb, t_mb, u_pct, r_mb = read_memory_stats()
        print(f"Total RAM Used: {u_mb:.1f}/{t_mb:.1f} MB ({u_pct:.1f}%) | RSS: {r_mb:.1f} MB")
        return

    root = tk.Tk()
    app = SmartMacroGaugeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
