# sat-grid-solver

Parallel cube-and-conquer SAT solving on GitHub Actions.

## What it does

Splits a boolean satisfiability problem into cubes (partial assignments),
distributes them across up to 20 GitHub Actions runners, and reports the
first verified satisfying assignment.

Each runner has ~4 vCPU. 20 jobs = ~80 cores of CDCL search.

## Usage

1. Drop a DIMACS CNF file into `cnf/` (e.g. `cnf/instance.cnf`)
2. Actions → **SAT Cube Solver** → Run workflow
3. Set `depth` (total cubes = 2^depth), `budget` (seconds per job), `chunk` (cubes/job)

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `cnf` | `instance.cnf` | File inside `cnf/` |
| `depth` | `10` | 1024 cubes total |
| `budget` | `600` | wall-clock seconds per job |
| `chunk` | `8` | cubes handled per job |
| `jobs` | `20` | parallel runners (cap 20) |

## Output

Each job uploads `result-<start>.json`. A SAT result contains:

```json
{
  "kind": "SAT",
  "assignment": "1011001...",
  "solver": "cadical195",
  "cube_index": 417,
  "elapsed": 123.4
}
```

The `assignment` string is one char per variable in order (var 1 first).

## Solvers

`python-sat` with `pblib` + `aiger` extras gives compiled bindings for:
- **CaDiCaL 1.9.5** — modern CDCL, usually strongest
- **Glucose 4** — aggressive clause deletion
- **MiniSat 2.2** — classic baseline

The solver tries all three per cube, first answer wins.

## Notes

- `fail-fast: false` — one slow cube never cancels its siblings
- Results are verified in-process before being reported (a model that fails
  verification is discarded, never uploaded as SAT)
- Free tier: unlimited minutes on public repos
