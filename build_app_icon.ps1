param(
    [string]$InputPath = (Join-Path $PSScriptRoot 'packet_inspector\assets\request-optimizer-logo.png'),
    [string]$OutputPath = (Join-Path $PSScriptRoot 'packet_inspector\assets\request-optimizer-logo.ico')
)
# Preserve the artwork while converting it to Windows icon sizes.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$source = [Drawing.Bitmap]::new((Resolve-Path -LiteralPath $InputPath).Path)
$sizes = @(16,20,24,32,40,48,64,128,256)
$frames = @()
try {
    foreach ($size in $sizes) {
        $bitmap = [Drawing.Bitmap]::new($size,$size,[Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        try {
            $graphics.CompositingMode = [Drawing.Drawing2D.CompositingMode]::SourceCopy
            $graphics.CompositingQuality = [Drawing.Drawing2D.CompositingQuality]::HighQuality
            $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.Clear([Drawing.Color]::Transparent)
            $graphics.DrawImage($source,[Drawing.Rectangle]::new(0,0,$size,$size))
            $stream = [IO.MemoryStream]::new()
            try {
                if ($size -eq 256) {
                    $bitmap.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
                } else {
                    $writer = [IO.BinaryWriter]::new($stream,[Text.Encoding]::UTF8,$true)
                    try {
                        $stride = [int]([Math]::Ceiling($size/32.0)*4)
                        $writer.Write([uint32]40)
                        $writer.Write([int32]$size); $writer.Write([int32]($size*2))
                        $writer.Write([uint16]1); $writer.Write([uint16]32)
                        $writer.Write([uint32]0)
                        $writer.Write([uint32]($size*$size*4+$stride*$size))
                        1..4 | ForEach-Object { $writer.Write([uint32]0) }
                        $mask = [byte[]]::new($stride*$size)
                        for ($y=$size-1;$y -ge 0;$y--) {
                            for ($x=0;$x -lt $size;$x++) {
                                $pixel=$bitmap.GetPixel($x,$y)
                                $writer.Write([byte]$pixel.B); $writer.Write([byte]$pixel.G)
                                $writer.Write([byte]$pixel.R); $writer.Write([byte]$pixel.A)
                                if ($pixel.A -eq 0) {
                                    $index=($size-1-$y)*$stride+[int][Math]::Floor($x/8)
                                    $mask[$index]=$mask[$index] -bor (128 -shr ($x%8))
                                }
                            }
                        }
                        $writer.Write($mask)
                    } finally { $writer.Dispose() }
                }
                $frames += [pscustomobject]@{Size=$size;Data=$stream.ToArray()}
            } finally { $stream.Dispose() }
        } finally { $graphics.Dispose(); $bitmap.Dispose() }
    }
    $file=[IO.File]::Create([IO.Path]::GetFullPath($OutputPath))
    $writer=[IO.BinaryWriter]::new($file)
    try {
        $writer.Write([uint16]0); $writer.Write([uint16]1); $writer.Write([uint16]$frames.Count)
        $offset=6+16*$frames.Count
        foreach ($frame in $frames) {
            $dimension=if($frame.Size -eq 256){0}else{$frame.Size}
            $writer.Write([byte]$dimension); $writer.Write([byte]$dimension)
            $writer.Write([byte]0); $writer.Write([byte]0)
            $writer.Write([uint16]1); $writer.Write([uint16]32)
            $writer.Write([uint32]$frame.Data.Length); $writer.Write([uint32]$offset)
            $offset+=$frame.Data.Length
        }
        foreach ($frame in $frames) { $writer.Write([byte[]]$frame.Data) }
    } finally { $writer.Dispose(); $file.Dispose() }
    [pscustomobject]@{Icon=[IO.Path]::GetFullPath($OutputPath);Sizes=$sizes} | ConvertTo-Json -Compress
} finally { $source.Dispose() }
