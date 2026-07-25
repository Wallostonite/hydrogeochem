<#
.SYNOPSIS
    Fetch PHREEQC thermodynamic databases and record their checksums.

.DESCRIPTION
    The Windows equivalent of ops/fetch_databases.sh. The checksums are the point: a
    saturation index is only reproducible together with the exact database that produced
    it, so the file set is pinned and verified rather than downloaded fresh at deploy time.

.EXAMPLE
    .\ops\fetch_databases.ps1
    .\ops\fetch_databases.ps1 -Destination C:\phreeqc\database
#>
param(
    [string]$Destination = "ops/phreeqc-databases"
)

$ErrorActionPreference = "Stop"

# The phreeqpython mirror ships databases version-matched to the IPhreeqc that phreeqpy
# bundles; usgs-coupled/phreeqc3 serves a newer phreeqc.dat/pitzer.dat whose Peng-Robinson
# gas sections the bundled (older) engine cannot parse. So try phreeqpython FIRST and fall
# back to usgs-coupled only for the files phreeqpython does not carry (wateq4f/llnl/minteq).
# water.usgs.gov's old static file path (the original source here) has gone 404 for every file.
$base   = "https://raw.githubusercontent.com/Vitens/phreeqpython/master/phreeqpython/database"
$mirror = "https://raw.githubusercontent.com/usgs-coupled/phreeqc3/master/database"
$files  = @("phreeqc.dat", "wateq4f.dat", "llnl.dat", "pitzer.dat", "minteq.v4.dat")

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

foreach ($file in $files) {
    $target = Join-Path $Destination $file
    if (Test-Path $target) {
        Write-Host "have  $file"
        continue
    }
    Write-Host "fetch $file"
    try {
        Invoke-WebRequest -Uri "$base/$file" -OutFile $target -UseBasicParsing
    } catch {
        try {
            Invoke-WebRequest -Uri "$mirror/$file" -OutFile $target -UseBasicParsing
        } catch {
            Write-Warning "could not fetch $file from either source"
            Remove-Item $target -ErrorAction SilentlyContinue
        }
    }
}

Get-ChildItem -Path $Destination -Filter *.dat |
    Get-FileHash -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash.ToLower())  $($_.Path | Split-Path -Leaf)" } |
    Set-Content (Join-Path $Destination "SHA256SUMS")

Write-Host "checksums written to $(Join-Path $Destination 'SHA256SUMS')"
