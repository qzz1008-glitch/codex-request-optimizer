param(
    [Parameter(Mandatory = $true)]
    [string]$InputPath,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [Parameter(Mandatory = $true)]
    [ValidateRange(65536, 8388608)]
    [int]$MaxBytes,

    [ValidateRange(256, 4096)]
    [int]$MaxDimension = 1600
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

function Save-Jpeg {
    param(
        [System.Drawing.Bitmap]$Bitmap,
        [string]$Path,
        [System.Drawing.Imaging.ImageCodecInfo]$Codec,
        [long]$Quality
    )

    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Force
    }
    $parameters = [System.Drawing.Imaging.EncoderParameters]::new(1)
    try {
        $parameters.Param[0] = [System.Drawing.Imaging.EncoderParameter]::new(
            [System.Drawing.Imaging.Encoder]::Quality,
            $Quality
        )
        $Bitmap.Save($Path, $Codec, $parameters)
    }
    finally {
        $parameters.Dispose()
    }
}

$resolvedInput = (Resolve-Path -LiteralPath $InputPath).Path
$outputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

$jpegCodec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() |
    Where-Object { $_.MimeType -eq "image/jpeg" } |
    Select-Object -First 1
if ($null -eq $jpegCodec) {
    throw "Windows JPEG encoder is unavailable."
}

$source = [System.Drawing.Image]::FromFile($resolvedInput)
try {
    try {
        if ($source.PropertyIdList -contains 274) {
            $orientation = $source.GetPropertyItem(274).Value[0]
            switch ($orientation) {
                2 { $source.RotateFlip([System.Drawing.RotateFlipType]::RotateNoneFlipX) }
                3 { $source.RotateFlip([System.Drawing.RotateFlipType]::Rotate180FlipNone) }
                4 { $source.RotateFlip([System.Drawing.RotateFlipType]::Rotate180FlipX) }
                5 { $source.RotateFlip([System.Drawing.RotateFlipType]::Rotate90FlipX) }
                6 { $source.RotateFlip([System.Drawing.RotateFlipType]::Rotate90FlipNone) }
                7 { $source.RotateFlip([System.Drawing.RotateFlipType]::Rotate270FlipX) }
                8 { $source.RotateFlip([System.Drawing.RotateFlipType]::Rotate270FlipNone) }
            }
        }
    }
    catch {
        # Orientation metadata is optional; decoding the pixels is sufficient.
    }

    $largestSide = [Math]::Max($source.Width, $source.Height)
    $scale = [Math]::Min(1.0, $MaxDimension / [double]$largestSide)
    $qualities = @(82L, 74L, 66L, 58L, 50L, 42L, 34L)

    for ($sizeAttempt = 0; $sizeAttempt -lt 10; $sizeAttempt++) {
        $width = [Math]::Max(1, [int][Math]::Round($source.Width * $scale))
        $height = [Math]::Max(1, [int][Math]::Round($source.Height * $scale))
        $bitmap = [System.Drawing.Bitmap]::new(
            $width,
            $height,
            [System.Drawing.Imaging.PixelFormat]::Format24bppRgb
        )
        try {
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.Clear([System.Drawing.Color]::White)
                $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
                $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
                $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $graphics.DrawImage($source, 0, 0, $width, $height)
            }
            finally {
                $graphics.Dispose()
            }

            foreach ($quality in $qualities) {
                Save-Jpeg -Bitmap $bitmap -Path $OutputPath -Codec $jpegCodec -Quality $quality
                $length = (Get-Item -LiteralPath $OutputPath).Length
                if ($length -le $MaxBytes) {
                    Write-Output $length
                    exit 0
                }
            }
        }
        finally {
            $bitmap.Dispose()
        }

        $scale *= 0.78
    }
}
finally {
    $source.Dispose()
}

if (Test-Path -LiteralPath $OutputPath) {
    Remove-Item -LiteralPath $OutputPath -Force
}
throw "Unable to compress the image below $MaxBytes bytes."
