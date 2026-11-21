import collections
import math
import os
import struct
import sys
import tkinter as tk
import numpy as np

DEV_PATH = "/dev/gauge_macro"
SAMPLE_FMT = struct.Struct("<qQQII")  # ts_ns, used_ram_kb, total_ram_kb, loop_running, panic_alarm


def read_mem_stats(page_size: int, mb_div: float):
    total_kb = 1
    avail_kb = 1
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("MemTotal:"):
                    total_kb = max(int(line.split()[1]), 1)
                    if avail_kb > 1:
                        break
                elif line.startswith("MemAvailable:"):
                    avail_kb = max(int(line.split()[1]), 1)
                    if total_kb > 1:
                        break
    except OSError:
        pass

    used_mb = (total_kb - avail_kb) / 1024.0
    total_mb = total_kb / 1024.0
    used_pct = max(0.0, min(100.0, (used_mb / total_mb) * 100.0))

    rss_mb = 0.0
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as fp:
            pages = int(fp.read().split()[1])
            rss_mb = (pages * page_size) / mb_div
    except OSError:
        pass

    return used_mb, total_mb, used_pct, rss_mb


class SmartRamGaugeGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RAM Usage Gauge — Fast Loop Controller")
        self.root.configure(bg="#0f172a")
        self.root.geometry("700x575")

        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.mb_div = 1024.0 * 1024.0

        self.fd = None
        self.running = False
        self.panic_flag = False
        self.auto_stopped = False
        self.tick_count = 0
        self.ram_hist = collections.deque(maxlen=128)
        self.periodic_ram_alert = False

        self.auto_stop_enabled = tk.BooleanVar(value=True)
        self.stop_threshold_pct = tk.DoubleVar(value=85.0)

        self._open_dev()
        self._ui()
        self._tick()

    def _open_dev(self):
        try:
            self.fd = os.open(DEV_PATH, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            self.fd = None

    def _send(self, cmd: str):
        if self.fd is not None:
            try:
                os.write(self.fd, (cmd + "\n").encode("ascii"))
            except OSError:
                pass

    def _ui(self):
        top = tk.Frame(self.root, bg="#0f172a")
        top.pack(fill=tk.X, padx=16, pady=(12, 6))

        self.mode_lbl = tk.Label(
            top,
            text="DEV: /dev/gauge_macro" if self.fd is not None else "DEV: /dev/gauge_macro (not loaded)",
            fg="#38bdf8",
            bg="#0f172a",
            font=("Consolas", 10, "bold"),
        )
        self.mode_lbl.pack(side=tk.LEFT)

        self.state_lbl = tk.Label(
            top,
            text="RAM NORMAL",
            fg="#22c55e",
            bg="#0f172a",
            font=("Consolas", 11, "bold"),
        )
        self.state_lbl.pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(
            self.root, width=660, height=320, bg="#1e293b", highlightthickness=0
        )
        self.canvas.pack(padx=16, pady=6)

        guard = tk.Frame(self.root, bg="#1e293b", padx=12, pady=8)
        guard.pack(fill=tk.X, padx=16, pady=(2, 6))

        tk.Checkbutton(
            guard,
            text="Auto-STOP before PANIC",
            variable=self.auto_stop_enabled,
            bg="#1e293b",
            fg="#f8fafc",
            selectcolor="#0f172a",
            activebackground="#1e293b",
            activeforeground="#f8fafc",
            font=("Consolas", 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 14))

        tk.Label(
            guard,
            text="RAM Threshold (%):",
            bg="#1e293b",
            fg="#cbd5e1",
            font=("Consolas", 10),
        ).pack(side=tk.LEFT)

        tk.Scale(
            guard,
            from_=10.0,
            to=95.0,
            resolution=1.0,
            orient=tk.HORIZONTAL,
            variable=self.stop_threshold_pct,
            length=220,
            bg="#1e293b",
            fg="#38bdf8",
            highlightthickness=0,
            troughcolor="#0f172a",
        ).pack(side=tk.LEFT, padx=8)

        self.info_lbl = tk.Label(
            self.root,
            text="Total RAM: -- | Viewer RSS: -- | Periodic RAM Growth: NO",
            fg="#e2e8f0",
            bg="#0f172a",
            font=("Consolas", 10),
        )
        self.info_lbl.pack(pady=(4, 6))

        bar = tk.Frame(self.root, bg="#0f172a")
        bar.pack(pady=6)

        tk.Button(
            bar,
            text="START Fast Loop",
            command=self.start_loop,
            bg="#16a34a",
            fg="white",
            font=("Sans", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            bar,
            text="RESET",
            command=self.reset_loop,
            bg="#ca8a04",
            fg="white",
            font=("Sans", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT, padx=6)

        tk.Button(
            bar,
            text="STOP & EXIT to CLI",
            command=self.stop_and_exit,
            bg="#dc2626",
            fg="white",
            font=("Sans", 10, "bold"),
            padx=14,
            pady=6,
        ).pack(side=tk.LEFT, padx=6)

    def start_loop(self):
        self.panic_flag = False
        self.auto_stopped = False
        self.running = True
        self._send("START")

    def reset_loop(self):
        self.running = False
        self.panic_flag = False
        self.auto_stopped = False
        self.ram_hist.clear()
        self._send("RESET")

    def stop_and_exit(self):
        self.running = False
        self._send("STOP")
        used_mb, total_mb, used_pct, rss_mb = read_mem_stats(self.page_size, self.mb_div)
        print(
            f"[STOPPED] Total RAM Used: {used_mb:.1f}/{total_mb:.1f} MB "
            f"({used_pct:.1f}%) | Viewer RSS: {rss_mb:.1f} MB | "
            f"AutoStopped: {self.auto_stopped}",
            flush=True,
        )
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.root.destroy()

    def _poll_device(self):
        if self.fd is None:
            return
        for _ in range(16):
            try:
                raw = os.read(self.fd, SAMPLE_FMT.size)
                if len(raw) != SAMPLE_FMT.size:
                    break
                _, _, _, loop_run, panic_al = SAMPLE_FMT.unpack(raw)
                self.running = bool(loop_run)
                self.panic_flag = bool(panic_al)
            except BlockingIOError:
                break
            except OSError:
                break

    def _update_ram_trend(self, used_mb: float):
        self.ram_hist.append(used_mb)
        if (self.tick_count & 3) != 0:
            return
        if len(self.ram_hist) < 32:
            self.periodic_ram_alert = False
            return
        arr = np.fromiter(self.ram_hist, dtype=np.float64)
        diffs = np.diff(arr)
        if np.std(diffs) < 1e-6:
            self.periodic_ram_alert = False
            return
        centered = diffs - np.mean(diffs)
        spec = np.abs(np.fft.rfft(centered)) ** 2
        med = float(np.median(spec)) + 1e-12
        peak = float(np.max(spec[1:])) if len(spec) > 1 else 0.0
        self.periodic_ram_alert = bool(self.running and (peak / med) > 6.0 and arr[-1] > arr[0])

    def _draw(self, used_mb: float, total_mb: float, used_pct: float, thresh_pct: float):
        c = self.canvas
        c.delete("all")
        cx, cy, r = 330, 255, 130

        c.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=0, extent=180, style=tk.ARC, outline="#334155", width=26
        )

        clamped = max(0.0, min(100.0, used_pct))
        col = "#22c55e" if clamped < 65 else ("#f59e0b" if clamped < 85 else "#ef4444")

        c.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=180 - (180.0 * clamped / 100.0),
            extent=(180.0 * clamped / 100.0),
            style=tk.ARC, outline=col, width=26
        )

        if self.auto_stop_enabled.get():
            t_ang = math.radians(180.0 - (180.0 * max(0.0, min(100.0, thresh_pct)) / 100.0))
            c.create_line(
                cx + (r - 16) * math.cos(t_ang),
                cy - (r - 16) * math.sin(t_ang),
                cx + (r + 16) * math.cos(t_ang),
                cy - (r + 16) * math.sin(t_ang),
                fill="#38bdf8",
                width=3,
            )

        ang = math.radians(180.0 - (180.0 * clamped / 100.0))
        c.create_line(
            cx, cy,
            cx + (r - 18) * math.cos(ang),
            cy - (r - 18) * math.sin(ang),
            fill="#f8fafc",
            width=4,
        )
        c.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, fill="#f8fafc", outline="")

        c.create_text(
            cx, cy - 180,
            text=f"RAM USED: {used_mb:.0f} / {total_mb:.0f} MB ({used_pct:.1f}%)",
            fill="#f8fafc", font=("Consolas", 13, "bold")
        )
        msg = (
            f"AUTO-STOPPED BEFORE PANIC AT {thresh_pct:.0f}% RAM"
            if self.auto_stopped
            else (
                "PERIODIC RAM CONSUMPTION GROWTH — STOP BEFORE PANIC!"
                if self.periodic_ram_alert
                else "Monitoring system RAM consumption"
            )
        )
        c.create_text(
            cx, cy + 28,
            text=msg,
            fill="#38bdf8" if self.auto_stopped else ("#fca5a5" if self.periodic_ram_alert else "#94a3b8"),
            font=("Consolas", 10, "bold")
        )

    def _tick(self):
        self.tick_count += 1
        self._poll_device()
        used_mb, total_mb, used_pct, rss_mb = read_mem_stats(self.page_size, self.mb_div)
        self._update_ram_trend(used_mb)
        thresh_pct = float(self.stop_threshold_pct.get())

        if (
            self.auto_stop_enabled.get()
            and self.running
            and not self.panic_flag
            and not self.auto_stopped
            and used_pct >= thresh_pct
        ):
            self.running = False
            self.auto_stopped = True
            self._send("STOP")
            print(
                f"[AUTO-STOP] RAM usage {used_pct:.1f}% >= {thresh_pct:.1f}% — STOP sent before KERNEL PANIC!",
                flush=True,
            )

        if self.panic_flag or used_pct >= 98.0:
            self.state_lbl.config(text="KERNEL PANIC!", fg="#ef4444")
        elif self.auto_stopped:
            self.state_lbl.config(text=f"AUTO-STOPPED (>={thresh_pct:.0f}%)", fg="#38bdf8")
        elif self.periodic_ram_alert:
            self.state_lbl.config(text="RAM GROWTH ALERT", fg="#f59e0b")
        else:
            self.state_lbl.config(text="RAM NORMAL", fg="#22c55e")

        self.info_lbl.config(
            text=(
                f"Total RAM: {used_mb:.0f}/{total_mb:.0f} MB ({used_pct:.1f}%) | "
                f"Viewer RSS: {rss_mb:.1f} MB | Periodic RAM: {'YES' if self.periodic_ram_alert else 'NO'}"
            )
        )
        self._draw(used_mb, total_mb, used_pct, thresh_pct)
        self.root.after(2, self._tick)


def main():
    if "--check-only" in sys.argv[1:]:
        u_mb, t_mb, u_pct, r_mb = read_mem_stats(os.sysconf("SC_PAGE_SIZE"), 1024.0 * 1024.0)
        print(f"Total RAM Used: {u_mb:.1f}/{t_mb:.1f} MB ({u_pct:.1f}%) | Viewer RSS: {r_mb:.1f} MB")
        return

    root = tk.Tk()
    SmartRamGaugeGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
