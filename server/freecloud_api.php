<?php
declare(strict_types=1);

$baseDir = __DIR__;
$storageRoot = $baseDir . '/freecloud_files';
$configFile = $baseDir . '/config.json';

function jsonResponse(array $data, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    exit;
}

function ensureStorageRoot(string $storageRoot): void
{
    if (!is_dir($storageRoot)) {
        mkdir($storageRoot, 0775, true);
    }

    $denyFile = $storageRoot . '/.htaccess';
    if (!is_file($denyFile)) {
        @file_put_contents($denyFile, "Require all denied\nDeny from all\n", LOCK_EX);
    }
}

function loadConfig(string $configFile): ?array
{
    if (!is_file($configFile)) {
        return null;
    }

    $raw = file_get_contents($configFile);
    $config = json_decode(is_string($raw) ? $raw : '', true);
    return is_array($config) ? $config : null;
}

function saveConfig(string $configFile, string $name, string $password): bool
{
    $config = [
        'name' => trim($name) !== '' ? trim($name) : 'FreeCloud',
        'password_hash' => $password !== '' ? password_hash($password, PASSWORD_DEFAULT) : '',
        'created_at' => time(),
    ];

    return file_put_contents($configFile, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX) !== false;
}

function headerValue(string $name): string
{
    $serverKey = 'HTTP_' . strtoupper(str_replace('-', '_', $name));
    return isset($_SERVER[$serverKey]) ? (string) $_SERVER[$serverKey] : '';
}

function requireApiAuth(?array $config): void
{
    if ($config === null) {
        jsonResponse(['ok' => false, 'setup' => false, 'error' => 'FreeCloud is not set up.'], 409);
    }

    $hash = (string) ($config['password_hash'] ?? '');
    if ($hash === '') {
        return;
    }

    $password = headerValue('X-FreeCloud-Password');
    if ($password === '') {
        $authorization = headerValue('Authorization');
        if (str_starts_with($authorization, 'Bearer ')) {
            $password = substr($authorization, 7);
        }
    }

    if ($password === '' || !password_verify($password, $hash)) {
        jsonResponse(['ok' => false, 'error' => 'Authentication failed.'], 401);
    }
}

function normalizeRelativePath(string $path): string
{
    $path = str_replace(["\\", "\0"], ['/', ''], trim($path));
    $path = preg_replace('/[[:cntrl:]]+/', '', $path) ?? '';
    $parts = array_values(array_filter(explode('/', $path), static function (string $part): bool {
        return $part !== '' && $part !== '.' && $part !== '..';
    }));

    $safe = [];
    foreach ($parts as $part) {
        $part = trim($part);
        if ($part !== '') {
            $safe[] = $part;
        }
    }

    return implode('/', $safe);
}

function isWithinRoot(string $path, string $root): bool
{
    $path = rtrim(str_replace('\\', '/', $path), '/');
    $root = rtrim(str_replace('\\', '/', $root), '/');
    return $path === $root || str_starts_with($path, $root . '/');
}

function pathToAbsolute(string $root, string $relativePath): string
{
    $relativePath = normalizeRelativePath($relativePath);
    return $relativePath === '' ? $root : $root . '/' . $relativePath;
}

function resolveExistingPath(string $root, string $relativePath): ?string
{
    $target = realpath(pathToAbsolute($root, $relativePath));
    if ($target === false || !isWithinRoot($target, $root)) {
        return null;
    }

    return $target;
}

function deletePathRecursively(string $path): bool
{
    if (is_file($path) || is_link($path)) {
        return @unlink($path);
    }

    if (!is_dir($path)) {
        return false;
    }

    $items = scandir($path);
    if ($items === false) {
        return false;
    }

    foreach ($items as $item) {
        if ($item === '.' || $item === '..') {
            continue;
        }
        if (!deletePathRecursively($path . '/' . $item)) {
            return false;
        }
    }

    return @rmdir($path);
}

