<?php
declare(strict_types=1);

$baseDir = __DIR__;
$storageRoot = $baseDir . '/freecloud_files';
$metaRoot = $baseDir . '/freecloud_meta';
$configFile = $baseDir . '/config.json';

function jsonResponse(array $data, int $status = 200): never
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
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

function ensureProtectedDirectory(string $path): void
{
    if (!is_dir($path)) {
        mkdir($path, 0775, true);
    }

    $denyFile = $path . '/.htaccess';
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

function loadJsonMap(string $path): array
{
    if (!is_file($path)) {
        return [];
    }

    $raw = file_get_contents($path);
    $data = json_decode(is_string($raw) ? $raw : '', true);
    return is_array($data) ? $data : [];
}

function saveJsonFile(string $path, array $data): bool
{
    $dir = dirname($path);
    if (!is_dir($dir) && !mkdir($dir, 0775, true) && !is_dir($dir)) {
        return false;
    }

    $tmp = $path . '.tmp';
    $saved = file_put_contents($tmp, json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX) !== false;
    if (!$saved) {
        return false;
    }
    if (!@rename($tmp, $path)) {
        @unlink($tmp);
        return false;
    }
    @chmod($path, 0600);
    return true;
}

function transferId(string $value): string
{
    $value = strtolower(trim($value));
    return preg_match('/^[a-f0-9]{32}$/', $value) === 1 ? $value : '';
}

function transferRecordPath(string $transferRoot, string $id): string
{
    return $transferRoot . '/' . $id . '.json';
}

function cleanupTransfers(string $transferRoot): void
{
    $now = time();
    foreach (glob($transferRoot . '/*.json') ?: [] as $recordPath) {
        $record = loadJsonMap($recordPath);
        $expiresAt = (int) ($record['expires_at'] ?? 0);
        if ($expiresAt > 0 && $expiresAt <= $now) {
            $id = transferId((string) ($record['transfer_id'] ?? ''));
            if ($id !== '') {
                @unlink($transferRoot . '/' . $id . '.bin');
                @unlink($transferRoot . '/' . $id . '.part');
            }
            @unlink($recordPath);
        }
    }
}

function requestPayload(): array
{
    $contentType = (string) ($_SERVER['CONTENT_TYPE'] ?? '');
    if (str_contains(strtolower($contentType), 'application/json')) {
        $raw = file_get_contents('php://input');
        $data = json_decode(is_string($raw) ? $raw : '', true);
        return is_array($data) ? $data : [];
    }

    return $_POST;
}

function saveConfig(string $configFile, string $name, string $password): bool
{
    $config = [
        'name' => trim($name) !== '' ? trim($name) : 'FreeCloud',
        'password_hash' => $password !== '' ? password_hash($password, PASSWORD_DEFAULT) : '',
        'created_at' => time(),
    ];

    $saved = file_put_contents($configFile, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX) !== false;
    if ($saved) {
        @chmod($configFile, 0600);
    }
    return $saved;
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

function normalizeDeviceId(string $value): string
{
    $value = strtolower(trim($value));
    $value = preg_replace('/[^a-z0-9._-]+/', '-', $value) ?? '';
    return trim($value, '.-_');
}

function makeDeviceId(): string
{
    return bin2hex(random_bytes(16));
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

function storageSummary(string $root): array
{
    $usedBytes = 0;
    $iterator = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS),
        RecursiveIteratorIterator::LEAVES_ONLY
    );

    foreach ($iterator as $item) {
        if ($item->isFile() && !$item->isLink() && $item->getFilename() !== '.htaccess') {
            $usedBytes += max(0, (int) $item->getSize());
        }
    }

    $freeBytes = disk_free_space($root);
    $capacityBytes = $freeBytes === false ? 0 : $usedBytes + max(0, (int) $freeBytes);
    $percentUsed = $capacityBytes > 0 ? min(100, max(0, (int) round(($usedBytes / $capacityBytes) * 100))) : 0;

    return [
        'used_bytes' => $usedBytes,
        'available_bytes' => $freeBytes === false ? null : max(0, (int) $freeBytes),
        'capacity_bytes' => $capacityBytes > 0 ? $capacityBytes : null,
        'percent_used' => $capacityBytes > 0 ? $percentUsed : null,
    ];
}

function metadataFileEntries(string $root): array
{
    $entries = [];
    foreach (listRecursive($root) as $entry) {
        $path = (string) ($entry['path'] ?? '');
        if ($path === '') {
            continue;
        }
        $entry['hash'] = '';
        $entry['version'] = 1;
        $entry['deleted'] = false;
        $entry['locations'] = [[
            'kind' => 'web_server',
            'device_id' => 'web_server',
            'name' => 'Web Server',
            'available' => true,
            'updated_at' => time(),
        ]];
        $entries[$path] = $entry;
    }
    return $entries;
}

function normalizedMetadataEntries(array $stored, string $root): array
{
    $entries = metadataFileEntries($root);
    foreach ($stored as $path => $entry) {
        if (!is_array($entry)) {
            continue;
        }
        $cleanPath = normalizeRelativePath((string) ($entry['path'] ?? $path));
        if ($cleanPath === '') {
            continue;
        }
        $entry['path'] = $cleanPath;
        $entry['name'] = (string) ($entry['name'] ?? basename($cleanPath));
        $entry['type'] = (string) ($entry['type'] ?? 'file');
        $entry['size'] = max(0, (int) ($entry['size'] ?? 0));
        $entry['mtime'] = max(0, (int) ($entry['mtime'] ?? 0));
        $entry['hash'] = (string) ($entry['hash'] ?? '');
        $entry['version'] = max(1, (int) ($entry['version'] ?? 1));
        $entry['deleted'] = (bool) ($entry['deleted'] ?? false);
        $locations = [];
        foreach (($entry['locations'] ?? []) as $location) {
            if (!is_array($location)) {
                continue;
            }
            $kind = (string) ($location['kind'] ?? '');
            $deviceId = normalizeDeviceId((string) ($location['device_id'] ?? ''));
            if ($kind === '' || $deviceId === '') {
                continue;
            }
            $locations[] = [
                'kind' => $kind,
                'device_id' => $deviceId,
                'name' => trim((string) ($location['name'] ?? '')),
                'available' => (bool) ($location['available'] ?? true),
                'updated_at' => max(0, (int) ($location['updated_at'] ?? 0)),
            ];
        }
        if (isset($entries[$cleanPath]) && is_array($entries[$cleanPath]['locations'] ?? null)) {
            $locations = array_merge((array) $entries[$cleanPath]['locations'], $locations);
        }
        $entry['locations'] = uniqueLocations($locations);
        if (!$entry['deleted']) {
            $entries[$cleanPath] = $entry;
        }
    }

    uasort($entries, static fn(array $a, array $b): int => strnatcasecmp((string) $a['path'], (string) $b['path']));
    return $entries;
}

function uniqueLocations(array $locations): array
{
    $seen = [];
    $out = [];
    foreach ($locations as $location) {
        if (!is_array($location)) {
            continue;
        }
        $kind = (string) ($location['kind'] ?? '');
        $deviceId = normalizeDeviceId((string) ($location['device_id'] ?? ''));
        if ($kind === '' || $deviceId === '') {
            continue;
        }
        $key = $kind . ':' . $deviceId;
        $clean = [
            'kind' => $kind,
            'device_id' => $deviceId,
            'name' => trim((string) ($location['name'] ?? '')),
            'available' => (bool) ($location['available'] ?? true),
            'updated_at' => max(0, (int) ($location['updated_at'] ?? 0)),
        ];
        if (!isset($seen[$key])) {
            $seen[$key] = count($out);
            $out[] = $clean;
            continue;
        }
        $index = $seen[$key];
        if ($clean['updated_at'] >= (int) ($out[$index]['updated_at'] ?? 0)) {
            $out[$index] = $clean;
        }
    }
    return $out;
}

function metadataListDirectory(array $entries, string $relativePath): array
{
    $folder = normalizeRelativePath($relativePath);
    $prefix = $folder === '' ? '' : $folder . '/';
    $out = [];
    foreach ($entries as $entry) {
        $path = (string) ($entry['path'] ?? '');
        if ($path === '') {
            continue;
        }
        if ($folder !== '') {
            if (!str_starts_with($path, $prefix)) {
                continue;
            }
            $remainder = substr($path, strlen($prefix));
        } else {
            $remainder = $path;
        }
        if ($remainder === '' || str_contains($remainder, '/')) {
            continue;
        }
        $out[] = $entry;
    }

    usort($out, static function (array $a, array $b): int {
        if (($a['type'] ?? '') !== ($b['type'] ?? '')) {
            return ($a['type'] ?? '') === 'dir' ? -1 : 1;
        }
        return strnatcasecmp((string) ($a['name'] ?? $a['path'] ?? ''), (string) ($b['name'] ?? $b['path'] ?? ''));
    });
    return $out;
}

function validateRule(array $rule): ?array
{
    $mode = (string) ($rule['mode'] ?? '');
    $allowedModes = ['browse_only', 'keep_offline', 'sync', 'upload_only', 'download_only', 'storage_only'];
    if (!in_array($mode, $allowedModes, true)) {
        return null;
    }
    return [
        'path' => normalizeRelativePath((string) ($rule['path'] ?? '')),
        'mode' => $mode,
        'recursive' => (bool) ($rule['recursive'] ?? true),
    ];
}

function movePath(string $root, string $fromPath, string $toPath): void
{
    $fromPath = normalizeRelativePath($fromPath);
    $toPath = normalizeRelativePath($toPath);
    if ($fromPath === '' || $toPath === '') {
        jsonResponse(['ok' => false, 'error' => 'Missing move path.'], 400);
    }
    if ($fromPath === $toPath) {
        jsonResponse(['ok' => true, 'path' => $toPath]);
    }

    $source = resolveExistingPath($root, $fromPath);
    if ($source === null || $source === $root) {
        jsonResponse(['ok' => false, 'error' => 'Item not found.'], 404);
    }

    $destination = pathToAbsolute($root, $toPath);
    $destinationDir = dirname($destination);
    $realDestinationDir = realpath($destinationDir);
    if (
        $realDestinationDir === false ||
        !is_dir($realDestinationDir) ||
        !isWithinRoot($realDestinationDir, $root) ||
        !isWithinRoot($destination, $root)
    ) {
        jsonResponse(['ok' => false, 'error' => 'Move target folder not found.'], 404);
    }
    if (file_exists($destination)) {
        jsonResponse(['ok' => false, 'error' => 'An item with that name already exists.'], 409);
    }
    if (is_dir($source) && isWithinRoot($destination, $source)) {
        jsonResponse(['ok' => false, 'error' => 'Cannot move a folder into itself.'], 400);
    }
    if (!@rename($source, $destination)) {
        jsonResponse(['ok' => false, 'error' => 'Could not move item.'], 500);
    }
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
ensureProtectedDirectory($metaRoot);
$transferRoot = $metaRoot . '/transfers';
ensureProtectedDirectory($transferRoot);
cleanupTransfers($transferRoot);
$realStorageRoot = realpath($storageRoot);
if ($realStorageRoot === false) {
    jsonResponse(['ok' => false, 'error' => 'Could not prepare storage.'], 500);
}

$devicesFile = $metaRoot . '/devices.json';
$filesFile = $metaRoot . '/files.json';
$rulesFile = $metaRoot . '/sync_rules.json';
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
    if (strlen($password) < 8) {
        jsonResponse(['ok' => false, 'error' => 'Use a password with at least 8 characters.'], 400);
    }
    if (!saveConfig($configFile, $name, $password)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save config.json.'], 500);
    }
    jsonResponse(['ok' => true, 'setup' => true, 'name' => trim($name) !== '' ? trim($name) : 'FreeCloud']);
}

requireApiAuth($config);

if ($action === 'register_device') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'register_device requires POST.'], 405);
    }
    $payload = requestPayload();
    $devices = loadJsonMap($devicesFile);
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    if ($deviceId === '') {
        $deviceId = makeDeviceId();
    }
    $now = time();
    $existing = isset($devices[$deviceId]) && is_array($devices[$deviceId]) ? $devices[$deviceId] : [];
    $devices[$deviceId] = [
        'device_id' => $deviceId,
        'name' => trim((string) ($payload['name'] ?? $existing['name'] ?? 'FreeCloud Device')),
        'type' => trim((string) ($payload['type'] ?? $existing['type'] ?? 'cli')),
        'capabilities' => is_array($payload['capabilities'] ?? null) ? array_values($payload['capabilities']) : array_values((array) ($existing['capabilities'] ?? [])),
        'first_seen_at' => (int) ($existing['first_seen_at'] ?? $now),
        'last_seen_at' => $now,
        'online' => true,
    ];
    if (!saveJsonFile($devicesFile, $devices)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save device metadata.'], 500);
    }
    jsonResponse(['ok' => true, 'device' => $devices[$deviceId]]);
}

