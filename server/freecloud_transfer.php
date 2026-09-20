<?php
declare(strict_types=1);

$baseDir = __DIR__;
$sessionDir = $baseDir . '/freecloud_sessions';
$metaRoot = $baseDir . '/freecloud_meta';
$transferRoot = $metaRoot . '/transfers';
$configFile = $baseDir . '/config.json';
$filesFile = $metaRoot . '/files.json';
$devicesFile = $metaRoot . '/devices.json';

if (!is_dir($sessionDir)) {
    mkdir($sessionDir, 0775, true);
}
session_save_path($sessionDir);
ini_set('session.cookie_httponly', '1');
ini_set('session.cookie_samesite', 'Strict');
if (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
    ini_set('session.cookie_secure', '1');
}
session_start();

function loadJson(string $path): array
{
    if (!is_file($path)) {
        return [];
    }
    $data = json_decode((string) file_get_contents($path), true);
    return is_array($data) ? $data : [];
}

function saveJson(string $path, array $data): bool
{
    $tmp = $path . '.tmp';
    if (file_put_contents($tmp, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX) === false) {
        return false;
    }
    if (!@rename($tmp, $path)) {
        @unlink($tmp);
        return false;
    }
    @chmod($path, 0600);
    return true;
}

function normalizedPath(string $path): string
{
    $path = str_replace(["\\", "\0"], ['/', ''], trim($path));
    $parts = array_values(array_filter(explode('/', $path), static fn(string $part): bool => $part !== '' && $part !== '.' && $part !== '..'));
    return implode('/', $parts);
}

function validId(string $value): string
{
    $value = strtolower(trim($value));
    return preg_match('/^[a-f0-9]{32}$/', $value) === 1 ? $value : '';
}

function iniBytes(string $value): int
{
    $value = trim($value);
    if ($value === '') {
        return 0;
    }
    $number = (float) $value;
    return match (strtolower(substr($value, -1))) {
        'g' => (int) ($number * 1024 * 1024 * 1024),
        'm' => (int) ($number * 1024 * 1024),
        'k' => (int) ($number * 1024),
        default => (int) $number,
    };
}

function transferLimit(array $config): int
{
    $limits = array_values(array_filter([
        iniBytes((string) ini_get('post_max_size')),
        iniBytes((string) ini_get('upload_max_filesize')),
        max(0, (int) ($config['transfer_max_bytes'] ?? 0)),
    ], static fn(int $value): bool => $value > 0));
    return $limits === [] ? 64 * 1024 * 1024 : min($limits);
}

function expireTransfer(string $transferRoot, string $id, array $record): bool
{
    if ((int) ($record['expires_at'] ?? 0) <= 0 || (int) $record['expires_at'] > time()) {
        return false;
    }
    @unlink($transferRoot . '/' . $id . '.bin');
    @unlink($transferRoot . '/' . $id . '.part');
    @unlink($transferRoot . '/' . $id . '.json');
    return true;
}

function jsonOut(array $data, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($data, JSON_UNESCAPED_SLASHES);
    exit;
}

function failPage(string $message, int $status = 400): never
{
    http_response_code($status);
    $safe = htmlspecialchars($message, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
    echo '<!doctype html><meta charset="utf-8"><title>Transfer error</title><div style="font:16px system-ui;max-width:700px;margin:4rem auto"><h1>Transfer error</h1><p>' . $safe . '</p><p><a href="freecloud.php">Back to FreeCloud</a></p></div>';
    exit;
}

$config = loadJson($configFile);
if ($config === []) {
    failPage('FreeCloud is not set up.', 403);
}
if ((string) ($config['password_hash'] ?? '') !== '' && !(bool) ($_SESSION['auth'] ?? false)) {
    header('Location: freecloud.php');
    exit;
}

if (!is_dir($transferRoot) && !mkdir($transferRoot, 0775, true) && !is_dir($transferRoot)) {
    failPage('Temporary transfer storage is unavailable.', 500);
}
if (!is_file($transferRoot . '/.htaccess')) {
    @file_put_contents($transferRoot . '/.htaccess', "Require all denied\nDeny from all\n", LOCK_EX);
}

$action = (string) ($_GET['action'] ?? 'view');

if ($action === 'create') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        failPage('Transfer creation requires POST.', 405);
    }
    $token = (string) ($_POST['csrf_token'] ?? '');
    if ($token === '' || !hash_equals((string) ($_SESSION['csrf_token'] ?? ''), $token)) {
        failPage('The request token expired. Return to FreeCloud and try again.', 403);
    }
    $path = normalizedPath((string) ($_POST['path'] ?? ''));
    $entries = loadJson($filesFile);
    $entry = isset($entries[$path]) && is_array($entries[$path]) ? $entries[$path] : [];
    if ($entry === [] || (string) ($entry['type'] ?? '') !== 'file' || (bool) ($entry['deleted'] ?? false)) {
        failPage('That file is no longer available.', 404);
    }
    $size = max(0, (int) ($entry['size'] ?? 0));
    $limit = transferLimit($config);
    if ($size > $limit) {
        failPage('This file is too large for this server transfer. Limit: ' . number_format($limit) . ' bytes.', 413);
    }
    $source = null;
    $fallbackSource = null;
    $devices = loadJson($devicesFile);
    foreach (($entry['locations'] ?? []) as $location) {
        if (is_array($location) && (string) ($location['kind'] ?? '') === 'local_node' && (string) ($location['device_id'] ?? '') !== '') {
            $fallbackSource ??= $location;
            $device = (array) ($devices[(string) $location['device_id']] ?? []);
            if ((bool) ($location['available'] ?? true) && (int) ($device['last_seen_at'] ?? 0) >= time() - 180) {
                $source = $location;
                break;
            }
        }
    }
    $source ??= $fallbackSource;
    if ($source === null) {
        failPage('No storage node is registered as a source for this file.', 409);
    }
    $id = bin2hex(random_bytes(16));
    $now = time();
    $hash = strtolower((string) ($entry['hash'] ?? ''));
    if (preg_match('/^[a-f0-9]{64}$/', $hash) !== 1) {
        $hash = '';
    }
    $record = [
        'transfer_id' => $id,
        'kind' => 'download',
        'path' => $path,
        'name' => (string) ($entry['name'] ?? basename($path)),
        'size' => $size,
        'hash' => $hash,
        'source_device_id' => (string) $source['device_id'],
        'source_name' => (string) ($source['name'] ?? 'storage node'),
        'status' => 'queued',
        'created_at' => $now,
        'updated_at' => $now,
        'expires_at' => $now + 86400,
        'max_bytes' => $limit,
    ];
    if (!saveJson($transferRoot . '/' . $id . '.json', $record)) {
        failPage('Could not create the transfer request.', 500);
    }
    header('Location: freecloud_transfer.php?id=' . rawurlencode($id));
    exit;
}