function entryInfo(string $root, string $absolutePath): ?array
{
    $real = realpath($absolutePath);
    if ($real === false || !isWithinRoot($real, $root)) {
        return null;
    }

    $relative = ltrim(str_replace('\\', '/', substr($real, strlen($root))), '/');
    if ($relative === '.htaccess' || str_starts_with($relative, '.htaccess/')) {
        return null;
    }

    $isDir = is_dir($real);
    return [
        'path' => $relative,
        'name' => basename($real),
        'type' => $isDir ? 'dir' : 'file',
        'size' => $isDir ? 0 : (filesize($real) ?: 0),
        'mtime' => filemtime($real) ?: 0,
    ];
}

function listRecursive(string $root): array
{
    $entries = [];
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::SELF_FIRST
    );

    foreach ($iterator as $item) {
        $name = $item->getFilename();
        if ($name === '.htaccess') {
            continue;
        }
        $info = entryInfo($root, $item->getPathname());
        if ($info !== null && $info['path'] !== '') {
            $entries[] = $info;
        }
    }

    usort($entries, static fn(array $a, array $b): int => strnatcasecmp($a['path'], $b['path']));
    return $entries;
}

function listDirectory(string $root, string $relativePath): array
{
    $directory = resolveExistingPath($root, $relativePath);
    if ($directory === null || !is_dir($directory)) {
        jsonResponse(['ok' => false, 'error' => 'Folder not found.'], 404);
    }

    $items = scandir($directory);
    if ($items === false) {
        jsonResponse(['ok' => false, 'error' => 'Could not list folder.'], 500);
    }

    $entries = [];
    foreach ($items as $item) {
        if ($item === '.' || $item === '..' || $item === '.htaccess') {
            continue;
        }
        $info = entryInfo($root, $directory . '/' . $item);
        if ($info !== null) {
            $entries[] = $info;
        }
    }

    usort($entries, static function (array $a, array $b): int {
        if ($a['type'] !== $b['type']) {
            return $a['type'] === 'dir' ? -1 : 1;
        }
        return strnatcasecmp($a['name'], $b['name']);
    });

    return $entries;
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

ensureStorageRoot($storageRoot);
$realStorageRoot = realpath($storageRoot);
if ($realStorageRoot === false) {
    jsonResponse(['ok' => false, 'error' => 'Could not prepare storage.'], 500);
}

$action = (string) ($_GET['action'] ?? $_POST['action'] ?? 'ping');
$config = loadConfig($configFile);

if ($action === 'ping') {
    if ($config === null) {
        jsonResponse(['ok' => true, 'setup' => false]);
    }
    requireApiAuth($config);
    jsonResponse([
        'ok' => true,
        'setup' => true,
        'name' => (string) ($config['name'] ?? 'FreeCloud'),
        'version' => 1,
    ]);
}

if ($action === 'setup') {
    if ($config !== null) {
        jsonResponse(['ok' => false, 'error' => 'FreeCloud is already set up.'], 409);
    }
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'Setup requires POST.'], 405);
    }

    $name = (string) ($_POST['name'] ?? 'FreeCloud');
    $password = (string) ($_POST['password'] ?? '');
    if (!saveConfig($configFile, $name, $password)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save config.json.'], 500);
    }
    jsonResponse(['ok' => true, 'setup' => true, 'name' => trim($name) !== '' ? trim($name) : 'FreeCloud']);
}

requireApiAuth($config);

if ($action === 'manifest') {
    jsonResponse(['ok' => true, 'entries' => listRecursive($realStorageRoot)]);
}

if ($action === 'list') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    jsonResponse(['ok' => true, 'path' => $path, 'entries' => listDirectory($realStorageRoot, $path)]);
}

if ($action === 'mkdir') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'mkdir requires POST.'], 405);
    }
    $path = normalizeRelativePath((string) ($_GET['path'] ?? $_POST['path'] ?? ''));
    if ($path === '') {
        jsonResponse(['ok' => false, 'error' => 'Missing folder path.'], 400);
    }
    $target = pathToAbsolute($realStorageRoot, $path);
    if (!isWithinRoot($target, $realStorageRoot)) {
        jsonResponse(['ok' => false, 'error' => 'Path rejected.'], 400);
    }
    if (!is_dir($target) && !mkdir($target, 0775, true) && !is_dir($target)) {
        jsonResponse(['ok' => false, 'error' => 'Could not create folder.'], 500);
    }
    jsonResponse(['ok' => true, 'path' => $path]);
}