if ($action === 'heartbeat') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'heartbeat requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $devices = loadJsonMap($devicesFile);
    if ($deviceId === '' || !isset($devices[$deviceId]) || !is_array($devices[$deviceId])) {
        jsonResponse(['ok' => false, 'error' => 'Unknown device. Register it first.'], 404);
    }
    $devices[$deviceId]['last_seen_at'] = time();
    $devices[$deviceId]['online'] = true;
    if (!saveJsonFile($devicesFile, $devices)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save heartbeat.'], 500);
    }
    jsonResponse(['ok' => true, 'device' => $devices[$deviceId]]);
}

if ($action === 'list_devices') {
    jsonResponse(['ok' => true, 'devices' => array_values(loadJsonMap($devicesFile))]);
}

if ($action === 'poll_transfers') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'poll_transfers requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $devices = loadJsonMap($devicesFile);
    if ($deviceId === '' || !isset($devices[$deviceId])) {
        jsonResponse(['ok' => false, 'error' => 'Unknown device.'], 404);
    }
    $now = time();
    $pending = [];
    foreach (glob($transferRoot . '/*.json') ?: [] as $recordPath) {
        $record = loadJsonMap($recordPath);
        if ((string) ($record['source_device_id'] ?? '') !== $deviceId) {
            continue;
        }
        $status = (string) ($record['status'] ?? '');
        $leaseExpired = $status === 'uploading' && (int) ($record['claimed_at'] ?? 0) < $now - 180;
        if ($status !== 'queued' && !$leaseExpired) {
            continue;
        }
        $record['status'] = 'uploading';
        $record['claimed_at'] = $now;
        $record['updated_at'] = $now;
        if (!saveJsonFile($recordPath, $record)) {
            continue;
        }
        $pending[] = [
            'transfer_id' => (string) ($record['transfer_id'] ?? ''),
            'path' => (string) ($record['path'] ?? ''),
            'size' => (int) ($record['size'] ?? 0),
            'hash' => (string) ($record['hash'] ?? ''),
        ];
    }
    jsonResponse(['ok' => true, 'transfers' => $pending]);
}

