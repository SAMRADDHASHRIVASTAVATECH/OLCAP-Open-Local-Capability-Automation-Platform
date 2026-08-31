"""
JIT CAPABILITY EXECUTION.

  CAPABILITY REQUESTED -> REGISTRY LOOKUP -> DEPENDENCY RESOLUTION ->
  ACTIVATE REQUIRED BACKEND -> EXECUTE -> OBSERVE -> VERIFY ->
  KEEP WARM OR RELEASE

Heavy components run in isolated subprocess workers that speak newline-delimited
JSON, so releasing a worker actually returns its memory to the OS. Light
components use in-process pooled handles. Both paths support warm pools,
idle timeouts, bounded concurrency, automatic restart and fallback.

The user never starts a backend by hand.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import state
from .config import cfg
from .models import ComponentSpec, HealthState
from .observability import span

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                 # the `olcap` package directory
# The directory that makes `import olcap` / `python -m olcap.core.worker`
# work. Relying on the caller's ambient PYTHONPATH is a real failure mode:
# a worker spawned from a different cwd starts with no way to import itself.
IMPORT_ROOT = ROOT.parent


def _readline_timed(fh, timeout: float):
    """Read one line, or return None if nothing arrives before `timeout`.

    A blocking readline() on a pipe ignores every deadline, which silently
    disabled the JIT timeouts and let a single hung network call stall an
    entire objective forever. A daemon thread does the blocking read so the
    deadline is honoured on Windows too, where select() cannot watch pipes.
    """
    box: Dict[str, Any] = {}

    def _reader() -> None:
        try:
            box["line"] = fh.readline()
        except Exception:
            box["line"] = ""

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(max(0.0, timeout))
    if t.is_alive():
        return None
    return box.get("line") or ""


class JITError(RuntimeError):
    pass


@dataclass
class Worker:
    component: str
    proc: Optional[subprocess.Popen] = None
    last_used: float = field(default_factory=time.time)
    busy: bool = False
    calls: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    mode: str = "subprocess"

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class SubprocessWorker(Worker):
    """Isolated worker: `python -m olcap.core.worker <component>`."""

    def __init__(self, component: str, timeout: float = 120.0):
        super().__init__(component=component, mode="subprocess")
        env = dict(os.environ)
        # Only the directory that CONTAINS the package goes on the path.
        # Putting the package directory itself on sys.path shadows stdlib
        # modules with ours - `import platform` inside a worker resolved to
        # olcap/platform instead of the standard library and crashed every
        # backend that needs it.
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(IMPORT_ROOT), env.get("PYTHONPATH", ""))
            if p and p != str(ROOT))
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "olcap.core.worker", component],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=str(IMPORT_ROOT), text=True, bufsize=1)
        self._lock = threading.Lock()
        self._timeout = timeout
        self._timed_out = False
        self._ready = self._wait_ready()

    def _wait_ready(self, timeout: float = 180.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = ""
                try:
                    err = self.proc.stderr.read()
                except Exception:
                    pass
                raise JITError(f"worker {self.component} exited during startup: {err[:400]}")
            line = _readline_timed(self.proc.stdout,
                                   max(0.05, deadline - time.time()))
            if line is None:            # nothing yet: re-check the deadline
                break
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("event") == "ready":
                return True
            if msg.get("event") == "error":
                raise JITError(f"worker {self.component}: {msg.get('error')}")
        raise JITError(f"worker {self.component} startup timeout")

    def call(self, method: str, params: Dict[str, Any],
             timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self.alive:
            raise JITError(f"worker {self.component} is dead")
        with self._lock:
            req = json.dumps({"method": method, "params": params or {}})
            self.proc.stdin.write(req + "\n")
            self.proc.stdin.flush()
            deadline = time.time() + (timeout or self._timeout)
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                line = _readline_timed(self.proc.stdout, remaining)
                if line is None:        # nothing before the deadline
                    break
                if not line:
                    if self.proc.poll() is not None:
                        raise JITError(f"worker {self.component} died: "
                                       f"{self.proc.stderr.read()[:300]}")
                    time.sleep(0.01)
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("event") == "result":
                    self.calls += 1
                    return msg
                if msg.get("event") == "ready":
                    continue
            # The worker is still blocked inside the call, so it must not go
            # back into the pool: kill it and let the next call respawn.
            self._timed_out = True
            try:
                self.proc.kill()
            except Exception:
                pass
            raise JITError(f"worker {self.component} call timeout ({method})")

    def stop(self) -> None:
        try:
            if self.alive:
                self.proc.stdin.write(json.dumps({"method": "__stop__"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=8)
        except Exception:
            pass
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass


class InProcWorker(Worker):
    """Pooled in-process handle with an initialiser and an idle release."""

    def __init__(self, component: str, init: Callable[[], Any],
                 release: Optional[Callable[[Any], None]] = None):
        super().__init__(component=component, mode="inproc")
        self._init = init
        self._release = release
        self.handle = None
        self._lock = threading.RLock()

    def _ensure(self):
        with self._lock:
            if self.handle is None:
                self.handle = self._init()
                self.started_at = time.time()
            return self.handle

    def call(self, method: str, params: Dict[str, Any], timeout=None) -> Dict[str, Any]:
        h = self._ensure()
        fn = getattr(h, method, None)
        if fn is None:
            raise JITError(f"{self.component} has no method {method}")
        self.calls += 1
        return {"ok": True, "result": fn(**(params or {}))}

    def stop(self) -> None:
        with self._lock:
            if self._release and self.handle is not None:
                try:
                    self._release(self.handle)
                except Exception:
                    pass
            self.handle = None


class JITManager:
    """Warm pools, lazy activation, idle release, bounded concurrency, restart."""

    def __init__(self) -> None:
        self.c = cfg()
        self._pools: Dict[str, List[Worker]] = {}
        self._locks: Dict[str, threading.Semaphore] = {}
        self._init_fns: Dict[str, Callable] = {}
        self._release_fns: Dict[str, Callable] = {}
        self._mutex = threading.RLock()
        self._sweeper: Optional[threading.Thread] = None
        self.stats: Dict[str, Any] = {"activations": 0, "releases": 0,
                                      "reuses": 0, "restarts": 0, "timeouts": 0}

    # ------------------------------------------------------------------ #
    def register_inproc(self, component: str, init: Callable[[], Any],
                        release: Optional[Callable[[Any], None]] = None) -> None:
        self._init_fns[component] = init
        if release:
            self._release_fns[component] = release

    def concurrency(self, comp: ComponentSpec) -> int:
        base = max(1, int(comp.max_concurrency or 1))
        cap = int(self.c.env("MAX_CONCURRENCY", "0") or 0)
        return min(base, cap) if cap else base

    def _lock_for(self, cid: str, n: int) -> threading.Semaphore:
        with self._mutex:
            if cid not in self._locks:
                self._locks[cid] = threading.Semaphore(n)
            return self._locks[cid]

    # ------------------------------------------------------------------ #
    def activate(self, comp: ComponentSpec) -> Worker:
        with self._mutex:
            pool = self._pools.setdefault(comp.id, [])
            for w in list(pool):
                if not w.alive:
                    # Automatic restart: a crashed worker is replaced, not reused.
                    pool.remove(w)
                    self.stats["restarts"] += 1
                    state.emit("jit.dead_worker_removed", "jit",
                               {"component": comp.id, "calls": w.calls})
                    continue
                if not w.busy:
                    w.busy = True
                    w.last_used = time.time()
                    self.stats["reuses"] += 1
                    return w
            if len(pool) >= self.concurrency(comp):
                # wait for a slot
                pass
            w = self._spawn(comp)
            w.busy = True
            pool.append(w)
            self.stats["activations"] += 1
            state.emit("jit.activate", "jit", {"component": comp.id,
                                               "mode": w.mode,
                                               "pool": len(pool)})
            self._ensure_sweeper()
            return w

    def _spawn(self, comp: ComponentSpec) -> Worker:
        if comp.id in self._init_fns:
            return InProcWorker(comp.id, self._init_fns[comp.id],
                                self._release_fns.get(comp.id))
        return SubprocessWorker(comp.id)

    def release(self, comp: ComponentSpec, w: Worker) -> None:
        w.busy = False
        w.last_used = time.time()
        state.emit("jit.release", "jit", {"component": comp.id, "calls": w.calls})

    def restart(self, comp: ComponentSpec, w: Worker) -> Worker:
        with self._mutex:
            try:
                w.stop()
            except Exception:
                pass
            pool = self._pools.get(comp.id, [])
            if w in pool:
                pool.remove(w)
            self.stats["restarts"] += 1
            state.emit("jit.restart", "jit", {"component": comp.id})
            nw = self._spawn(comp)
            nw.busy = True
            pool.append(nw)
            return nw

    # ------------------------------------------------------------------ #
    def invoke(self, comp: ComponentSpec, method: str, params: Dict[str, Any],
               timeout: Optional[float] = None) -> Dict[str, Any]:
        sem = self._lock_for(comp.id, self.concurrency(comp))
        acquired = sem.acquire(timeout=timeout or 120)
        if not acquired:
            self.stats["timeouts"] += 1
            raise JITError(f"no worker slot for {comp.id} within timeout")
        w = self.activate(comp)
        try:
            with span("jit.invoke", "capability",
                      {"component": comp.id, "method": method}) as sp:
                out = w.call(method, params, timeout=timeout)
                sp.set(mode=w.mode, calls=w.calls)
            return out
        except Exception as e:
            w.errors += 1
            state.emit("jit.error", "jit", {"component": comp.id,
                                            "error": str(e)[:300]})
            raise
        finally:
            if getattr(w, "_timed_out", False):
                # hung mid-call: retire it, a fresh worker is spawned next time
                with self._mutex:
                    pool = self._pools.get(comp.id, [])
                    if w in pool:
                        pool.remove(w)
                try:
                    w.stop()
                except Exception:
                    pass
                self.stats["restarts"] += 1
            else:
                self.release(comp, w)
            sem.release()

    # ------------------------------------------------------------------ #
    def sweep_once(self) -> int:
        """Release every idle-expired worker. Returns how many were released."""
        released = 0
        with self._mutex:
            for cid, pool in list(self._pools.items()):
                from .registry import registry
                comp = registry().component(cid)
                ttl = comp.warm_ttl_s if comp else 300
                for w in list(pool):
                    if w.busy:
                        continue
                    if time.time() - w.last_used > ttl:
                        w.stop()
                        pool.remove(w)
                        self.stats["releases"] += 1
                        released += 1
                        state.emit("jit.idle_release", "jit",
                                   {"component": cid, "calls": w.calls})
        return released

    def _sweep(self) -> None:
        while True:
            time.sleep(20)
            try:
                self.sweep_once()
            except Exception:
                pass

    def _ensure_sweeper(self) -> None:
        if self._sweeper and self._sweeper.is_alive():
            return
        self._sweeper = threading.Thread(target=self._sweep, daemon=True)
        self._sweeper.start()

    # ------------------------------------------------------------------ #
    def pool_state(self) -> Dict[str, Any]:
        out = {}
        with self._mutex:
            for cid, pool in self._pools.items():
                out[cid] = [{"mode": w.mode, "busy": w.busy, "calls": w.calls,
                             "errors": w.errors, "alive": w.alive,
                             "idle_s": round(time.time() - w.last_used, 1)}
                            for w in pool]
        return {"pools": out, "stats": self.stats}

    def release_all(self) -> None:
        with self._mutex:
            for cid, pool in list(self._pools.items()):
                for w in pool:
                    try:
                        w.stop()
                    except Exception:
                        pass
                pool.clear()
                del self._pools[cid]          # no empty pool keys left behind
            state.emit("jit.release_all", "jit", {})


_JIT: Optional[JITManager] = None


def jit() -> JITManager:
    global _JIT
    if _JIT is None:
        _JIT = JITManager()
    return _JIT
