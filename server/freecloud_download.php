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
    if ($target === false || !isWithinRoot($target, $storageRoot)) {
        return null;
    }

    return $target;
}

function buildZipWithSystemCommand(string $target, string $zipPath, string $name): bool
{
    $command = sprintf(
        'cd %s && zip -r -q %s %s 2>&1',
        escapeshellarg(dirname($target)),
        escapeshellarg($zipPath),
        escapeshellarg($name)
    );

    $output = [];
    $exitCode = 1;
    exec($command, $output, $exitCode);

    return $exitCode === 0 && is_file($zipPath);
}

function streamFile(string $path): void
{
    $handle = fopen($path, 'rb');
    if ($handle === false) {
        http_response_code(500);
        exit('Could not open file.');
    }

    while (!feof($handle)) {
        $buffer = fread($handle, 1024 * 1024);
        if ($buffer === false) {
            break;
        }
        echo $buffer;
        flush();
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

if (is_file($target)) {
    $filename = basename($target);
    $size = filesize($target);
    header('Content-Type: application/octet-stream');
    if ($size !== false) {
        header('Content-Length: ' . (string) $size);
    }
    header('Content-Disposition: attachment; filename="' . rawurlencode($filename) . '"');
    header('X-Content-Type-Options: nosniff');
    streamFile($target);
    exit;
}

if (!is_dir($target)) {
    http_response_code(404);
    exit('File not found.');
}

$name = basename($target);
$tempZip = tempnam(sys_get_temp_dir(), 'freecloud-');
if ($tempZip === false) {
    http_response_code(500);
    exit('Could not create temporary zip file.');
}

$zipPath = $tempZip . '.zip';
if (!rename($tempZip, $zipPath)) {
    @unlink($tempZip);
    http_response_code(500);
    exit('Could not prepare zip file.');
}

if (class_exists('ZipArchive')) {
    $zip = new ZipArchive();
    if ($zip->open($zipPath, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
        @unlink($zipPath);
        http_response_code(500);
        exit('Could not create zip file.');
    }

    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($target, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::LEAVES_ONLY
    );

    foreach ($iterator as $file) {
        if (!$file->isFile()) {
            continue;
        }

        $filePath = $file->getRealPath();
        if ($filePath === false || !isWithinRoot($filePath, $target)) {
            continue;
        }

        $localName = $name . '/' . substr($filePath, strlen($target) + 1);
        $zip->addFile($filePath, $localName);
    }

    $zip->close();
} elseif (!buildZipWithSystemCommand($target, $zipPath, $name)) {
    @unlink($zipPath);
    http_response_code(500);
    exit('Could not create zip file on this server.');
}

$zipSize = filesize($zipPath);
header('Content-Type: application/zip');
if ($zipSize !== false) {
    header('Content-Length: ' . (string) $zipSize);
}
header('Content-Disposition: attachment; filename="' . rawurlencode($name . '.zip') . '"');
header('X-Content-Type-Options: nosniff');
streamFile($zipPath);
@unlink($zipPath);