$id = validId((string) ($_GET['id'] ?? ''));
$recordPath = $transferRoot . '/' . $id . '.json';
$record = $id !== '' ? loadJson($recordPath) : [];
if ($record !== [] && expireTransfer($transferRoot, $id, $record)) {
    $record = [];
}

if ($action === 'status') {
    if ($record === []) {
        jsonOut(['ok' => false, 'status' => 'expired', 'error' => 'Transfer expired or was already downloaded.'], 404);
    }
    $devices = loadJson($devicesFile);
    $device = (array) ($devices[(string) ($record['source_device_id'] ?? '')] ?? []);
    $online = (int) ($device['last_seen_at'] ?? 0) >= time() - 180;
    jsonOut([
        'ok' => true,
        'status' => (string) ($record['status'] ?? 'queued'),
        'source_name' => (string) ($record['source_name'] ?? 'storage node'),
        'source_online' => $online,
        'error' => (string) ($record['error'] ?? ''),
    ]);
}

if ($action === 'download') {
    if ($record === [] || (string) ($record['status'] ?? '') !== 'ready') {
        failPage('This transfer is not ready or has expired.', 409);
    }
    $file = $transferRoot . '/' . $id . '.bin';
    if (!is_file($file)) {
        failPage('The temporary transfer file is missing.', 410);
    }
    session_write_close();
    ignore_user_abort(true);
    $name = str_replace(["\r", "\n", '"'], '', (string) ($record['name'] ?? 'download'));
    header('Content-Type: application/octet-stream');
    header('Content-Length: ' . (string) filesize($file));
    header('Content-Disposition: attachment; filename="' . rawurlencode($name) . '"');
    header('X-Content-Type-Options: nosniff');
    $handle = fopen($file, 'rb');
    if ($handle !== false) {
        while (!feof($handle)) {
            echo fread($handle, 1024 * 1024);
            flush();
        }
        fclose($handle);
    }
    @unlink($file);
    @unlink($recordPath);
    exit;
}

if ($record === []) {
    failPage('Transfer expired or was already downloaded.', 404);
}
$sourceName = htmlspecialchars((string) ($record['source_name'] ?? 'storage node'), ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
$fileName = htmlspecialchars((string) ($record['name'] ?? 'file'), ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
session_write_close();
?>
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>FreeCloud transfer</title>
    <style>
        body{font:16px system-ui;background:#f4f7fb;color:#172033;margin:0}.card{max-width:650px;margin:10vh auto;background:white;padding:2rem;border-radius:18px;box-shadow:0 12px 35px #24334d22}.status{padding:1rem;background:#eef4ff;border-radius:10px;margin:1.25rem 0}.error{background:#fff0f0;color:#8b1d1d}a{color:#245ec7}
    </style>
</head>
<body><main class="card">
    <h1><?= $fileName ?></h1>
    <p id="message">Waiting for <?= $sourceName ?>…</p>
    <div class="status" id="detail">The download will start automatically when the file is ready.</div>
    <p><a href="freecloud.php">Back to FreeCloud</a></p>
</main>
<script>
const started = Date.now();
const statusUrl = <?= json_encode('freecloud_transfer.php?action=status&id=' . $id) ?>;
const downloadUrl = <?= json_encode('freecloud_transfer.php?action=download&id=' . $id) ?>;
async function poll() {
    const message = document.getElementById('message');
    const detail = document.getElementById('detail');
    if (Date.now() - started > 10 * 60 * 1000) {
        message.textContent = 'Transfer timed out.';
        detail.textContent = 'The request remains available for the node, but this page stopped waiting. Reload to check again.';
        detail.classList.add('error');
        return;
    }
    try {
        const response = await fetch(statusUrl, {cache: 'no-store'});
        const data = await response.json();
        if (data.status === 'ready') {
            message.textContent = 'Download ready.';
            detail.textContent = 'Your download is starting…';
            window.location.assign(downloadUrl);
            return;
        }
        if (data.status === 'failed' || data.status === 'expired') {
            message.textContent = data.status === 'failed' ? 'Transfer failed.' : 'Transfer expired.';
            detail.textContent = data.error || 'Please return to FreeCloud and try again.';
            detail.classList.add('error');
            return;
        }
        message.textContent = data.status === 'uploading' ? 'Receiving from ' + data.source_name + '…' : 'Waiting for ' + data.source_name + '…';
        detail.textContent = data.source_online ? 'The source is online. The download will begin automatically.' : 'The source is offline or has not checked in recently.';
    } catch (_) {
        detail.textContent = 'Could not check transfer status. Retrying…';
    }
    setTimeout(poll, 2000);
}
poll();
</script></body></html>