if ($action === 'upload_transfer') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'upload_transfer requires POST.'], 405);
    }
    $id = transferId((string) ($_GET['transfer_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($_GET['device_id'] ?? headerValue('X-FreeCloud-Device')));
    $recordPath = transferRecordPath($transferRoot, $id);
    $record = $id !== '' ? loadJsonMap($recordPath) : [];
    if ($record === [] || (string) ($record['source_device_id'] ?? '') !== $deviceId) {
        jsonResponse(['ok' => false, 'error' => 'Transfer not found or assigned to another device.'], 404);
    }
    if (!in_array((string) ($record['status'] ?? ''), ['queued', 'uploading'], true)) {
        jsonResponse(['ok' => false, 'error' => 'Transfer is not accepting an upload.'], 409);
    }
    $expectedSize = max(0, (int) ($record['size'] ?? 0));
    $maxBytes = max(1, (int) ($record['max_bytes'] ?? 0));
    $contentLength = (int) ($_SERVER['CONTENT_LENGTH'] ?? 0);
    if (($contentLength > 0 && $contentLength > $maxBytes) || ($expectedSize > 0 && $contentLength > 0 && $contentLength !== $expectedSize)) {
        jsonResponse(['ok' => false, 'error' => 'Transfer size is invalid or exceeds the server limit.'], 413);
    }
    $input = fopen('php://input', 'rb');
    $partPath = $transferRoot . '/' . $id . '.part';
    $output = fopen($partPath, 'wb');
    if ($input === false || $output === false) {
        jsonResponse(['ok' => false, 'error' => 'Could not create temporary transfer file.'], 500);
    }
    $written = 0;
    while (!feof($input)) {
        $chunk = fread($input, 1024 * 1024);
        if ($chunk === false) {
            break;
        }
        $written += strlen($chunk);
        if ($written > $maxBytes || fwrite($output, $chunk) === false) {
            fclose($input);
            fclose($output);
            @unlink($partPath);
            jsonResponse(['ok' => false, 'error' => 'Transfer exceeds the server limit or could not be written.'], 413);
        }
    }
    fclose($input);
    fclose($output);
    if (($expectedSize > 0 && $written !== $expectedSize) || ($contentLength > 0 && $written !== $contentLength)) {
        @unlink($partPath);
        jsonResponse(['ok' => false, 'error' => 'Uploaded size does not match the requested file.'], 422);
    }
    $expectedHash = strtolower((string) ($record['hash'] ?? ''));
    $actualHash = hash_file('sha256', $partPath) ?: '';
    if ($expectedHash !== '' && !hash_equals($expectedHash, $actualHash)) {
        @unlink($partPath);
        jsonResponse(['ok' => false, 'error' => 'Uploaded file hash does not match metadata.'], 422);
    }
    $readyPath = $transferRoot . '/' . $id . '.bin';
    if (!@rename($partPath, $readyPath)) {
        @unlink($partPath);
        jsonResponse(['ok' => false, 'error' => 'Could not finalize temporary transfer file.'], 500);
    }
    @chmod($readyPath, 0600);
    $record['status'] = 'ready';
    $record['actual_size'] = $written;
    $record['actual_hash'] = $actualHash;
    $record['updated_at'] = time();
    $record['expires_at'] = time() + 3600;
    if (!saveJsonFile($recordPath, $record)) {
        @unlink($readyPath);
        jsonResponse(['ok' => false, 'error' => 'Could not finalize transfer metadata.'], 500);
    }
    jsonResponse(['ok' => true, 'transfer_id' => $id, 'size' => $written, 'hash' => $actualHash]);
}

if ($action === 'fail_transfer') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'fail_transfer requires POST.'], 405);
    }
    $payload = requestPayload();
    $id = transferId((string) ($payload['transfer_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $recordPath = transferRecordPath($transferRoot, $id);
    $record = $id !== '' ? loadJsonMap($recordPath) : [];
    if ($record === [] || (string) ($record['source_device_id'] ?? '') !== $deviceId) {
        jsonResponse(['ok' => false, 'error' => 'Transfer not found or assigned to another device.'], 404);
    }
    $record['status'] = 'failed';
    $record['error'] = substr(trim((string) ($payload['error'] ?? 'Source could not provide the file.')), 0, 500);
    $record['updated_at'] = time();
    $record['expires_at'] = time() + 3600;
    saveJsonFile($recordPath, $record);
    jsonResponse(['ok' => true]);
}

if ($action === 'metadata_manifest') {
    jsonResponse(['ok' => true, 'entries' => array_values(normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot))]);
}

if ($action === 'metadata_list') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    jsonResponse([
        'ok' => true,
        'path' => $path,
        'entries' => metadataListDirectory(normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot), $path),
    ]);
}

