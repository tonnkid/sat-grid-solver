#!/usr/bin/env python3
"""
Parallel SAT cube solver for NONCE 5-SAT balanced random instances.

Strategy: cube-and-conquer across GitHub Actions runners. Each runner solves
its cube slice with a portfolio of engines, each in its own killable process:

  - Cadical195 (CDCL, modern)     via python-sat
  - Glucose4   (CDCL, aggressive) via python-sat
  - Minisat22  (CDCL, classic)    via python-sat
  - Kissat     (CDCL, fastest)    external binary
  - YalSAT     (SLS + Luby restarts on CDCL clauses) external binary
  - probSAT    (SLS, balanced-instance specialist) external binary

Balanced random 5-SAT instances are the hard class here: pure CDCL stalls and
pure SLS can miss. Mixing trajectories is what buys probability, not guarantees.

No network. No keys. Pure computation.
"""
import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# external solvers resolved from env, then common build paths
BIN_CANDIDATES = {
    "yalsat": [os.environ.get("YALSAT_BIN", ""), "/home/ubuntu/tools/yalsat/yalsat",
               shutil.which("yalsat") or ""],
    "kissat": [os.environ.get("KISSAT_BIN", ""), "/home/ubuntu/tools/kissat/build/kissat",
               shutil.which("kissat") or ""],
    "probsab": [os.environ.get("PROBSAB_BIN", ""), "/home/ubuntu/nonce_mint/probsab",
                shutil.which("probsab") or ""],
}


def find_bin(name):
    for c in BIN_CANDIDATES.get(name, []):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def read_dimacs(path):
    nv = 0
    clauses = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("c"):
                continue
            if line.startswith("p"):
                parts = line.split()
                nv = int(parts[2])
                continue
            lits = [int(x) for x in line.split() if x.strip()]
            if lits and lits[-1] == 0:
                lits = lits[:-1]
            if lits:
                clauses.append(lits)
    return nv, clauses


def verify(clauses, bits):
    for c in clauses:
        ok = False
        for lit in c:
            v = abs(lit)
            if (bits[v - 1] == "1") == (lit > 0):
                ok = True
                break
        if not ok:
            return False
    return True


def make_cubes(nv, depth, seed=0, strategy="prefix"):
    """Deterministic cube list.

    prefix: cubes over the first `depth` variables in natural order, so every
            runner reproduces the exact same partition regardless of its slice.
    shuffled: same but over a seeded shuffle (diversifies the split).
    """
    if strategy == "shuffled":
        rnd = random.Random(seed)
        order = list(range(1, nv + 1))
        rnd.shuffle(order)
    else:
        order = list(range(1, nv + 1))
    picked = order[:depth]
    cubes = []
    for mask in range(1 << depth):
        cubes.append([v if (mask >> i) & 1 else -v for i, v in enumerate(picked)])
    return cubes


# --------------------------------------------------------------------------- #
# One solver attempt in a killable subprocess
# --------------------------------------------------------------------------- #

def _run_pysat(name, cnf_path, cube, budget, outbox):
    """Child entry point for python-sat engines. Writes bits into outbox dict."""
    try:
        from pysat.solvers import Cadical195, Glucose4, Minisat22
    except ImportError:
        outbox["err"] = "no-pysat"
        return
    cls = {"cadical195": Cadical195, "glucose4": Glucose4, "minisat22": Minisat22}[name]
    try:
        nv, clauses = read_dimacs(cnf_path)
        with cls(bootstrap_with=clauses) as s:
            st = s.solve_limited(assumptions=cube, expect_interrupt=False)
            if st:
                model = s.get_model()
                nv_real = max(abs(x) for x in model)
                bits = "".join("1" if (i + 1) in
                               {abs(v) for v in model if v > 0} else "0"
                               for i in range(nv_real))
                outbox["bits"] = bits
            elif st is False:
                outbox["unsat"] = True
    except Exception as e:
        outbox["err"] = f"{type(e).__name__}: {e}"


def _pysat_worker(name, cnf_path, cube, budget, q):
    box = {}
    _run_pysat(name, cnf_path, cube, budget, box)
    q.put(box)


def solve_pysat(name, clauses, nv, cube, budget):
    """python-sat engine in a separate process we can hard-kill."""
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_pysat_worker, args=(name, _WORK_CNF[0], cube, budget, q))
    p.start()
    p.join(budget)
    if p.is_alive():
        p.terminate()
        p.join(3)
        if p.is_alive():
            p.kill()
        return None, name, budget
    try:
        box = q.get_nowait()
    except Exception:
        return None, name, budget
    if box.get("unsat"):
        return False, name, budget
    if "bits" in box and verify(clauses, box["bits"]):
        return box["bits"], name, budget
    return None, name, budget


# --------------------------------------------------------------------------- #
# External binary engines
# --------------------------------------------------------------------------- #

def _parse_kissat(rc, out):
    if rc == 10:
        for line in out.splitlines():
            if line.startswith("v "):
                lits = [int(x) for x in line[2:].split() if x not in ("0",)]
                return lits
        # kissat in --quiet still prints v lines; collect across all v lines
        lits = []
        for line in out.splitlines():
            if line.startswith("v "):
                lits.extend(int(x) for x in line[2:].split() if x not in ("0",))
        return lits if lits else None
    return None