if ($action === 'upload') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'upload requires POST.'], 405);
    }
    $path = normalizeRelativePath((string) ($_GET['path'] ?? $_POST['path'] ?? ''));
    if ($path === '') {
        jsonResponse(['ok' => false, 'error' => 'Missing upload path.'], 400);
    }
    $destination = pathToAbsolute($realStorageRoot, $path);
    $destinationDir = dirname($destination);
    if (!is_dir($destinationDir) && !mkdir($destinationDir, 0775, true) && !is_dir($destinationDir)) {
        jsonResponse(['ok' => false, 'error' => 'Could not create target folder.'], 500);
    }
    $realDestinationDir = realpath($destinationDir);
    if ($realDestinationDir === false || !isWithinRoot($realDestinationDir, $realStorageRoot) || !isWithinRoot($destination, $realStorageRoot)) {
        jsonResponse(['ok' => false, 'error' => 'Path rejected.'], 400);
    }

    if (isset($_FILES['file']) && is_uploaded_file((string) $_FILES['file']['tmp_name'])) {
        if (!move_uploaded_file((string) $_FILES['file']['tmp_name'], $destination)) {
            jsonResponse(['ok' => false, 'error' => 'Could not save upload.'], 500);
        }
    } else {
        $input = fopen('php://input', 'rb');
        $output = fopen($destination, 'wb');
        if ($input === false || $output === false) {
            jsonResponse(['ok' => false, 'error' => 'Could not open upload stream.'], 500);
        }
        stream_copy_to_stream($input, $output);
        fclose($input);
        fclose($output);
    }

    $mtime = (int) ($_GET['mtime'] ?? $_POST['mtime'] ?? 0);
    if ($mtime > 0) {
        @touch($destination, $mtime);
    }
    jsonResponse(['ok' => true, 'path' => $path]);
}

if ($action === 'save') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'save requires POST.'], 405);
    }
    $path = normalizeRelativePath((string) ($_GET['path'] ?? $_POST['path'] ?? ''));
    if ($path === '') {
        jsonResponse(['ok' => false, 'error' => 'Missing file path.'], 400);
    }
    $destination = pathToAbsolute($realStorageRoot, $path);
    $destinationDir = dirname($destination);
    if (!is_dir($destinationDir) && !mkdir($destinationDir, 0775, true) && !is_dir($destinationDir)) {
        jsonResponse(['ok' => false, 'error' => 'Could not create target folder.'], 500);
    }
    if (!isWithinRoot($destination, $realStorageRoot)) {
        jsonResponse(['ok' => false, 'error' => 'Path rejected.'], 400);
    }
    $content = file_get_contents('php://input');
    if ($content === false || file_put_contents($destination, $content, LOCK_EX) === false) {
        jsonResponse(['ok' => false, 'error' => 'Could not save file.'], 500);
    }
    jsonResponse(['ok' => true, 'path' => $path]);
}

if ($action === 'download') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    $target = $path === '' ? null : resolveExistingPath($realStorageRoot, $path);
    if ($target === null || !is_file($target)) {
        http_response_code(404);
        exit('File not found.');
    }
    $size = filesize($target);
    header('Content-Type: application/octet-stream');
    header('Content-Disposition: attachment; filename="' . rawurlencode(basename($target)) . '"');
    header('X-Content-Type-Options: nosniff');
    if ($size !== false) {
        header('Content-Length: ' . (string) $size);
    }
    streamFile($target);
    exit;
}

if ($action === 'delete') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'delete requires POST.'], 405);
    }
    $path = normalizeRelativePath((string) ($_GET['path'] ?? $_POST['path'] ?? ''));
    $target = $path === '' ? null : resolveExistingPath($realStorageRoot, $path);
    if ($target === null || $target === $realStorageRoot) {
        jsonResponse(['ok' => false, 'error' => 'Item not found.'], 404);
    }
    if (!deletePathRecursively($target)) {
        jsonResponse(['ok' => false, 'error' => 'Could not delete item.'], 500);
    }
    jsonResponse(['ok' => true, 'path' => $path]);
}

jsonResponse(['ok' => false, 'error' => 'Unknown action.'], 400);