if ($action === 'get_sync_rules') {
    $deviceId = normalizeDeviceId((string) ($_GET['device_id'] ?? ''));
    $rules = loadJsonMap($rulesFile);
    if ($deviceId !== '') {
        jsonResponse(['ok' => true, 'device_id' => $deviceId, 'rules' => array_values((array) ($rules[$deviceId] ?? []))]);
    }
    jsonResponse(['ok' => true, 'rules' => $rules]);
}

if ($action === 'set_sync_rules') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'set_sync_rules requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    if ($deviceId === '') {
        jsonResponse(['ok' => false, 'error' => 'Missing device_id.'], 400);
    }
    $newRules = [];
    foreach (($payload['rules'] ?? []) as $rule) {
        if (is_array($rule)) {
            $cleanRule = validateRule($rule);
            if ($cleanRule !== null) {
                $newRules[] = $cleanRule;
            }
        }
    }
    $rules = loadJsonMap($rulesFile);
    $rules[$deviceId] = $newRules;
    if (!saveJsonFile($rulesFile, $rules)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save sync rules.'], 500);
    }
    jsonResponse(['ok' => true, 'device_id' => $deviceId, 'rules' => $newRules]);
}

if ($action === 'publish_manifest') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'publish_manifest requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    if ($deviceId === '') {
        jsonResponse(['ok' => false, 'error' => 'Missing device_id.'], 400);
    }
    $entries = is_array($payload['entries'] ?? null) ? $payload['entries'] : [];
    $stored = loadJsonMap($filesFile);
    $devices = loadJsonMap($devicesFile);
    $device = isset($devices[$deviceId]) && is_array($devices[$deviceId]) ? $devices[$deviceId] : [];
    $deviceName = trim((string) ($device['name'] ?? $deviceId));
    $now = time();
    $seenPaths = [];

    foreach ($entries as $entry) {
        if (!is_array($entry)) {
            continue;
        }
        $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
        if ($path === '') {
            continue;
        }
        $seenPaths[$path] = true;
        $existing = isset($stored[$path]) && is_array($stored[$path]) ? $stored[$path] : [];
        $incomingHash = (string) ($entry['hash'] ?? '');
        $existingHash = (string) ($existing['hash'] ?? '');
        $version = $existing === [] ? 1 : max(1, (int) ($existing['version'] ?? 1));
        if ($existing !== [] && $incomingHash !== '' && $existingHash !== '' && $incomingHash !== $existingHash) {
            $version += 1;
        }
        $locations = [];
        foreach (($existing['locations'] ?? []) as $location) {
            if (!is_array($location)) {
                continue;
            }
            if (normalizeDeviceId((string) ($location['device_id'] ?? '')) !== $deviceId) {
                $locations[] = $location;
            }
        }
        $locations[] = [
            'kind' => 'local_node',
            'device_id' => $deviceId,
            'name' => $deviceName,
            'available' => true,
            'updated_at' => $now,
        ];
        $locations = uniqueLocations($locations);
        $stored[$path] = [
            'path' => $path,
            'name' => (string) ($entry['name'] ?? basename($path)),
            'type' => (string) ($entry['type'] ?? 'file'),
            'size' => max(0, (int) ($entry['size'] ?? 0)),
            'mtime' => max(0, (int) ($entry['mtime'] ?? 0)),
            'hash' => $incomingHash,
            'version' => $version,
            'deleted' => false,
            'locations' => $locations,
        ];
    }

    foreach ($stored as $path => $entry) {
        if (!is_array($entry) || isset($seenPaths[(string) $path])) {
            continue;
        }
        $locations = [];
        foreach (($entry['locations'] ?? []) as $location) {
            if (!is_array($location)) {
                continue;
            }
            if (normalizeDeviceId((string) ($location['device_id'] ?? '')) !== $deviceId) {
                $locations[] = $location;
            }
        }
        $stored[$path]['locations'] = uniqueLocations($locations);
        if ($locations === []) {
            $stored[$path]['deleted'] = true;
        }
    }

    if (!saveJsonFile($filesFile, $stored)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save file metadata.'], 500);
    }
    jsonResponse(['ok' => true, 'published' => count($seenPaths)]);
}

if ($action === 'manifest') {
    jsonResponse(['ok' => true, 'entries' => listRecursive($realStorageRoot)]);
}

if ($action === 'list') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    jsonResponse(['ok' => true, 'path' => $path, 'entries' => listDirectory($realStorageRoot, $path)]);
}

if ($action === 'storage') {
    jsonResponse(['ok' => true, 'storage' => storageSummary($realStorageRoot)]);
}

if ($action === 'move') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'move requires POST.'], 405);
    }
    $fromPath = (string) ($_GET['from'] ?? $_POST['from'] ?? '');
    $toPath = (string) ($_GET['to'] ?? $_POST['to'] ?? '');
    movePath($realStorageRoot, $fromPath, $toPath);
    jsonResponse(['ok' => true, 'path' => normalizeRelativePath($toPath)]);
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
