"""Minimal Prometheus exposition (counters + histograms) without a client library dependency."""
from __future__ import annotations

import threading
from collections import defaultdict

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32)


def _labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = defaultdict(float)
        self._hists: dict[tuple[str, str], list[float]] = {}
        self._help: dict[str, tuple[str, str]] = {}

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        self._help.setdefault(name, ("counter", name.replace("_", " ")))
        with self._lock:
            self._counters[(name, _labels(labels))] += value

    def observe(self, name: str, seconds: float, labels: dict[str, str] | None = None) -> None:
        self._help.setdefault(name, ("histogram", name.replace("_", " ")))
        key = (name, _labels(labels))
        with self._lock:
            h = self._hists.setdefault(key, [0.0] * (len(_LATENCY_BUCKETS) + 2))  # buckets..., count, sum
            for i, b in enumerate(_LATENCY_BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[-2] += 1
            h[-1] += seconds

    def observe_request(self, method: str, path: str, status: int, seconds: float) -> None:
        self.inc("veritas_http_requests_total", {"method": method, "path": path, "status": str(status)})
        self.observe("veritas_http_request_seconds", seconds, {"path": path})

    def observe_query(self, seconds: float, abstained: bool, usage: dict, mode: str,
                      groundedness: float | None, injection: bool) -> None:
        self.inc("veritas_queries_total", {"mode": mode, "abstained": str(abstained).lower()})
        self.observe("veritas_query_seconds", seconds, {"mode": mode})
        self.inc("veritas_llm_tokens_total", {"direction": "input"}, usage["input_tokens"])
        self.inc("veritas_llm_tokens_total", {"direction": "output"}, usage["output_tokens"])
        self.inc("veritas_llm_cost_usd_total", None, usage["cost_usd"])
        if groundedness is not None and groundedness < 0.5:
            self.inc("veritas_low_groundedness_answers_total")
        if injection:
            self.inc("veritas_prompt_injection_flags_total")

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, (kind, help_text) in sorted(self._help.items()):
                lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {kind}")
                if kind == "counter":
                    lines += [f"{n}{lbl} {v:g}" for (n, lbl), v in self._counters.items() if n == name]
                    continue
                for (n, lbl), h in self._hists.items():
                    if n != name:
                        continue
                    inner = lbl[1:-1] + "," if lbl else ""
                    for b, count in zip(_LATENCY_BUCKETS, h):
                        lines.append(f'{n}_bucket{{{inner}le="{b}"}} {count:g}')
                    lines.append(f'{n}_bucket{{{inner}le="+Inf"}} {h[-2]:g}')
                    lines.append(f"{n}_count{lbl} {h[-2]:g}")
                    lines.append(f"{n}_sum{lbl} {h[-1]:.6f}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