def solve_external(name, cnf_path, cube, budget, extra_cube_file=None):
    """Run an external binary. Returns (bits|False|None, name, elapsed)."""
    binpath = find_bin(name)
    if not binpath:
        return None, name + ":", 0.0

    tmpdir = tempfile.mkdtemp(prefix=f"{name}_")
    try:
        target = cnf_path
        if cube:
            # materialise cube as extra unit clauses
            target = os.path.join(tmpdir, "cube.cnf")
            with open(cnf_path) as src, open(target, "w") as dst:
                first = True
                for line in src:
                    if first and line.startswith("p"):
                        parts = line.split()
                        dst.write(f"p cnf {parts[2]} {int(parts[3]) + len(cube)}\n")
                        first = False
                    else:
                        dst.write(line)
                for lit in cube:
                    dst.write(f"{lit} 0\n")

        if name == "kissat":
            # kissat has no -t; wall clock is --time=<seconds>
            cmd = [binpath, "-q", "--time=%d" % max(1, budget), target]
        elif name == "yalsat":
            # yalsat has no time flag at all; rely on subprocess timeout below
            cmd = [binpath, target]
        elif name == "probsab":
            # probsab <cnf> <seed> <beta> <maxFlips>
            cmd = [binpath, target, str(random.randint(1, 10 ** 6)), "2.3",
                   str(budget * 2_000_000)]
        else:
            return None, name, 0.0

        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=budget + 15)
        except subprocess.TimeoutExpired:
            return None, name, round(time.time() - t0, 1)
        dt = round(time.time() - t0, 1)
        out = r.stdout or ""

        if name == "kissat":
            lits = _parse_kissat(r.returncode, out)
            if lits:
                return _lits_to_bits(lits), name, dt
            if r.returncode == 20:
                return False, name, dt
            return None, name, dt

        if name == "yalsat":
            # yalsat exit code is unreliable; parse stdout for the verdict.
            if "s SATISFIABLE" in out:
                lits = _parse_kissat(10, out)
                if lits:
                    return _lits_to_bits(lits), name, dt
            if "s UNSATISFIABLE" in out:
                return False, name, dt
            return None, name, dt

        if name == "probsab":
            model = out.strip()
            if model and set(model) <= {"0", "1"}:
                return model, name, dt
            return None, name, dt

        return None, name, dt
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _lits_to_bits(lits):
    nv = max(abs(x) for x in lits)
    pos = {abs(x) for x in lits if x > 0}
    return "".join("1" if (i + 1) in pos else "0" for i in range(nv))


_WORK_CNF = [None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cnf", required=True)
    ap.add_argument("--cube-start", type=int, default=0)
    ap.add_argument("--cube-count", type=int, default=0, help="0 = all remaining")
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--budget", type=int, default=600, help="seconds for this slice")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--strategy", default="prefix", choices=["prefix", "shuffled"])
    ap.add_argument("--engines", default="cdcl,sls",
                    help="cdcl | sls | cdcl+sls | all")
    ap.add_argument("--out", default="result.json")
    args = ap.parse_args()

    t_start = time.time()
    nv, clauses = read_dimacs(args.cnf)
    _WORK_CNF[0] = args.cnf
    print(f"[init] vars={nv} clauses={len(clauses)} depth={args.depth}", flush=True)

    cubes = make_cubes(nv, args.depth, args.seed, args.strategy)
    total = len(cubes)
    end = total if args.cube_count == 0 else min(total, args.cube_start + args.cube_count)
    my_cubes = cubes[args.cube_start:end]
    print(f"[plan] total={total} mine={len(my_cubes)} range=[{args.cube_start},{end})", flush=True)

    cdcl = [n for n in ("cadical195", "glucose4", "minisat22")
            if args.engines in ("all", "cdcl", "cdcl+sls")]
    kissat = [n for n in ("kissat",) if args.engines in ("all", "cdcl", "cdcl+sls")
              and find_bin("kissat")]
    sls = [n for n in ("yalsat", "probsab") if args.engines in ("all", "sls", "cdcl+sls")
           and find_bin(n)]
    print(f"[engines] cdcl={cdcl} kissat={kissat} sls={sls}", flush=True)

    result = {
        "kind": "no-solution",
        "vars": nv,
        "clauses": len(clauses),
        "depth": args.depth,
        "cube_start": args.cube_start,
        "cube_count": len(my_cubes),
        "cubes_total": total,
        "unsat_cubes": 0,
        "elapsed": 0,
    }

    for idx, cube in enumerate(my_cubes):
        gidx = args.cube_start + idx
        remain = len(my_cubes) - idx
        slice_budget = max(20, int((args.budget - (time.time() - t_start)) / max(1, remain)))
        print(f"[cube {gidx}] budget={slice_budget}s", flush=True)

        # Order matters: strongest CDCL first, SLS as the trajectory changeup
        order = []
        for n in kissat:
            order.append(("ext", n))
        for n in cdcl:
            order.append(("pysat", n))
        for n in sls:
            order.append(("ext", n))
        if not order:
            print("  no engines available", flush=True)
            break

        per = max(8, slice_budget // len(order))
        for kind, name in order:
            if time.time() - t_start > args.budget:
                break
            if kind == "pysat":
                bits, used, dt = solve_pysat(name, clauses, nv, cube, per)
            else:
                bits, used, dt = solve_external(name, args.cnf, cube, per)

            if bits is False:
                result["unsat_cubes"] += 1
                print(f"  {used}: UNSAT ({dt}s) -> cube closed", flush=True)
                break
            if bits and verify(clauses, bits):
                result.update({
                    "kind": "SAT",
                    "assignment": bits,
                    "solver": used,
                    "cube": cube,
                    "cube_index": gidx,
                    "elapsed": round(time.time() - t_start, 2),
                })
                json.dump(result, open(args.out, "w"), indent=2)
                print(f"  {used}: *** SAT *** verified ({dt}s)", flush=True)
                print(json.dumps(result))
                return 0
            print(f"  {used}: nothing ({dt}s)", flush=True)

    result["elapsed"] = round(time.time() - t_start, 2)
    json.dump(result, open(args.out, "w"), indent=2)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
