# Export the monthly "Early_Late Report" attendance workbooks to UTF-8 CSV.
# Uses Excel COM (read-only) so no extra Python dependency is needed for .xls.
param(
    [string]$SourceDir = (Join-Path $PSScriptRoot "..\testdata\Full_year_test"),
    [string]$OutDir = (Join-Path $PSScriptRoot "..\testdata\Full_year_test\raw_csv")
)

$SourceDir = (Resolve-Path $SourceDir).Path
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

$columns = @("Employee Id", "Employee Name", "Email ID", "Date", "First In", "Last Out",
             "Total Hours", "Early Entry", "Late Entry", "Early Exit", "Late Exit", "Net hours", "Shift name")

$excel = New-Object -ComObject Excel.Application
$excel.DisplayAlerts = $false
try {
    foreach ($file in Get-ChildItem $SourceDir -Filter *.xls) {
        $month = [datetime]::ParseExact($file.BaseName, "MMMM yyyy", [Globalization.CultureInfo]::InvariantCulture)
        $wb = $excel.Workbooks.Open($file.FullName, 0, $true)
        try {
            $values = $wb.Sheets.Item("Early_Late Report(Decimal)").UsedRange.Value2
            $rows = $values.GetLength(0)
            $cols = $values.GetLength(1)

            $headerRow = 0
            for ($i = 1; $i -le [Math]::Min(10, $rows); $i++) {
                if ([string]($values[$i, 1]) -eq "Employee Id") { $headerRow = $i; break }
            }
            if ($headerRow -eq 0) { throw "No 'Employee Id' header row in $($file.Name)" }

            $header = @(); for ($j = 1; $j -le $cols; $j++) { $header += [string]($values[$headerRow, $j]) }
            $hasDateColumn = $header -contains "Date"
            # Single-day exports carry the date in the title block ("Date | 01-May-2025") instead of a column.
            $singleDate = if ($hasDateColumn) { $null } else { [string]($values[1, 2]) }

            $records = New-Object System.Collections.Generic.List[object]
            for ($i = $headerRow + 1; $i -le $rows; $i++) {
                if ([string]::IsNullOrWhiteSpace([string]($values[$i, 1]))) { continue }
                $row = [ordered]@{}
                for ($j = 1; $j -le $cols; $j++) { $row[$header[$j - 1]] = [string]($values[$i, $j]) }
                if (-not $hasDateColumn) { $row["Date"] = $singleDate }
                $out = [ordered]@{}
                foreach ($c in $columns) { $out[$c] = $row[$c] }
                $out["Source File"] = $file.Name
                $records.Add([pscustomobject]$out)
            }

            $target = Join-Path $OutDir ($month.ToString("yyyy-MM") + ".csv")
            $records | Export-Csv -Path $target -NoTypeInformation -Encoding UTF8
            "{0,-20} -> {1}  ({2} rows)" -f $file.Name, (Split-Path $target -Leaf), $records.Count
        } finally {
            $wb.Close($false)
        }
    }
} finally {
    $excel.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
}
