#!/usr/bin/env bash
# Build Kissat (CDCL) and YalSAT (SLS) inside a GitHub Actions runner.
# Binaries land in ./bin so solve.py can find them via env vars.
set -euo pipefail

mkdir -p bin

# --- Kissat: modern CDCL, usually the strongest pure-CDCL engine -------------
if [ ! -x bin/kissat ]; then
  echo "== building kissat =="
  git clone --depth 1 https://github.com/arminbiere/kissat.git /tmp/kissat
  cd /tmp/kissat
  ./configure --quiet
  make -j"$(nproc)"
  cp build/kissat "$OLDPWD/bin/kissat"
  cd - >/dev/null
fi
bin/kissat --version | head -1

# --- YalSAT: SLS with Luby restarts, different trajectory from probSAT ------
if [ ! -x bin/yalsat ]; then
  echo "== building yalsat =="
  git clone --depth 1 https://github.com/arminbiere/yalsat.git /tmp/yalsat
  cd /tmp/yalsat
  ./configure.sh >/dev/null
  make -j"$(nproc)"
  cp yalsat "$OLDPWD/bin/yalsat"
  cd - >/dev/null
fi
bin/yalsat --version 2>/dev/null | head -2 || true

echo "== built =="
ls -la bin/
