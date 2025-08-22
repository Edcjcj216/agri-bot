# ================= CONFIG =================
$RenderURL = "https://agri-bot-fc6r.onrender.com/tb-webhook"

function Send-FakePayload {
    $payload = @{
        shared = @{
            hoi = @(
                "cách trồng rau muống",
                "tưới nước cho cà chua",
                "bón phân cho lúa"
            ) | Get-Random
            crop = @("rau muống", "cà chua", "lúa") | Get-Random
            location = "Hồ Chí Minh"
            temperature = [math]::Round((24 + 8 * (Get-Random -Minimum 0 -Maximum 1)), 1)
            humidity = [math]::Round((60 + 30 * (Get-Random -Minimum 0 -Maximum 1)), 1)
            battery = [math]::Round((3.5 + 0.7 * (Get-Random -Minimum 0 -Maximum 1)), 2)
        }
    }

    try {
        $jsonBody = $payload | ConvertTo-Json -Depth 5 -Compress
        $response = Invoke-RestMethod -Uri $RenderURL -Method Post -Body $jsonBody -ContentType "application/json; charset=utf-8"
        Write-Host "✅ Payload sent at $(Get-Date -Format G)"
        Write-Host "AI advice:" $response.advice_text
    }
    catch {
        Write-Warning "❌ Failed to send payload: $_"
    }
}

Write-Host "🚀 Starting auto-send payload every 5 minutes..."
while ($true) {
    Send-FakePayload
    Start-Sleep -Seconds 300   # 5 phút
}
