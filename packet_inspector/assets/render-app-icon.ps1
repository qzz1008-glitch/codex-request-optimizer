param(
    [string]$InputPath = (Join-Path $PSScriptRoot 'request-optimizer-logo.svg'),
    [string]$OutputPath = (Join-Path $PSScriptRoot 'request-optimizer-logo.png')
)
# Render this small, flat SVG with built-in Windows drawing APIs.
# Support the icon's rects, paths and matrix-transformed groups.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
[xml]$artwork = Get-Content -LiteralPath $InputPath -Raw
$culture = [Globalization.CultureInfo]::InvariantCulture
function Number([string]$value) { return [single]::Parse($value, $culture) }
function RoundedRect($node) {
    $x=Number $node.x; $y=Number $node.y
    $w=Number $node.width; $h=Number $node.height; $r=Number $node.rx
    $diameter=2*$r
    $p=[Drawing.Drawing2D.GraphicsPath]::new()
    $p.AddArc($x,$y,$diameter,$diameter,180,90)
    $p.AddArc(($x+$w-$diameter),$y,$diameter,$diameter,270,90)
    $p.AddArc(($x+$w-$diameter),($y+$h-$diameter),$diameter,$diameter,0,90)
    $p.AddArc($x,($y+$h-$diameter),$diameter,$diameter,90,90)
    $p.CloseFigure()
    return ,$p
}
function Outline([string]$data) {
    $p=[Drawing.Drawing2D.GraphicsPath]::new()
    $point=[Drawing.PointF]::new(0,0)
    foreach($segment in [regex]::Matches($data,'([A-Za-z])([^A-Za-z]*)')) {
        $command=$segment.Groups[1].Value
        $values=@([regex]::Matches($segment.Groups[2].Value,'-?\d+(?:\.\d+)?') | ForEach-Object { Number $_.Value })
        switch($command) {
            'M' { $point=[Drawing.PointF]::new($values[0],$values[1]); $p.StartFigure() }
            'L' {
                $next=[Drawing.PointF]::new($values[0],$values[1])
                $p.AddLine($point,$next); $point=$next
            }
            'C' {
                $next=[Drawing.PointF]::new($values[4],$values[5])
                $p.AddBezier($point,[Drawing.PointF]::new($values[0],$values[1]),[Drawing.PointF]::new($values[2],$values[3]),$next)
                $point=$next
            }
            'Z' { $p.CloseFigure() }
            default { $p.Dispose(); throw "Unsupported icon path command: $command" }
        }
    }
    return ,$p
}
function RenderElements($nodes) {
    foreach($node in $nodes) {
        if($node -isnot [Xml.XmlElement] -or $node.LocalName -eq 'title') { continue }
        $saved=$graphics.Save()
        try {
            if($node.HasAttribute('transform')) {
                $match=[regex]::Match($node.GetAttribute('transform'),'^matrix\(([^)]+)\)$')
                if(-not $match.Success) { throw 'Unsupported icon transform.' }
                $parts=@([regex]::Matches($match.Groups[1].Value,'[-+]?\d+(?:\.\d+)?') | ForEach-Object { Number $_.Value })
                if($parts.Count -ne 6) { throw 'Invalid icon transform matrix.' }
                $matrix=[Drawing.Drawing2D.Matrix]::new($parts[0],$parts[1],$parts[2],$parts[3],$parts[4],$parts[5])
                try { $graphics.MultiplyTransform($matrix) }
                finally { $matrix.Dispose() }
            }
            if($node.LocalName -eq 'g') {
                RenderElements $node.ChildNodes
                continue
            }
            $path=switch($node.LocalName) {
                'rect' { RoundedRect $node }
                'path' { Outline $node.d }
                default { throw "Unsupported icon element: $($node.LocalName)" }
            }
            try {
                $fill=$node.GetAttribute('fill')
                if($fill -ne 'none') {
                    if(-not $fill) { $fill='#000000' }
                    $brush=[Drawing.SolidBrush]::new([Drawing.ColorTranslator]::FromHtml($fill))
                    try { $graphics.FillPath($brush,$path) }
                    finally { $brush.Dispose() }
                }
                $stroke=$node.GetAttribute('stroke')
                if($stroke -and $stroke -ne 'none') {
                    $strokeWidth=if($node.HasAttribute('stroke-width')) { Number $node.GetAttribute('stroke-width') } else { 1 }
                    $pen=[Drawing.Pen]::new([Drawing.ColorTranslator]::FromHtml($stroke),$strokeWidth)
                    try {
                        if($node.GetAttribute('stroke-linecap') -eq 'round') {
                            $pen.StartCap=[Drawing.Drawing2D.LineCap]::Round
                            $pen.EndCap=[Drawing.Drawing2D.LineCap]::Round
                        }
                        if($node.GetAttribute('stroke-linejoin') -eq 'round') { $pen.LineJoin=[Drawing.Drawing2D.LineJoin]::Round }
                        $graphics.DrawPath($pen,$path)
                    } finally { $pen.Dispose() }
                }
            } finally { $path.Dispose() }
        } finally { $graphics.Restore($saved) }
    }
}
$w=[int]$artwork.svg.width; $h=[int]$artwork.svg.height
$viewBox=@([regex]::Matches($artwork.svg.viewBox,'[-+]?\d+(?:\.\d+)?') | ForEach-Object { Number $_.Value })
if($viewBox.Count -ne 4 -or $viewBox[2] -le 0 -or $viewBox[3] -le 0) { throw 'Invalid icon viewBox.' }
$bitmap=[Drawing.Bitmap]::new($w,$h,[Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics=[Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.Clear([Drawing.Color]::Transparent)
    $graphics.SmoothingMode=[Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.PixelOffsetMode=[Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.ScaleTransform(($w/$viewBox[2]),($h/$viewBox[3]))
    $graphics.TranslateTransform((-$viewBox[0]),(-$viewBox[1]))
    RenderElements $artwork.svg.ChildNodes
    $bitmap.Save([IO.Path]::GetFullPath($OutputPath),[Drawing.Imaging.ImageFormat]::Png)
    [pscustomobject]@{Output=[IO.Path]::GetFullPath($OutputPath);Canvas=@($w,$h);Background='transparent';Palette='indigo, copper, jade'} | ConvertTo-Json -Compress
} finally { $graphics.Dispose(); $bitmap.Dispose() }
