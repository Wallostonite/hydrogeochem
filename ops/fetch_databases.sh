#!/usr/bin/env bash
# Fetch PHREEQC thermodynamic databases and record their checksums.
#
# The checksums are the point: a saturation index is only reproducible together with
# the exact database that produced it, so the file set is pinned and verified rather
# than downloaded fresh at deploy time.
set -euo pipefail

DEST="${1:-ops/phreeqc-databases}"
# The phreeqpython mirror ships databases version-matched to the IPhreeqc that phreeqpy
# bundles; usgs-coupled/phreeqc3 serves a newer phreeqc.dat/pitzer.dat whose Peng-Robinson
# gas sections the bundled (older) engine cannot parse. So try phreeqpython FIRST and fall
# back to usgs-coupled only for the files phreeqpython does not carry (wateq4f/llnl/minteq).
# water.usgs.gov's old static file path (the original source here) has gone 404 for every file.
BASE="https://raw.githubusercontent.com/Vitens/phreeqpython/master/phreeqpython/database"
MIRROR="https://raw.githubusercontent.com/usgs-coupled/phreeqc3/master/database"
FILES=(phreeqc.dat wateq4f.dat llnl.dat pitzer.dat minteq.v4.dat)

mkdir -p "$DEST"
for file in "${FILES[@]}"; do
  if [[ -f "$DEST/$file" ]]; then
    echo "have  $file"
    continue
  fi
  echo "fetch $file"
  curl -fsSL "$BASE/$file" -o "$DEST/$file" \
    || curl -fsSL "$MIRROR/$file" -o "$DEST/$file" \
    || { echo "could not fetch $file from either source"; rm -f "$DEST/$file"; }
done

( cd "$DEST" && sha256sum ./*.dat > SHA256SUMS )
echo "checksums written to $DEST/SHA256SUMS"
