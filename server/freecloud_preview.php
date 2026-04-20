<?php
declare(strict_types=1);

$baseDir = __DIR__;
$sessionDir = $baseDir . '/freecloud_sessions';
if (!is_dir($sessionDir)) {
    mkdir($sessionDir, 0775, true);
}
session_save_path($sessionDir);
session_start();

$storageRoot = realpath($baseDir . '/freecloud_files');
$configFile = $baseDir . '/config.json';

function loadConfig(string $configFile): ?array
{
    if (!is_file($configFile)) {
        return null;
    }

    $raw = file_get_contents($configFile);
    $config = json_decode(is_string($raw) ? $raw : '', true);
    return is_array($config) ? $config : null;
}

function authRequired(?array $config): bool
{
    return is_array($config) && (string) ($config['password_hash'] ?? '') !== '';
}

function requireAuth(?array $config): void
{
    if ($config === null) {
        http_response_code(403);
        exit('FreeCloud is not set up.');
    }

    if (authRequired($config) && !($_SESSION['auth'] ?? false)) {
        http_response_code(403);
        exit('Login required.');
    }
}

function normalizeRelativePath(string $path): string
{
    $path = str_replace(["\\", "\0"], ['/', ''], trim($path));
    $path = preg_replace('/[[:cntrl:]]+/', '', $path) ?? '';
    $parts = array_values(array_filter(explode('/', $path), static function (string $part): bool {
        return $part !== '' && $part !== '.' && $part !== '..';
    }));

    return implode('/', array_map(static fn(string $part): string => trim($part), $parts));
}

function isWithinRoot(string $path, string $root): bool
{
    $path = rtrim(str_replace('\\', '/', $path), '/');
    $root = rtrim(str_replace('\\', '/', $root), '/');
    return $path === $root || str_starts_with($path, $root . '/');
}

function resolveTarget(string $requestedPath, ?string $storageRoot): ?string
{
    if (!is_string($storageRoot) || $storageRoot === '') {
        return null;
    }

    $relativePath = normalizeRelativePath($requestedPath);
    if ($relativePath === '') {
        return null;
    }

    $target = realpath($storageRoot . DIRECTORY_SEPARATOR . $relativePath);
    if ($target === false || !isWithinRoot($target, $storageRoot) || !is_file($target)) {
        return null;
    }

    return $target;
}

function parseRangeHeader(string $header, int $fileSize): ?array
{
    if (!preg_match('/bytes=(\d*)-(\d*)/', $header, $matches)) {
        return null;
    }

    $startText = $matches[1];
    $endText = $matches[2];
    if ($startText === '' && $endText === '') {
        return null;
    }

    if ($startText === '') {
        $suffixLength = (int) $endText;
        if ($suffixLength <= 0) {
            return null;
        }
        return [max(0, $fileSize - $suffixLength), $fileSize - 1];
    }

    $start = (int) $startText;
    $end = $endText === '' ? $fileSize - 1 : (int) $endText;
    if ($start < 0 || $end < $start || $start >= $fileSize) {
        return null;
    }

    return [$start, min($end, $fileSize - 1)];
}

function streamFileRange(string $path, int $start, int $end): void
{
    $handle = fopen($path, 'rb');
    if ($handle === false) {
        http_response_code(500);
        exit('Could not open file.');
    }

    $remaining = $end - $start + 1;
    fseek($handle, $start);

    while ($remaining > 0 && !feof($handle)) {
        $buffer = fread($handle, min(1024 * 1024, $remaining));
        if ($buffer === false) {
            break;
        }

        echo $buffer;
        flush();
        $remaining -= strlen($buffer);
    }

    fclose($handle);
}

$config = loadConfig($configFile);
requireAuth($config);

$requestedPath = isset($_GET['path']) ? (string) $_GET['path'] : '';
$target = resolveTarget($requestedPath, $storageRoot);
if ($target === null) {
    http_response_code(404);
    exit('File not found.');
}

$mimeType = mime_content_type($target);
if (!is_string($mimeType) || $mimeType === '') {
    $mimeType = 'application/octet-stream';
}

$fileSize = filesize($target);
if ($fileSize === false) {
    http_response_code(500);
    exit('Could not read file size.');
}

header('Content-Type: ' . $mimeType);
header('Content-Disposition: inline; filename="' . rawurlencode(basename($target)) . '"');
header('X-Content-Type-Options: nosniff');
header('Accept-Ranges: bytes');

$rangeHeader = isset($_SERVER['HTTP_RANGE']) ? trim((string) $_SERVER['HTTP_RANGE']) : '';
if ($rangeHeader !== '') {
    $range = parseRangeHeader($rangeHeader, $fileSize);
    if ($range === null) {
        http_response_code(416);
        header('Content-Range: bytes */' . $fileSize);
        exit;
    }

    [$start, $end] = $range;
    http_response_code(206);
    header('Content-Length: ' . (string) ($end - $start + 1));
    header('Content-Range: bytes ' . $start . '-' . $end . '/' . $fileSize);
    streamFileRange($target, $start, $end);
    exit;
}

header('Content-Length: ' . (string) $fileSize);
streamFileRange($target, 0, $fileSize - 1);
