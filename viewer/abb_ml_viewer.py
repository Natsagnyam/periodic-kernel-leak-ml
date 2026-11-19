import argparse
import collections
import math
import sys
import numpy as np
from sklearn.ensemble import IsolationForest


def read_mem_available_bytes() -> int:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fp:
            for line in fp:
                if line.startswith("MemAvailable:"):
                    return max(int(line.split()[1]) * 1024, 1)
    except OSError:
        pass
    return 16 * 1024**3


class ABBPeriodicLeakMLViewer:
    def __init__(
        self,
        win_len: int = 128,
        min_hz: float = 20.0,
        max_hz: float = 500.0,
        warmup_wins: int = 20,
        avail_bytes: int | None = None,
    ):
        self.win_len = win_len
        self.min_hz = min_hz
        self.max_hz = max_hz
        self.warmup_wins = warmup_wins

        self.ts_q = collections.deque(maxlen=win_len)
        self.bytes_q = collections.deque(maxlen=win_len)
        self.train_rows = []
        self.model = None
        self.cum_bytes = 0
        self.avail_bytes = (
            read_mem_available_bytes() if avail_bytes is None else max(avail_bytes, 1)
        )

    def _features(self, ts: np.ndarray, b: np.ndarray):
        dt = np.clip(np.diff(ts) / 1e9, 1e-6, None)
        fs = 1.0 / float(np.median(dt))
        span_s = max((ts[-1] - ts[0]) / 1e9, 1e-6)
        bps = float(np.sum(b)) / span_s

        centered = b.astype(np.float64) - np.mean(b)
        n = len(centered)
        window = np.hanning(n)
        win_scale = float(np.sum(window**2)) + 1e-12
        spec = np.fft.rfft(centered * window)
        power = (np.abs(spec) ** 2) / win_scale

        freqs = np.fft.rfftfreq(n, d=1.0 / fs)
        total_p = float(np.sum(power)) + 1e-12
        band = (freqs >= self.min_hz) & (freqs <= self.max_hz)
        band_p = float(np.sum(power[band])) if np.any(band) else 0.0
        fast_ratio = band_p / total_p

        med_p = float(np.median(power)) + 1e-12
        max_band_p = float(np.max(power[band])) if np.any(band) else 0.0
        peak_ratio = max_band_p / med_p

        denom = float(np.dot(centered, centered)) + 1e-12
        max_lag = min(16, n - 1)
        lag_corr = 0.0
        for lag in range(1, max_lag + 1):
            c = float(np.dot(centered[:-lag], centered[lag:])) / denom
            if c > lag_corr:
                lag_corr = c

        vec = np.array(
            [fast_ratio, peak_ratio, lag_corr, math.log1p(bps)],
            dtype=np.float64,
        )
        info = {
            "bps": bps,
            "fast_ratio": fast_ratio,
            "peak_ratio": peak_ratio,
            "lag_corr": lag_corr,
        }
        return vec, info

    def _eta_ram_full(self, bps: float) -> str:
        if bps <= 1e-6:
            return "never"
        secs = self.avail_bytes / bps
        days = secs / 86400.0
        if days >= 1.0:
            return f"{days:.1f}d ({days / 7.0:.2f}w)"
        return f"{secs / 3600.0:.1f}h"

    def feed(self, ts_ns: int, alloc_bytes: int) -> bool:
        self.cum_bytes += alloc_bytes
        self.ts_q.append(ts_ns)
        self.bytes_q.append(alloc_bytes)

        if len(self.ts_q) < self.win_len:
            return False

        ts_arr = np.fromiter(self.ts_q, dtype=np.float64)
        b_arr = np.fromiter(self.bytes_q, dtype=np.float64)
        vec, info = self._features(ts_arr, b_arr)

        if len(self.train_rows) < self.warmup_wins:
            self.train_rows.append(vec)
            if len(self.train_rows) == self.warmup_wins:
                self.model = IsolationForest(
                    n_estimators=100,
                    contamination=0.05,
                    random_state=42,
                )
                self.model.fit(np.vstack(self.train_rows))
            return False

        pred = int(self.model.predict(vec.reshape(1, -1))[0])
        score = float(-self.model.decision_function(vec.reshape(1, -1))[0])

        is_only_fast_periodic = (
            pred == -1
            and info["fast_ratio"] >= 0.40
            and info["peak_ratio"] >= 4.0
            and info["lag_corr"] >= 0.30
            and info["bps"] > 0.0
        )

        if is_only_fast_periodic:
            print(
                f"[PERIODIC-LEAK] leaked={self.cum_bytes}B "
                f"rate={info['bps']:.0f}B/s eta_oom={self._eta_ram_full(info['bps'])} "
                f"fast_ratio={info['fast_ratio']:.2f} peak={info['peak_ratio']:.1f} "
                f"lag_corr={info['lag_corr']:.2f} score={score:.3f}",
                flush=True,
            )
            return True

        return False


def run_self_test() -> int:
    step_ns = 2_000_000
    t = 1_000_000_000

    viewer = ABBPeriodicLeakMLViewer(
        win_len=128,
        min_hz=20.0,
        max_hz=500.0,
        warmup_wins=20,
        avail_bytes=16 * 1024**3,
    )

    for _ in range(25 * 128):
        viewer.feed(t, 64)
        t += step_ns

    non_periodic_alerts = 0

    for _ in range(1024):
        if viewer.feed(t, 64):
            non_periodic_alerts += 1
        t += step_ns

    for idx in range(1500):
        b = 4096 if (idx % 300 == 0) else 64
        if viewer.feed(t, b):
            non_periodic_alerts += 1
        t += step_ns

    for idx in range(1500):
        b = 64 + (idx // 15)
        if viewer.feed(t, b):
            non_periodic_alerts += 1
        t += step_ns

    print(
        f"[SELF-TEST] non_periodic_alerts={non_periodic_alerts} "
        f"(steady baseline + single spikes + slow ramp)",
        flush=True,
    )

    if non_periodic_alerts != 0:
        print("[FAIL] Expected 0 false alerts on non-periodic patterns.", file=sys.stderr)
        return 1

    periodic_alerts = 0
    for idx in range(1024):
        b = 1024 if ((idx % 5) == 0) else 0
        if viewer.feed(t, b):
            periodic_alerts += 1
        t += step_ns

    print(f"[SELF-TEST] fast_periodic_alerts={periodic_alerts}", flush=True)
    if periodic_alerts == 0:
        print("[FAIL] Expected >=1 alert on fast periodic allocation pattern.", file=sys.stderr)
        return 1

    print("[PASS] Zero false alerts on non-periodic traffic; fast periodic leak detected.")
    return 0


def main():
    if "--self-test" in sys.argv[1:]:
        sys.exit(run_self_test())

    viewer = ABBPeriodicLeakMLViewer(win_len=128)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        cols = line.split(",")
        if len(cols) < 2:
            continue
        try:
            ts_ns = int(cols[0])
            alloc_bytes = int(cols[1])
        except ValueError:
            continue
        viewer.feed(ts_ns, alloc_bytes)


if __name__ == "__main__":
    main()
