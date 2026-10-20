<?php
declare(strict_types=1);

// Change only this origin if the temporary Cloudflare tunnel is restarted.
$demoOrigin = 'https://entire-burn-cricket-ashley.trycloudflare.com';

// This file must run before index.php outputs any HTML.
header('Content-Type: text/html; charset=utf-8');
header('Cache-Control: no-store');
header('Permissions-Policy: geolocation=(self "' . $demoOrigin . '"), camera=(), microphone=()');
header('Content-Security-Policy: default-src \'none\'; style-src \'unsafe-inline\'; frame-src ' . $demoOrigin . '; base-uri \'none\'; form-action \'none\'; frame-ancestors \'self\'');
header('Referrer-Policy: strict-origin-when-cross-origin');
?>
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Astana Walks</title>
    <style>
        html, body { margin: 0; width: 100%; height: 100%; overflow: hidden; }
        iframe { position: fixed; inset: 0; width: 100%; height: 100%; border: 0; }
    </style>
</head>
<body>
    <iframe
        title="Astana Walks — помощник по прогулкам"
        src="<?= htmlspecialchars($demoOrigin . '/', ENT_QUOTES, 'UTF-8') ?>"
        allow="geolocation"
    ></iframe>
</body>
</html>
