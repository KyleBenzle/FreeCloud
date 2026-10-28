<?php
declare(strict_types=1);

require_once __DIR__ . '/freecloud_lifecycle.php';

if (!defined('FC_LIFECYCLE_VERSION') || FC_LIFECYCLE_VERSION < 2) {
    http_response_code(503);
    header('Content-Type: application/json; charset=utf-8');
    echo '{"ok":false,"error":"Server files are out of sync: freecloud_lifecycle.php is older than freecloud_api.php. Deploy both together."}';
    exit;
}

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
    // Responses must always be valid JSON, even if a stored name is not UTF-8.
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);
    exit;
}

// Unreadable critical metadata stops the request before anything is written,
// so a damaged catalog or tombstone file is never replaced by an empty one.
set_exception_handler(static function (Throwable $error): void {
    if ($error instanceof FcCorruptMetadataException) {
        error_log('FreeCloud: ' . $error->getMessage());
        jsonResponse(['ok' => false, 'error' => $error->getMessage()
            . '. FreeCloud stopped without changing it; restore the file from a backup.'], 503);
    }
    error_log('FreeCloud API error: ' . $error);
    jsonResponse(['ok' => false, 'error' => 'Internal server error.'], 500);
});

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
    // Missing files are empty; unreadable critical metadata throws (see
    // fc_read_json_file) instead of masquerading as an empty catalog.
    return fc_read_json_file($path);
}

function saveJsonFile(string $path, array $data): bool
{
    return fc_save_json_atomic($path, $data);
}

/** Take the catalog write lock once per request (released when the request exits). */
function acquireCatalogLock(): void
{
    static $lock = null;
    global $metaRoot;
    if ($lock !== null) {
        return;
    }
    $handle = @fopen($metaRoot . '/catalog.lock', 'c');
    if ($handle === false || !flock($handle, LOCK_EX)) {
        jsonResponse(['ok' => false, 'error' => 'Could not lock FreeCloud metadata; retry safely.'], 503);
    }
    $lock = $handle;
}

/** Reject request strings that are not UTF-8 before they can reach metadata. */
function requireUtf8Request(): void
{
    $check = static function ($value) use (&$check): bool {
        if (is_array($value)) {
            foreach ($value as $key => $item) {
                if ((is_string($key) && !fc_is_valid_utf8($key)) || !$check($item)) return false;
            }
            return true;
        }
        return !is_string($value) || fc_is_valid_utf8($value);
    };
    $names = [];
    foreach ($_FILES as $file) {
        if (is_array($file)) $names[] = $file['name'] ?? '';
    }
    if (!$check($_GET) || !$check($_POST) || !$check($names)) {
        jsonResponse(['ok' => false, 'error' => 'File and folder names must be valid UTF-8.'], 400);
    }
}

/** Expected upload length from the request, when the client declared one. */
function declaredContentLength(): ?int
{
    $length = $_SERVER['CONTENT_LENGTH'] ?? $_SERVER['HTTP_CONTENT_LENGTH'] ?? null;
    if ($length === null || $length === '' || preg_match('/^\d+$/', (string) $length) !== 1) {
        return null;
    }
    return (int) $length;
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

    return fc_save_json_atomic($configFile, $config);
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
    // Names that cannot be stored as UTF-8 JSON stay on disk untouched but are
    // not catalogued; in-flight upload/install temp files are never content.
    if (!fc_is_valid_utf8($relative) || fc_is_internal_temp_name(basename($real))) {
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

function metadataFileEntries(string $root, ?string $relativePath = null): array
{
    $entries = [];
    if ($relativePath === null) {
        $physicalEntries = listRecursive($root);
    } else {
        // A metadata folder request needs only the physical Web Server
        // children of that folder, not a recursive walk of the whole drive.
        $physicalEntries = [];
        $directory = resolveExistingPath($root, $relativePath);
        if ($directory !== null && is_dir($directory)) {
            foreach (scandir($directory) ?: [] as $item) {
                if ($item === '.' || $item === '..' || $item === '.htaccess') {
                    continue;
                }
                $entry = entryInfo($root, $directory . '/' . $item);
                if ($entry !== null && $entry['path'] !== '') {
                    $physicalEntries[] = $entry;
                }
            }
        }
    }
    foreach ($physicalEntries as $entry) {
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
            'size' => max(0, (int) ($entry['size'] ?? 0)),
            'mtime' => max(0, (int) ($entry['mtime'] ?? 0)),
            'hash' => '',
        ]];
        $entries[$path] = $entry;
    }
    return $entries;
}

function metadataFileEntriesForRemoval(string $root, string $relativePath, bool $recursive): array
{
    $absolute = resolveExistingPath($root, $relativePath);
    if ($absolute === null) return [];
    $paths = [];
    if (is_file($absolute)) {
        $paths[] = $absolute;
    } elseif (is_dir($absolute)) {
        $paths[] = $absolute;
        if ($recursive) {
            $iterator = new RecursiveIteratorIterator(
                new RecursiveDirectoryIterator($absolute, FilesystemIterator::SKIP_DOTS),
                RecursiveIteratorIterator::SELF_FIRST
            );
            foreach ($iterator as $item) $paths[] = $item->getPathname();
        }
    }
    $entries = [];
    foreach ($paths as $path) {
        if (basename($path) === '.htaccess') continue;
        $entry = entryInfo($root, $path);
        if ($entry !== null && $entry['path'] !== '') {
            $entry['locations'] = [[
                'kind' => 'web_server', 'device_id' => 'web_server', 'name' => 'Web Server',
                'available' => true, 'updated_at' => time(), 'size' => max(0, (int) ($entry['size'] ?? 0)),
                'mtime' => max(0, (int) ($entry['mtime'] ?? 0)), 'hash' => '',
            ]];
            $entries[$entry['path']] = $entry;
        }
    }
    return $entries;
}

function normalizedMetadataEntries(array $stored, string $root, ?string $physicalPath = null): array
{
    global $tombstonesFile;
    $tombstones = isset($tombstonesFile) ? loadJsonMap($tombstonesFile) : [];
    $entries = metadataFileEntries($root, $physicalPath);
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
            // Web storage is authoritative from the managed filesystem scan.
            // Never preserve a historical Web location after its bytes are gone.
            if ($deviceId === 'web_server') {
                foreach ((array) ($entries[$cleanPath]['locations'] ?? []) as $webIndex => $webLocation) {
                    if (normalizeDeviceId((string) ($webLocation['device_id'] ?? '')) === 'web_server'
                        && (int) ($webLocation['size'] ?? -1) === (int) ($location['size'] ?? -2)
                        && abs((int) ($webLocation['mtime'] ?? 0) - (int) ($location['mtime'] ?? 0)) <= 2
                        && preg_match('/^[a-f0-9]{64}$/i', (string) ($location['hash'] ?? '')) === 1) {
                        $entries[$cleanPath]['locations'][$webIndex]['hash'] = strtolower((string) $location['hash']);
                    }
                }
                continue;
            }
            $locations[] = [
                'kind' => $kind,
                'device_id' => $deviceId,
                'name' => trim((string) ($location['name'] ?? '')),
                'available' => (bool) ($location['available'] ?? true),
                'updated_at' => max(0, (int) ($location['updated_at'] ?? 0)),
                // Older records inherit the entry facts for compatibility.
                'size' => max(0, (int) ($location['size'] ?? $entry['size'])),
                'mtime' => max(0, (int) ($location['mtime'] ?? $entry['mtime'])),
                'hash' => (string) ($location['hash'] ?? $entry['hash']),
            ];
        }
        if (isset($entries[$cleanPath]) && is_array($entries[$cleanPath]['locations'] ?? null)) {
            $locations = array_merge((array) $entries[$cleanPath]['locations'], $locations);
        }
        $entry['locations'] = uniqueLocations($locations);
        if (!$entry['deleted'] && !fc_catalog_path_is_tombstoned($cleanPath, $tombstones)) {
            $entries[$cleanPath] = $entry;
        }
    }

    foreach (array_keys($entries) as $path) {
        if (fc_catalog_path_is_tombstoned((string) $path, $tombstones)) unset($entries[$path]);
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
            'size' => max(0, (int) ($location['size'] ?? 0)),
            'mtime' => max(0, (int) ($location['mtime'] ?? 0)),
            'hash' => (string) ($location['hash'] ?? ''),
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

function deviceLocationKind(array $device): string
{
    $capabilities = array_map('strval', (array) ($device['capabilities'] ?? []));
    if (in_array('storage_node', $capabilities, true)) {
        return 'local_node';
    }
    $type = strtolower(trim((string) ($device['type'] ?? 'device')));
    $type = preg_replace('/[^a-z0-9_-]+/', '_', $type) ?? 'device';
    return $type !== '' ? $type : 'device';
}

function reconciliationDeviceSupported(array $device): bool
{
    $type = (string) ($device['type'] ?? '');
    $capabilities = array_map('strval', (array) ($device['capabilities'] ?? []));
    return in_array($type, ['desktop', 'android', 'raspberry_pi', 'linux_node', 'local_node'], true)
        || in_array('storage_node', $capabilities, true);
}

function placementRuleApplies(array $rule, string $path): bool
{
    $rulePath = normalizeRelativePath((string) ($rule['path'] ?? ''));
    $path = normalizeRelativePath($path);
    if ($rulePath === $path) {
        return true;
    }
    return (bool) ($rule['recursive'] ?? true)
        && ($rulePath === '' || str_starts_with($path, $rulePath . '/'));
}

function effectiveDesiredRule(string $path, string $deviceId, array $desiredRules, array $syncRules): ?array
{
    $candidates = [];
    foreach ((array) ($syncRules[$deviceId] ?? []) as $rule) {
        if (!is_array($rule) || !in_array((string) ($rule['mode'] ?? ''), ['storage_only', 'sync', 'keep_offline'], true)) {
            continue;
        }
        if (placementRuleApplies($rule, $path)) {
            $candidates[] = [
                'path' => normalizeRelativePath((string) ($rule['path'] ?? '')),
                'desired' => true,
                'recursive' => (bool) ($rule['recursive'] ?? true),
                'source' => (string) ($rule['mode'] ?? 'sync'),
                '_priority' => 0,
            ];
        }
    }
    foreach ((array) ($desiredRules[$deviceId] ?? []) as $rule) {
        if (!is_array($rule) || !placementRuleApplies($rule, $path)) {
            continue;
        }
        $candidates[] = [
            'path' => normalizeRelativePath((string) ($rule['path'] ?? '')),
            'desired' => (bool) ($rule['desired'] ?? false),
            'recursive' => (bool) ($rule['recursive'] ?? true),
            'source' => 'desired_rule',
            'updated_at' => max(0, (int) ($rule['updated_at'] ?? 0)),
            '_priority' => 1,
        ];
    }
    if ($candidates === []) {
        return null;
    }
    usort($candidates, static function (array $a, array $b): int {
        $specificity = strlen((string) $a['path']) <=> strlen((string) $b['path']);
        return $specificity !== 0 ? $specificity : ((int) $a['_priority'] <=> (int) $b['_priority']);
    });
    $winner = $candidates[count($candidates) - 1];
    unset($winner['_priority']);
    return $winner;
}

function desiredLocationsForPath(
    string $path,
    array $desiredRules,
    array $syncRules,
    array $devices,
    ?array $entry = null
): array
{
    $providerIds = ['web_server' => true];
    foreach (array_keys($devices) as $deviceId) {
        $providerIds[normalizeDeviceId((string) $deviceId)] = true;
    }
    foreach (array_keys($desiredRules) as $deviceId) {
        $providerIds[normalizeDeviceId((string) $deviceId)] = true;
    }
    foreach (array_keys($syncRules) as $deviceId) {
        $providerIds[normalizeDeviceId((string) $deviceId)] = true;
    }

    $locations = [];
    foreach (array_keys($providerIds) as $deviceId) {
        if ($deviceId === '') {
            continue;
        }
        $rule = effectiveDesiredRule($path, $deviceId, $desiredRules, $syncRules);
        // New normal FreeCloud content is shared by default. This policy is
        // stamped only on first publication; older catalog entries are not
        // migrated, and any explicit/inherited rule (including OFF) wins.
        if ($rule === null && is_array($entry)) {
            $placementDefault = (string) ($entry['placement_default'] ?? '');
            $defaultSource = normalizeDeviceId((string) ($entry['placement_source_device_id'] ?? ''));
            $isStorageProvider = $deviceId === 'web_server'
                || (isset($devices[$deviceId]) && is_array($devices[$deviceId])
                    && reconciliationDeviceSupported($devices[$deviceId]));
            if ($isStorageProvider && ($placementDefault === 'all'
                || ($placementDefault === 'source_only' && $deviceId === $defaultSource))) {
                $rule = [
                    'path' => '', 'desired' => true, 'recursive' => true,
                    'source' => 'new_item_default',
                ];
            }
        }
        if ($rule === null || !(bool) ($rule['desired'] ?? false)) {
            continue;
        }
        $device = isset($devices[$deviceId]) && is_array($devices[$deviceId]) ? $devices[$deviceId] : [];
        $locations[] = [
            'device_id' => $deviceId,
            'kind' => $deviceId === 'web_server' ? 'web_server' : deviceLocationKind($device),
            'name' => $deviceId === 'web_server' ? 'Web Server' : trim((string) ($device['name'] ?? $deviceId)),
            'rule_path' => (string) ($rule['path'] ?? ''),
            'recursive' => (bool) ($rule['recursive'] ?? true),
            'source' => (string) ($rule['source'] ?? 'desired_rule'),
        ];
    }
    return $locations;
}

function addDesiredLocations(array $entries, array $desiredRules, array $syncRules, array $devices): array
{
    foreach ($entries as $path => $entry) {
        if (!is_array($entry)) {
            continue;
        }
        $entryPath = normalizeRelativePath((string) ($entry['path'] ?? $path));
        $entries[$path]['desired_locations'] = desiredLocationsForPath($entryPath, $desiredRules, $syncRules, $devices, $entry);
    }
    return $entries;
}

function newManifestPlacementDefault(string $path, string $sourceDeviceId, array $syncRules, array $devices): array
{
    // A storage_only subtree remains selective no matter which client reports
    // it. This protects external collections such as the Pi library without
    // naming or special-casing any particular folder.
    $storageOnly = [];
    foreach ($syncRules as $deviceId => $rules) {
        foreach ((array) $rules as $rule) {
            if (!is_array($rule) || (string) ($rule['mode'] ?? '') !== 'storage_only'
                || !placementRuleApplies($rule, $path)) {
                continue;
            }
            $storageOnly[] = [
                'device_id' => normalizeDeviceId((string) $deviceId),
                'path' => normalizeRelativePath((string) ($rule['path'] ?? '')),
            ];
        }
    }
    if ($storageOnly !== []) {
        usort($storageOnly, static fn(array $a, array $b): int => strlen($a['path']) <=> strlen($b['path']));
        $source = $storageOnly[count($storageOnly) - 1]['device_id'];
        return ['placement_default' => 'source_only', 'placement_source_device_id' => $source];
    }

    $source = normalizeDeviceId($sourceDeviceId);
    $device = $devices[$source] ?? [];
    if ($source === 'web_server' || (is_array($device) && reconciliationDeviceSupported($device))) {
        return ['placement_default' => 'all', 'placement_source_device_id' => $source];
    }
    return ['placement_default' => 'source_only', 'placement_source_device_id' => $source];
}

function recordNewWebPlacementDefaults(string $path): bool
{
    global $filesFile, $realStorageRoot, $tombstonesFile;
    $path = normalizeRelativePath($path);
    if ($path === '') return false;
    $files = loadJsonMap($filesFile);
    $tombstones = loadJsonMap($tombstonesFile);
    $parts = explode('/', $path);
    $created = false;
    for ($index = 1; $index <= count($parts); $index++) {
        $itemPath = implode('/', array_slice($parts, 0, $index));
        if (isset($files[$itemPath]) && is_array($files[$itemPath])) continue;
        if (fc_catalog_path_is_tombstoned($itemPath, $tombstones)) continue;
        $entry = entryInfo($realStorageRoot, pathToAbsolute($realStorageRoot, $itemPath));
        if ($entry === null || normalizeRelativePath((string) ($entry['path'] ?? '')) !== $itemPath) continue;
        $entry['hash'] = '';
        $entry['version'] = 1;
        $entry['deleted'] = false;
        $entry['locations'] = [[
            'kind' => 'web_server', 'device_id' => 'web_server', 'name' => 'Web Server',
            'available' => true, 'updated_at' => time(), 'size' => max(0, (int) ($entry['size'] ?? 0)),
            'mtime' => max(0, (int) ($entry['mtime'] ?? 0)), 'hash' => '',
        ]];
        $entry['placement_default'] = 'all';
        $entry['placement_source_device_id'] = 'web_server';
        $files[$itemPath] = $entry;
        $created = true;
    }
    return !$created || saveJsonFile($filesFile, $files);
}

function pruneUnreferencedCatalogEntries(array &$files, string $root, string $reconcileRoot): int
{
    global $desiredRulesFile, $rulesFile, $devicesFile, $tombstonesFile;
    $desiredRules = loadJsonMap($desiredRulesFile);
    $syncRules = loadJsonMap($rulesFile);
    $devices = loadJsonMap($devicesFile);
    $tombstones = loadJsonMap($tombstonesFile);
    $physical = metadataFileEntries($root);
    $active = [];
    foreach (glob(rtrim($reconcileRoot, '/') . '/*.json') ?: [] as $recordPath) {
        $job = loadJsonMap($recordPath);
        $status = (string) ($job['status'] ?? '');
        if (!in_array($status, ['complete', 'cancelled', 'canceled'], true)) {
            $active[] = $job;
        }
    }
    $keep = [];
    foreach ($files as $key => $entry) {
        if (!is_array($entry)) continue;
        $path = normalizeRelativePath((string) ($entry['path'] ?? $key));
        if ($path === '') continue;
        $pending = false;
        foreach ($active as $job) {
            $jobPath = normalizeRelativePath((string) ($job['path'] ?? ''));
            $isDirectory = strtolower((string) ($entry['type'] ?? '')) === 'dir';
            if ($jobPath !== '' && (fc_scope_contains($path, $jobPath, (bool) ($job['recursive'] ?? false))
                || fc_scope_contains($jobPath, $path, $isDirectory))) {
                $pending = true;
                break;
            }
        }
        $actual = !empty($entry['locations']) || isset($physical[$path]);
        $tombstone = fc_tombstone_for_path($path, $tombstones);
        $desired = $tombstone === null && desiredLocationsForPath($path, $desiredRules, $syncRules, $devices, $entry) !== [];
        if ($actual || $desired || $pending || ($tombstone !== null && ($tombstone['status'] ?? '') !== 'complete')) $keep[$path] = true;
    }
    // Keep folder ancestors of retained descendants without retaining empty ghosts.
    foreach (array_keys($keep) as $path) {
        $parts = explode('/', $path);
        while (count($parts) > 1) {
            array_pop($parts);
            $parent = implode('/', $parts);
            if (isset($files[$parent]) && is_array($files[$parent])) $keep[$parent] = true;
        }
    }
    $removed = 0;
    foreach (array_keys($files) as $path) {
        $clean = normalizeRelativePath((string) ((is_array($files[$path] ?? null) ? ($files[$path]['path'] ?? '') : '') ?: $path));
        if ($clean !== '' && !isset($keep[$clean])) { unset($files[$path]); $removed++; }
    }
    return $removed;
}

function locationForDevice(array $entry, string $deviceId): ?array
{
    foreach ((array) ($entry['locations'] ?? []) as $location) {
        if (is_array($location) && normalizeDeviceId((string) ($location['device_id'] ?? '')) === $deviceId) {
            return $location;
        }
    }
    return null;
}

function locationFacts(array $location): array
{
    return [
        'size' => max(0, (int) ($location['size'] ?? 0)),
        'mtime' => max(0, (int) ($location['mtime'] ?? 0)),
        'hash' => strtolower((string) ($location['hash'] ?? '')),
    ];
}

function persistLocationHash(array &$entries, string $path, string $deviceId, string $hash, int $size, int $mtime): bool
{
    $path = normalizeRelativePath($path);
    $hash = strtolower($hash);
    if ($path === '' || preg_match('/^[a-f0-9]{64}$/', $hash) !== 1 || !isset($entries[$path]) || !is_array($entries[$path])) return false;
    foreach ((array) ($entries[$path]['locations'] ?? []) as $index => $location) {
        if (!is_array($location) || normalizeDeviceId((string) ($location['device_id'] ?? '')) !== normalizeDeviceId($deviceId)) continue;
        if ((int) ($location['size'] ?? -1) !== $size || abs((int) ($location['mtime'] ?? 0) - $mtime) > 2) return false;
        $entries[$path]['locations'][$index]['hash'] = $hash;
        $entries[$path]['locations'][$index]['updated_at'] = time();
        return true;
    }
    return false;
}

function saveVerifiedLocationHashes(string $filesFile, array $entries, array $verifiedHashes): bool
{
    $stored = loadJsonMap($filesFile);
    foreach ($verifiedHashes as $path => $providers) {
        $clean = normalizeRelativePath((string) $path);
        $entry = $entries[$clean] ?? null;
        if (!is_array($entry)) continue;
        foreach ((array) $providers as $providerId => $hash) {
            $deviceId = normalizeDeviceId((string) $providerId);
            $hash = strtolower((string) $hash);
            $location = locationForDevice($entry, $deviceId);
            if ($location === null || preg_match('/^[a-f0-9]{64}$/', $hash) !== 1) continue;
            if (!isset($stored[$clean]) || !is_array($stored[$clean])) {
                $stored[$clean] = [
                    'path' => $clean, 'name' => (string) ($entry['name'] ?? basename($clean)),
                    'type' => (string) ($entry['type'] ?? 'file'), 'size' => (int) ($entry['size'] ?? 0),
                    'mtime' => (int) ($entry['mtime'] ?? 0), 'hash' => '', 'version' => 1,
                    'deleted' => false, 'locations' => [],
                ];
            }
            $locations = (array) ($stored[$clean]['locations'] ?? []);
            $found = false;
            foreach ($locations as $index => $old) {
                if (!is_array($old) || normalizeDeviceId((string) ($old['device_id'] ?? '')) !== $deviceId) continue;
                if ((int) ($old['size'] ?? -1) !== (int) ($location['size'] ?? -2)
                    || abs((int) ($old['mtime'] ?? 0) - (int) ($location['mtime'] ?? 0)) > 2) continue;
                $locations[$index]['hash'] = $hash;
                $locations[$index]['updated_at'] = time();
                $found = true;
                break;
            }
            if (!$found) {
                $location['hash'] = $hash;
                $locations[] = $location;
            }
            $stored[$clean]['locations'] = uniqueLocations($locations);
        }
    }
    return saveJsonFile($filesFile, $stored);
}

function ensureRemovalHashJobs(array $entry, string $targetId, string $reconcileRoot, string $storageRoot, array &$entries): array
{
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    $required = [];
    $stale = array_map('strval', (array) ($entry['stale_locations'] ?? []));
    $locations = array_values(array_filter((array) ($entry['locations'] ?? []), 'is_array'));
    $targetHash = '';
    $verified = [];
    foreach ($locations as $location) {
        if (normalizeDeviceId((string) ($location['device_id'] ?? '')) === $targetId) {
            $candidateHash = strtolower((string) ($location['hash'] ?? ''));
            if (preg_match('/^[a-f0-9]{64}$/', $candidateHash) === 1) $targetHash = $candidateHash;
            break;
        }
    }
    if ($targetHash !== '') {
        foreach ($locations as $location) {
            $otherId = normalizeDeviceId((string) ($location['device_id'] ?? ''));
            $otherHash = strtolower((string) ($location['hash'] ?? ''));
            if ($otherId !== '' && $otherId !== $targetId && !in_array($otherId, $stale, true)
                && preg_match('/^[a-f0-9]{64}$/', $otherHash) === 1 && hash_equals($targetHash, $otherHash)) {
                return ['pending' => false, 'jobs' => [], 'verified' => [], 'error' => ''];
            }
        }
    }
    foreach ($locations as $location) {
        if (!is_array($location)) continue;
        $id = normalizeDeviceId((string) ($location['device_id'] ?? ''));
        if ($id === '' || ($id !== $targetId && in_array($id, $stale, true))) continue;
        if ($id === 'web_server') {
            $absolute = resolveExistingPath($storageRoot, $path);
            $hash = $absolute !== null && is_file($absolute) ? (hash_file('sha256', $absolute) ?: '') : '';
            if ($hash === '' || !persistLocationHash($entries, $path, $id, $hash, (int) ($location['size'] ?? 0), (int) ($location['mtime'] ?? 0))) {
                return ['pending' => false, 'jobs' => [], 'error' => 'Could not hash the current Web Server copy safely.'];
            }
            $verified[$path][$id] = $hash;
            continue;
        }
        $hash = strtolower((string) ($location['hash'] ?? ''));
        if (preg_match('/^[a-f0-9]{64}$/', $hash) === 1) continue;
        $jobId = substr(hash('sha256', "hash-proof\0{$path}\0{$id}\0" . (int) ($location['size'] ?? 0) . "\0" . (int) ($location['mtime'] ?? 0)), 0, 32);
        $recordPath = $reconcileRoot . '/' . $jobId . '.json';
        $record = loadJsonMap($recordPath);
        if (($record['status'] ?? '') === 'failed') {
            $record['status'] = 'queued_hash';
            $record['error'] = '';
            $record['updated_at'] = time();
            saveJsonFile($recordPath, $record);
        }
        if ($record === []) {
            $now = time();
            $record = [
                'job_id' => $jobId, 'kind' => 'hash_location', 'path' => $path,
                'source_device_id' => $id, 'destination_device_id' => '', 'status' => 'queued_hash',
                'expected_size' => max(0, (int) ($location['size'] ?? 0)),
                'expected_mtime' => max(0, (int) ($location['mtime'] ?? 0)),
                'hash' => '', 'created_at' => $now, 'updated_at' => $now, 'error' => '',
            ];
            if (!saveJsonFile($recordPath, $record)) return ['pending' => false, 'jobs' => [], 'error' => 'Could not queue file hash verification.'];
        }
        if (($record['status'] ?? '') !== 'complete') $required[] = $jobId;
    }
    return ['pending' => $required !== [], 'jobs' => array_values(array_unique($required)), 'verified' => $verified, 'error' => ''];
}

function sameLocationFacts(array $a, array $b): bool
{
    if ((int) ($a['size'] ?? -1) !== (int) ($b['size'] ?? -2)
        || abs((int) ($a['mtime'] ?? 0) - (int) ($b['mtime'] ?? 0)) > 2) {
        return false;
    }
    $aHash = strtolower((string) ($a['hash'] ?? ''));
    $bHash = strtolower((string) ($b['hash'] ?? ''));
    return $aHash === '' || $bHash === '' || hash_equals($aHash, $bHash);
}

function removalHash(array $entry, array $location, string $storageRoot): string
{
    $deviceId = normalizeDeviceId((string) ($location['device_id'] ?? ''));
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    if ($deviceId === 'web_server' && ($path !== '') && (string) ($entry['type'] ?? 'file') !== 'dir') {
        $absolute = resolveExistingPath($storageRoot, $path);
        if ($absolute !== null && is_file($absolute)) {
            $hash = hash_file('sha256', $absolute);
            return is_string($hash) ? strtolower($hash) : '';
        }
    }
    $hash = strtolower((string) ($location['hash'] ?? ''));
    return preg_match('/^[a-f0-9]{64}$/', $hash) === 1 ? $hash : '';
}

/** True when an explicit or inherited placement rule turns this provider OFF for the path. */
function providerSwitchedOff(string $path, string $deviceId, array $desiredRules, array $syncRules): bool
{
    $rule = effectiveDesiredRule($path, $deviceId, $desiredRules, $syncRules);
    return $rule !== null && !(bool) ($rule['desired'] ?? false);
}

/** True when a copy-removal job for this provider covering the path is still queued. */
function removalPendingFor(string $path, string $deviceId): bool
{
    global $reconcileRoot;
    // Loaded once per request: removal jobs created later in this request
    // always target the provider being removed, which is never an alternate.
    static $queued = null;
    if ($queued === null) {
        $queued = [];
        foreach (isset($reconcileRoot) ? (glob($reconcileRoot . '/*.json') ?: []) : [] as $recordPath) {
            $record = fc_read_json_file($recordPath, false);
            if (($record['status'] ?? '') !== 'queued_remove') continue;
            $queued[] = [
                normalizeDeviceId((string) ($record['destination_device_id'] ?? '')),
                normalizeRelativePath((string) ($record['path'] ?? '')),
                (bool) ($record['recursive'] ?? false),
            ];
        }
    }
    foreach ($queued as [$queuedDevice, $queuedPath, $queuedRecursive]) {
        if ($queuedDevice === $deviceId && fc_scope_contains($path, $queuedPath, $queuedRecursive)) return true;
    }
    return false;
}

function safeRemovalFacts(
    array $entries,
    string $path,
    string $deviceId,
    bool $recursive,
    array $desiredRules,
    array $syncRules,
    string $storageRoot
): array
{
    $path = normalizeRelativePath($path);
    $facts = [];
    // Removing a folder row always removes what is inside it (devices and
    // the Web Server delete the whole directory), so a non-recursive folder
    // request must be checked exactly like a recursive one.
    $rootEntry = $entries[$path] ?? null;
    if (!$recursive && is_array($rootEntry) && strtolower((string) ($rootEntry['type'] ?? '')) === 'dir') {
        $recursive = true;
    }
    foreach ($entries as $entryPath => $entry) {
        if (!is_array($entry)) continue;
        $clean = normalizeRelativePath((string) ($entry['path'] ?? $entryPath));
        if ($clean !== $path && (!$recursive || !str_starts_with($clean, $path . '/'))) continue;
        $target = locationForDevice($entry, $deviceId);
        if ($target === null) continue;
        if ($clean !== $path && $recursive) {
            $effective = desiredLocationsForPath($clean, $desiredRules, $syncRules, [], $entry);
            if (in_array($deviceId, array_map(static fn(array $item): string => (string) ($item['device_id'] ?? ''), $effective), true)) {
                throw new RuntimeException('Cannot remove folder: a more-specific child is still selected on this device.');
            }
        }
        $stale = array_map('strval', (array) ($entry['stale_locations'] ?? []));
        if (in_array($deviceId, $stale, true)) {
            throw new RuntimeException("Cannot remove: target copy has ambiguous or pending version state.");
        }
        $targetHash = removalHash($entry, $target, $storageRoot);
        $removing = array_map('strval', (array) ($entry['removing_locations'] ?? []));
        $alternate = false;
        foreach ((array) ($entry['locations'] ?? []) as $location) {
            if (!is_array($location)) continue;
            $otherId = normalizeDeviceId((string) ($location['device_id'] ?? ''));
            if ($otherId === '' || $otherId === $deviceId || in_array($otherId, $stale, true)) continue;
            // A copy that is itself being removed, or whose provider has been
            // switched OFF for this item, cannot be the surviving copy. Without
            // this, two OFF toggles could each count on the other's copy.
            if (in_array($otherId, $removing, true) || providerSwitchedOff($clean, $otherId, $desiredRules, $syncRules)
                || removalPendingFor($clean, $otherId)) continue;
            if ((string) ($entry['type'] ?? '') === 'dir') {
                $alternate = true;
                break;
            }
            $otherHash = removalHash($entry, $location, $storageRoot);
            if ($targetHash !== '' && $otherHash !== '' && hash_equals($targetHash, $otherHash)) {
                $alternate = true;
                break;
            }
        }
        if (!$alternate) throw new RuntimeException('Cannot remove: no other verified copy could be proven by SHA-256.');
        $facts[$clean] = ['type' => (string) ($entry['type'] ?? 'file')] + locationFacts($target);
        if ((string) ($entry['type'] ?? 'file') !== 'dir') $facts[$clean]['hash'] = $targetHash;
    }
    return $facts;
}

function planRemovalJob(array $entries, string $path, string $deviceId, bool $recursive, string $reconcileRoot, array $desiredRules, array $syncRules, string $storageRoot): array
{
    $facts = safeRemovalFacts($entries, $path, $deviceId, $recursive, $desiredRules, $syncRules, $storageRoot);
    $jobId = substr(hash('sha256', "remove\0{$path}\0{$deviceId}\0" . json_encode($facts)), 0, 32);
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $existing = loadJsonMap($recordPath);
    if ($existing !== []) return $existing;
    $now = time();
    $record = [
        'job_id' => $jobId, 'kind' => 'remove_copy', 'path' => normalizeRelativePath($path),
        'source_device_id' => '', 'destination_device_id' => $deviceId,
        'status' => 'queued_remove', 'recursive' => $recursive, 'removal_facts' => $facts,
        'expected_size' => 0, 'expected_mtime' => 0, 'hash' => '',
        'created_at' => $now, 'updated_at' => $now, 'error' => '',
    ];
    if (!saveJsonFile($recordPath, $record)) throw new RuntimeException('Could not save removal job.');
    return $record;
}

function processWebRemoval(array $job, array $entries, string $storageRoot, string $recordPath, array $desiredRules, array $syncRules): array
{
    safeRemovalFacts($entries, (string) $job['path'], 'web_server', (bool) ($job['recursive'] ?? false), $desiredRules, $syncRules, $storageRoot);
    $target = resolveExistingPath($storageRoot, (string) $job['path']);
    if ($target === null || $target === $storageRoot || !deletePathRecursively($target)) {
        throw new RuntimeException('Could not safely remove the Web Server copy.');
    }
    $job['status'] = 'complete';
    $job['updated_at'] = time();
    saveJsonFile($recordPath, $job);
    return $job;
}

function retryBlockedRemoval(array $request, string $requestPath, string $filesFile, string $desiredRulesFile, string $rulesFile, string $devicesFile, string $reconcileRoot, string $storageRoot): void
{
    $path = normalizeRelativePath((string) ($request['path'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($request['device_id'] ?? ''));
    $recursive = (bool) ($request['recursive'] ?? false);
    if ($path === '' || $deviceId === '') return;

    $rules = loadJsonMap($desiredRulesFile);
    $deviceRules = [];
    foreach ((array) ($rules[$deviceId] ?? []) as $existingRule) {
        if (is_array($existingRule) && normalizeRelativePath((string) ($existingRule['path'] ?? '')) !== $path) $deviceRules[] = $existingRule;
    }
    $deviceRules[] = ['path' => $path, 'desired' => false, 'recursive' => $recursive, 'updated_at' => time()];
    $rules[$deviceId] = $deviceRules;
    if (!saveJsonFile($desiredRulesFile, $rules)) {
        $request['status'] = 'blocked';
        $request['error'] = 'Could not save desired placement.';
        $request['updated_at'] = time();
        saveJsonFile($requestPath, $request);
        return;
    }

    $stored = loadJsonMap($filesFile);
    $subset = [];
    foreach ($stored as $entryPath => $entry) {
        $clean = normalizeRelativePath((string) (is_array($entry) ? ($entry['path'] ?? $entryPath) : $entryPath));
        if ($clean === $path || ($recursive && str_starts_with($clean, $path . '/'))) $subset[$entryPath] = $entry;
    }
    foreach (metadataFileEntriesForRemoval($storageRoot, $path, $recursive) as $physicalPath => $physicalEntry) {
        $old = isset($subset[$physicalPath]) && is_array($subset[$physicalPath]) ? $subset[$physicalPath] : [];
        $oldLocations = (array) ($old['locations'] ?? []);
        $subset[$physicalPath] = array_merge($old, $physicalEntry, [
            'locations' => uniqueLocations(array_merge($oldLocations, (array) ($physicalEntry['locations'] ?? []))),
        ]);
    }
    $entries = normalizedMetadataEntries($subset, $storageRoot, $path);
    foreach (metadataFileEntriesForRemoval($storageRoot, $path, $recursive) as $physicalPath => $physicalEntry) {
        $oldLocations = (array) ($entries[$physicalPath]['locations'] ?? []);
        $entries[$physicalPath] = array_merge($entries[$physicalPath] ?? [], $physicalEntry, [
            'locations' => uniqueLocations(array_merge($oldLocations, (array) ($physicalEntry['locations'] ?? []))),
        ]);
    }
    $entries = addReconciliationState($entries, $reconcileRoot);
    $hashJobIds = [];
    $verifiedHashes = [];
    foreach ($entries as $entry) {
        if (!is_array($entry) || (string) ($entry['type'] ?? 'file') === 'dir'
            || locationForDevice($entry, $deviceId) === null) continue;
        $hashes = ensureRemovalHashJobs($entry, $deviceId, $reconcileRoot, $storageRoot, $entries);
        if ($hashes['error'] !== '') {
            $request['status'] = 'blocked';
            $request['error'] = $hashes['error'];
            $request['updated_at'] = time();
            $request['next_retry_at'] = time() + 30;
            saveJsonFile($requestPath, $request);
            return;
        }
        $hashJobIds = array_merge($hashJobIds, $hashes['jobs']);
        foreach ((array) ($hashes['verified'] ?? []) as $verifiedPath => $providers) {
            foreach ((array) $providers as $providerId => $hash) $verifiedHashes[$verifiedPath][$providerId] = $hash;
        }
    }
    $hashJobIds = array_values(array_unique($hashJobIds));
    if ($verifiedHashes !== [] && !saveVerifiedLocationHashes($filesFile, $entries, $verifiedHashes)) {
        $request['status'] = 'blocked';
        $request['error'] = 'Could not save on-demand verification state.';
        $request['updated_at'] = time();
        $request['next_retry_at'] = time() + 30;
        saveJsonFile($requestPath, $request);
        return;
    }
    if ($hashJobIds !== []) {
        $devices = loadJsonMap($devicesFile);
        $waiting = false;
        foreach ($hashJobIds as $hashJobId) {
            $hashJob = loadJsonMap($reconcileRoot . '/' . $hashJobId . '.json');
            $sourceId = normalizeDeviceId((string) ($hashJob['source_device_id'] ?? ''));
            $lastSeen = (int) ($devices[$sourceId]['last_seen_at'] ?? 0);
            if ($sourceId !== '' && $sourceId !== 'web_server' && $lastSeen < time() - 180) $waiting = true;
        }
        $request['status'] = $waiting ? 'waiting' : 'hashing';
        $request['hash_job_ids'] = $hashJobIds;
        $request['error'] = '';
        $request['updated_at'] = time();
        $request['next_retry_at'] = time() + 30;
        saveJsonFile($requestPath, $request);
        return;
    }

    $syncRules = loadJsonMap($rulesFile);
    try {
        $facts = safeRemovalFacts($entries, $path, $deviceId, $recursive, $rules, $syncRules, $storageRoot);
        if ($facts === []) {
            $request['status'] = 'complete';
            $request['error'] = '';
            $request['updated_at'] = time();
            saveJsonFile($requestPath, $request);
            return;
        }
        $devices = loadJsonMap($devicesFile);
        $targetLastSeen = (int) ($devices[$deviceId]['last_seen_at'] ?? 0);
        if ($deviceId !== 'web_server' && $targetLastSeen < time() - 180) {
            $request['status'] = 'waiting';
            $request['error'] = 'Waiting for the target device to reconnect.';
            $request['updated_at'] = time();
            $request['next_retry_at'] = time() + 30;
            saveJsonFile($requestPath, $request);
            return;
        }
        $job = planRemovalJob($entries, $path, $deviceId, $recursive, $reconcileRoot, $rules, $syncRules, $storageRoot);
        if ($deviceId === 'web_server') {
            $job = processWebRemoval($job, $entries, $storageRoot, $reconcileRoot . '/' . $job['job_id'] . '.json', $rules, $syncRules);
            foreach ($stored as $storedPath => &$storedEntry) {
                if (!is_array($storedEntry)) continue;
                $cleanStoredPath = normalizeRelativePath((string) ($storedEntry['path'] ?? $storedPath));
                if ($cleanStoredPath !== $path && (!$recursive || !str_starts_with($cleanStoredPath, $path . '/'))) continue;
                $storedEntry['locations'] = array_values(array_filter((array) ($storedEntry['locations'] ?? []), static function ($location): bool {
                    return !is_array($location) || normalizeDeviceId((string) ($location['device_id'] ?? '')) !== 'web_server';
                }));
            }
            unset($storedEntry);
            saveJsonFile($filesFile, $stored);
        }
        $request['status'] = 'complete';
        $request['removal_job_id'] = (string) ($job['job_id'] ?? '');
        $request['error'] = '';
        $request['updated_at'] = time();
        saveJsonFile($requestPath, $request);
    } catch (RuntimeException $exc) {
        $request['status'] = 'blocked';
        $request['error'] = $exc->getMessage();
        $request['updated_at'] = time();
        $request['next_retry_at'] = time() + 30;
        saveJsonFile($requestPath, $request);
    }
}

function finishPendingRemoval(
    array $request,
    string $requestPath,
    string $filesFile,
    string $desiredRulesFile,
    string $rulesFile,
    string $devicesFile,
    string $reconcileRoot,
    string $storageRoot
): void {
    foreach ((array) ($request['hash_job_ids'] ?? []) as $jobId) {
        $hashJob = loadJsonMap($reconcileRoot . '/' . basename((string) $jobId) . '.json');
        if (($hashJob['status'] ?? '') === 'failed') {
            $request['status'] = 'blocked';
            $request['error'] = (string) ($hashJob['error'] ?? 'Could not obtain SHA-256 proof.');
            $request['updated_at'] = time();
            $request['next_retry_at'] = time() + 30;
            saveJsonFile($requestPath, $request);
            return;
        }
        if (($hashJob['status'] ?? '') !== 'complete') return;
    }
    $path = normalizeRelativePath((string) ($request['path'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($request['device_id'] ?? ''));
    $recursive = (bool) ($request['recursive'] ?? false);
    $allRules = loadJsonMap($desiredRulesFile);
    $deviceRules = [];
    foreach ((array) ($allRules[$deviceId] ?? []) as $oldRule) {
        if (is_array($oldRule) && normalizeRelativePath((string) ($oldRule['path'] ?? '')) !== $path) $deviceRules[] = $oldRule;
    }
    $deviceRules[] = ['path' => $path, 'desired' => false, 'recursive' => $recursive, 'updated_at' => time()];
    $allRules[$deviceId] = $deviceRules;
    if (!saveJsonFile($desiredRulesFile, $allRules)) {
        $request['status'] = 'blocked';
        $request['error'] = 'Could not save desired placement.';
        $request['updated_at'] = time();
        saveJsonFile($requestPath, $request);
        return;
    }
    $syncRules = loadJsonMap($rulesFile);
    $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $storageRoot);
    $entries = addReconciliationState($entries, $reconcileRoot);
    try {
        $facts = safeRemovalFacts($entries, $path, $deviceId, $recursive, $allRules, $syncRules, $storageRoot);
        if (!saveJsonFile($desiredRulesFile, $allRules)) throw new RuntimeException('Could not save desired placement.');
        $removal = planRemovalJob($entries, $path, $deviceId, $recursive, $reconcileRoot, $allRules, $syncRules, $storageRoot);
        if ($deviceId === 'web_server') {
            $removal = processWebRemoval($removal, $entries, $storageRoot, $reconcileRoot . '/' . $removal['job_id'] . '.json', $allRules, $syncRules);
        }
        $request['status'] = 'complete';
        $request['removal_job_id'] = (string) ($removal['job_id'] ?? '');
        $request['updated_at'] = time();
        saveJsonFile($requestPath, $request);
    } catch (RuntimeException $exc) {
        $request['status'] = 'blocked';
        $request['error'] = $exc->getMessage();
        $request['updated_at'] = time();
        $request['next_retry_at'] = time() + 30;
        saveJsonFile($requestPath, $request);
    }
}

function addReconciliationState(array $entries, string $reconcileRoot): array
{
    $stale = [];
    $conflicts = [];
    $failed = [];
    $reconciliationErrors = [];
    $removing = [];
    $removalBlocked = [];
    $removalBlockedErrors = [];
    $verifying = [];
    $waiting = [];
    foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
        $record = loadJsonMap($recordPath);
        $status = (string) ($record['status'] ?? '');
        if (($record['kind'] ?? '') === 'pending_removal' && in_array($status, ['hashing', 'waiting'], true)) {
            $path = normalizeRelativePath((string) ($record['path'] ?? ''));
            $destination = normalizeDeviceId((string) ($record['device_id'] ?? ''));
            foreach ($entries as $entryPath => $_entry) {
                $clean = normalizeRelativePath((string) $entryPath);
                if ($clean === $path || ((bool) ($record['recursive'] ?? false) && str_starts_with($clean, $path . '/'))) {
                    if ($status === 'waiting') $waiting[$clean][$destination] = true;
                    else $verifying[$clean][$destination] = true;
                }
            }
            continue;
        }
        if (($record['kind'] ?? '') === 'pending_removal' && in_array($status, ['failed', 'blocked'], true)) {
            $path = normalizeRelativePath((string) ($record['path'] ?? ''));
            $destination = normalizeDeviceId((string) ($record['device_id'] ?? ''));
            foreach ($entries as $entryPath => $_entry) {
                $clean = normalizeRelativePath((string) $entryPath);
                if ($clean === $path || ((bool) ($record['recursive'] ?? false) && str_starts_with($clean, $path . '/'))) {
                    $removalBlocked[$clean][$destination] = true;
                    $removalBlockedErrors[$clean][$destination] = (string) ($record['error'] ?? '');
                }
            }
            continue;
        }
        if ($status === 'queued_remove') {
            $path = normalizeRelativePath((string) ($record['path'] ?? ''));
            $destination = normalizeDeviceId((string) ($record['destination_device_id'] ?? ''));
            foreach ($entries as $entryPath => $_entry) {
                $clean = normalizeRelativePath((string) $entryPath);
                if ($clean === $path || ((bool) ($record['recursive'] ?? false) && str_starts_with($clean, $path . '/'))) {
                    $removing[$clean][$destination] = true;
                }
            }
            continue;
        }
        if (!in_array($status, ['queued_source', 'ready', 'installed', 'conflict', 'failed'], true)) {
            continue;
        }
        $path = normalizeRelativePath((string) ($record['path'] ?? ''));
        $destination = normalizeDeviceId((string) ($record['destination_device_id'] ?? ''));
        if ($path !== '' && $destination !== '') {
            $stale[$path][$destination] = true;
            if ($status === 'conflict') $conflicts[$path][$destination] = true;
            if ($status === 'failed') $failed[$path][$destination] = true;
            if (in_array($status, ['conflict', 'failed'], true)) {
                $reconciliationErrors[$path][$destination] = (string) ($record['error'] ?? '');
            }
        }
        foreach ((array) ($record['stale_device_ids'] ?? []) as $staleId) {
            $staleId = normalizeDeviceId((string) $staleId);
            if ($path !== '' && $staleId !== '') {
                $stale[$path][$staleId] = true;
                if ($status === 'conflict') $conflicts[$path][$staleId] = true;
                if ($status === 'failed') $failed[$path][$staleId] = true;
                if (in_array($status, ['conflict', 'failed'], true)) {
                    $reconciliationErrors[$path][$staleId] = (string) ($record['error'] ?? '');
                }
            }
        }
    }
    foreach ($entries as $path => $entry) {
        if (!is_array($entry)) {
            continue;
        }
        $entries[$path]['stale_locations'] = array_keys($stale[normalizeRelativePath((string) $path)] ?? []);
        $cleanPath = normalizeRelativePath((string) $path);
        $entries[$path]['reconciliation_conflict_locations'] = array_keys($conflicts[$cleanPath] ?? []);
        $entries[$path]['reconciliation_failed_locations'] = array_keys($failed[$cleanPath] ?? []);
        $entries[$path]['reconciliation_errors'] = $reconciliationErrors[$cleanPath] ?? (object) [];
        $entries[$path]['removing_locations'] = array_keys($removing[normalizeRelativePath((string) $path)] ?? []);
        $entries[$path]['removal_blocked_locations'] = array_keys($removalBlocked[normalizeRelativePath((string) $path)] ?? []);
        $entries[$path]['removal_blocked_errors'] = $removalBlockedErrors[normalizeRelativePath((string) $path)] ?? (object) [];
        $entries[$path]['removal_verifying_locations'] = array_keys($verifying[normalizeRelativePath((string) $path)] ?? []);
        $entries[$path]['removal_waiting_locations'] = array_keys($waiting[normalizeRelativePath((string) $path)] ?? []);
    }
    return $entries;
}

function phase3aSourceIsUnambiguous(array $entry, array $webLocation): bool
{
    $size = (int) ($webLocation['size'] ?? -1);
    $mtime = (int) ($webLocation['mtime'] ?? -1);
    foreach ((array) ($entry['locations'] ?? []) as $location) {
        if (!is_array($location) || (string) ($location['device_id'] ?? '') === 'web_server') {
            continue;
        }
        if (!array_key_exists('size', $location) || !array_key_exists('mtime', $location)) {
            return false;
        }
        if ((int) $location['size'] !== $size || abs((int) $location['mtime'] - $mtime) > 2) {
            return false;
        }
    }
    return true;
}

function phase3aJobId(string $path, string $destinationId, int $size, int $mtime): string
{
    return substr(hash('sha256', "phase3a\0{$path}\0web_server\0{$destinationId}\0{$size}\0{$mtime}"), 0, 32);
}

function planPhase3aJob(
    array $entry,
    string $destinationId,
    array $devices,
    string $storageRoot,
    string $reconcileRoot,
    bool $allowExistingDestination = false
): ?array {
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    $destination = isset($devices[$destinationId]) && is_array($devices[$destinationId]) ? $devices[$destinationId] : [];
    if ($path === '' || (string) ($entry['type'] ?? '') !== 'file'
        || !reconciliationDeviceSupported($destination)) {
        return null;
    }
    $destinationLocation = locationForDevice($entry, $destinationId);
    if ($destinationLocation !== null && !$allowExistingDestination) {
        return null;
    }
    $webLocation = locationForDevice($entry, 'web_server');
    if ($webLocation === null) {
        return null;
    }
    $size = max(0, (int) ($webLocation['size'] ?? $entry['size'] ?? 0));
    $mtime = max(0, (int) ($webLocation['mtime'] ?? $entry['mtime'] ?? 0));
    $jobId = phase3aJobId($path, $destinationId, $size, $mtime);
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $existing = loadJsonMap($recordPath);
    if ($existing !== []) {
        return $existing;
    }
    $now = time();
    if (!$allowExistingDestination && !phase3aSourceIsUnambiguous($entry, $webLocation)) {
        $record = [
            'job_id' => $jobId, 'kind' => 'phase3a_web_to_desktop', 'path' => $path,
            'source_device_id' => 'web_server', 'destination_device_id' => $destinationId,
            'status' => 'conflict', 'expected_size' => $size, 'expected_mtime' => $mtime,
            'hash' => '', 'created_at' => $now, 'updated_at' => $now,
            'error' => 'Provider metadata disagrees; automatic overwrite was refused.',
        ];
        saveJsonFile($recordPath, $record);
        return $record;
    }
    $source = resolveExistingPath($storageRoot, $path);
    if ($source === null || !is_file($source)) {
        return null;
    }
    $beforeSize = filesize($source);
    $beforeMtime = filemtime($source);
    if ($beforeSize === false || $beforeMtime === false || (int) $beforeSize !== $size || abs((int) $beforeMtime - $mtime) > 2) {
        return null;
    }
    $part = $reconcileRoot . '/' . $jobId . '.part';
    $ready = $reconcileRoot . '/' . $jobId . '.bin';
    if (!@copy($source, $part)) {
        return null;
    }
    $actualSize = filesize($part);
    $hash = hash_file('sha256', $part) ?: '';
    $afterSize = filesize($source);
    $afterMtime = filemtime($source);
    if ($actualSize === false || (int) $actualSize !== $size || $afterSize !== $beforeSize || $afterMtime !== $beforeMtime || $hash === '') {
        @unlink($part);
        return null;
    }
    if (!@rename($part, $ready)) {
        @unlink($part);
        return null;
    }
    @chmod($ready, 0600);
    $record = [
        'job_id' => $jobId, 'kind' => 'phase3a_web_to_desktop', 'path' => $path,
        'source_device_id' => 'web_server', 'destination_device_id' => $destinationId,
        'status' => 'ready', 'expected_size' => $size, 'expected_mtime' => $mtime,
        'hash' => $hash, 'created_at' => $now, 'updated_at' => $now, 'error' => '',
        'destination_facts' => $destinationLocation === null ? null : locationFacts($destinationLocation),
    ];
    if (!saveJsonFile($recordPath, $record)) {
        @unlink($ready);
        return null;
    }
    return $record;
}

function planClientToWebJob(array $entry, string $clientId, string $reconcileRoot): ?array
{
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    $client = locationForDevice($entry, $clientId);
    if ($path === '' || (string) ($entry['type'] ?? '') !== 'file' || $client === null) {
        return null;
    }
    $facts = locationFacts($client);
    $jobId = substr(hash('sha256', "reconcile\0{$path}\0{$clientId}\0web_server\0{$facts['size']}\0{$facts['mtime']}"), 0, 32);
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $existing = loadJsonMap($recordPath);
    if ($existing !== []) {
        return $existing;
    }
    $web = locationForDevice($entry, 'web_server');
    $now = time();
    $record = [
        'job_id' => $jobId, 'kind' => 'phase3b_desktop_to_web', 'path' => $path,
        'source_device_id' => $clientId, 'destination_device_id' => 'web_server',
        'status' => 'queued_source', 'expected_size' => $facts['size'], 'expected_mtime' => $facts['mtime'],
        'hash' => (string) $facts['hash'], 'created_at' => $now, 'updated_at' => $now, 'error' => '',
        'destination_facts' => $web === null ? null : locationFacts($web),
    ];
    return saveJsonFile($recordPath, $record) ? $record : null;
}

function saveAcceptedVersion(string $path, array $facts, string $acceptedVersionsFile): void
{
    $accepted = loadJsonMap($acceptedVersionsFile);
    $accepted[normalizeRelativePath($path)] = [
        'size' => max(0, (int) ($facts['size'] ?? 0)),
        'mtime' => max(0, (int) ($facts['mtime'] ?? 0)),
        'hash' => strtolower((string) ($facts['hash'] ?? '')),
        'updated_at' => time(),
    ];
    saveJsonFile($acceptedVersionsFile, $accepted);
}

function saveReconciliationConflict(
    string $path,
    string $desktopId,
    array $desktopFacts,
    array $webFacts,
    bool $desktopDesired,
    bool $webDesired,
    string $reconcileRoot,
    string $error
): array {
    $jobId = substr(hash('sha256', 'conflict' . "\0{$path}\0{$desktopId}\0" . json_encode([$desktopFacts, $webFacts])), 0, 32);
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $existing = loadJsonMap($recordPath);
    if ($existing !== []) {
        return $existing;
    }
    $now = time();
    $staleIds = [];
    if ($desktopDesired) {
        $staleIds[] = $desktopId;
    }
    if ($webDesired) {
        $staleIds[] = 'web_server';
    }
    $record = [
        'job_id' => $jobId, 'kind' => 'phase3b_conflict', 'path' => $path,
        'source_device_id' => '', 'destination_device_id' => '', 'stale_device_ids' => $staleIds,
        'status' => 'conflict', 'expected_size' => 0, 'expected_mtime' => 0, 'hash' => '',
        'created_at' => $now, 'updated_at' => $now, 'error' => $error,
    ];
    saveJsonFile($recordPath, $record);
    return $record;
}

function planDesktopWebReconciliation(
    array $entry,
    string $desktopId,
    bool $desktopDesired,
    bool $webDesired,
    array $devices,
    string $storageRoot,
    string $reconcileRoot,
    string $acceptedVersionsFile
): ?array {
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    $desktop = locationForDevice($entry, $desktopId);
    $web = locationForDevice($entry, 'web_server');
    if ($path === '' || (string) ($entry['type'] ?? '') !== 'file') {
        return null;
    }
    if ($desktop === null && $web !== null && $desktopDesired) {
        return planPhase3aJob($entry, $desktopId, $devices, $storageRoot, $reconcileRoot);
    }
    if ($web === null && $desktop !== null && $webDesired) {
        return planClientToWebJob($entry, $desktopId, $reconcileRoot);
    }
    if ($desktop === null || $web === null || sameLocationFacts($desktop, $web)) {
        if ($desktop !== null && $web !== null) {
            saveAcceptedVersion($path, locationFacts($desktop), $acceptedVersionsFile);
        }
        return null;
    }

    $desktopFacts = locationFacts($desktop);
    $webFacts = locationFacts($web);
    $accepted = loadJsonMap($acceptedVersionsFile);
    $baseline = isset($accepted[$path]) && is_array($accepted[$path]) ? $accepted[$path] : null;
    $desktopChanged = $baseline === null ? null : !sameLocationFacts($desktopFacts, $baseline);
    $webChanged = $baseline === null ? null : !sameLocationFacts($webFacts, $baseline);
    if ($baseline !== null && $desktopChanged && $webChanged) {
        return saveReconciliationConflict(
            $path, $desktopId, $desktopFacts, $webFacts, $desktopDesired, $webDesired,
            $reconcileRoot, 'Desktop and Web Server both changed since the accepted revision.'
        );
    }
    if (($baseline !== null && $desktopChanged && !$webChanged)
        || ($baseline === null && (int) $desktopFacts['mtime'] > (int) $webFacts['mtime'] + 2)) {
        return $webDesired ? planClientToWebJob($entry, $desktopId, $reconcileRoot) : null;
    }
    if (($baseline !== null && $webChanged && !$desktopChanged)
        || ($baseline === null && (int) $webFacts['mtime'] > (int) $desktopFacts['mtime'] + 2)) {
        return $desktopDesired ? planPhase3aJob($entry, $desktopId, $devices, $storageRoot, $reconcileRoot, true) : null;
    }
    return saveReconciliationConflict(
        $path, $desktopId, $desktopFacts, $webFacts, $desktopDesired, $webDesired,
        $reconcileRoot, 'Provider versions are too close or contradictory to choose safely.'
    );
}

function planClientToClientJob(array $entry, string $sourceId, string $destinationId, string $reconcileRoot): ?array
{
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    $source = locationForDevice($entry, $sourceId);
    if ($path === '' || $source === null || $sourceId === $destinationId) {
        return null;
    }
    $facts = locationFacts($source);
    $jobId = substr(hash('sha256', "reconcile\0{$path}\0{$sourceId}\0{$destinationId}\0{$facts['size']}\0{$facts['mtime']}"), 0, 32);
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $existing = loadJsonMap($recordPath);
    if ($existing !== []) {
        return $existing;
    }
    $destination = locationForDevice($entry, $destinationId);
    $now = time();
    $record = [
        'job_id' => $jobId, 'kind' => 'client_to_client', 'path' => $path,
        'source_device_id' => $sourceId, 'destination_device_id' => $destinationId,
        'status' => 'queued_source', 'expected_size' => $facts['size'], 'expected_mtime' => $facts['mtime'],
        'hash' => (string) $facts['hash'], 'created_at' => $now, 'updated_at' => $now, 'error' => '',
        'destination_facts' => $destination === null ? null : locationFacts($destination),
    ];
    return saveJsonFile($recordPath, $record) ? $record : null;
}

function planMultiProviderReconciliation(
    array $entry,
    array $devices,
    array $desiredIds,
    string $storageRoot,
    string $reconcileRoot,
    string $acceptedVersionsFile
): array {
    $desiredIds = array_values(array_unique(array_filter(array_map('strval', $desiredIds))));
    if ((string) ($entry['type'] ?? '') !== 'file' || $desiredIds === []) {
        return [];
    }
    $providers = ['web_server' => locationForDevice($entry, 'web_server')];
    foreach ($devices as $deviceId => $device) {
        if (is_array($device) && reconciliationDeviceSupported($device)) {
            $providers[(string) $deviceId] = locationForDevice($entry, (string) $deviceId);
        }
    }
    $actual = array_filter($providers, static fn($location): bool => is_array($location));
    if ($actual === []) {
        return [];
    }
    $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
    if (count($actual) > 1) {
        $firstActual = reset($actual);
        $agreedFacts = locationFacts(is_array($firstActual) ? $firstActual : []);
        $allActualSame = true;
        foreach ($actual as $location) {
            if (!sameLocationFacts(locationFacts($location), $agreedFacts)) {
                $allActualSame = false;
                break;
            }
        }
        if ($allActualSame) {
            saveAcceptedVersion($path, $agreedFacts, $acceptedVersionsFile);
            foreach ($desiredIds as $destinationId) {
                if (!isset($actual[$destinationId])) {
                    continue;
                }
                foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
                    $record = loadJsonMap($recordPath);
                    if (normalizeRelativePath((string) ($record['path'] ?? '')) === $path
                        && (string) ($record['destination_device_id'] ?? '') === $destinationId
                        && in_array((string) ($record['status'] ?? ''), ['queued_source', 'staged', 'ready', 'installed', 'conflict'], true)) {
                        $record['status'] = 'complete';
                        $record['error'] = '';
                        $record['updated_at'] = time();
                        saveJsonFile($recordPath, $record);
                        @unlink($reconcileRoot . '/' . (string) ($record['job_id'] ?? '') . '.bin');
                    }
                }
            }
            return [];
        }
    }
    $accepted = loadJsonMap($acceptedVersionsFile);
    $baseline = isset($accepted[$path]) && is_array($accepted[$path]) ? $accepted[$path] : null;
    $sourceId = '';
    if ($baseline !== null) {
        $changed = [];
        $current = [];
        foreach ($actual as $providerId => $location) {
            $facts = locationFacts($location);
            if (sameLocationFacts($facts, $baseline)) {
                $current[] = (string) $providerId;
                continue;
            }
            // A copy older than the accepted revision is out of date (for
            // example a device reconnecting after being offline), not a new
            // edit. It may only receive the accepted revision; letting it act
            // as the source would overwrite newer content everywhere.
            if ((int) ($facts['mtime'] ?? 0) < (int) ($baseline['mtime'] ?? 0) - 2) {
                continue;
            }
            $changed[] = (string) $providerId;
        }
        if (count($changed) > 1) {
            $facts = array_map(static fn(array $location): array => locationFacts($location), $actual);
            $jobId = substr(hash('sha256', 'multi-conflict' . "\0{$path}\0" . json_encode($facts)), 0, 32);
            $recordPath = $reconcileRoot . '/' . $jobId . '.json';
            $conflict = loadJsonMap($recordPath);
            if ($conflict === []) {
                $now = time();
                $conflict = [
                    'job_id' => $jobId, 'kind' => 'provider_conflict', 'path' => $path,
                    'source_device_id' => '', 'destination_device_id' => '',
                    'stale_device_ids' => array_values(array_unique($desiredIds)), 'status' => 'conflict',
                    'expected_size' => 0, 'expected_mtime' => 0, 'hash' => '',
                    'created_at' => $now, 'updated_at' => $now,
                    'error' => 'Multiple providers changed since the accepted revision.',
                ];
                saveJsonFile($recordPath, $conflict);
            }
            return [$conflict];
        }
        // With nothing newer than the accepted revision, copy from a provider
        // that holds it (never from an arbitrary, possibly stale, provider).
        $sourceId = count($changed) === 1 ? $changed[0] : (string) ($current[0] ?? '');
    } elseif (count($actual) === 1) {
        $sourceId = (string) array_key_first($actual);
    } else {
        $ordered = $actual;
        uasort($ordered, static fn(array $a, array $b): int => (int) ($b['mtime'] ?? 0) <=> (int) ($a['mtime'] ?? 0));
        $ids = array_keys($ordered);
        $first = $ordered[$ids[0]];
        $second = $ordered[$ids[1]];
        if ((int) ($first['mtime'] ?? 0) <= (int) ($second['mtime'] ?? 0) + 2) {
            $facts = array_map(static fn(array $location): array => locationFacts($location), $actual);
            $jobId = substr(hash('sha256', 'ambiguous' . "\0{$path}\0" . json_encode($facts)), 0, 32);
            $recordPath = $reconcileRoot . '/' . $jobId . '.json';
            $conflict = loadJsonMap($recordPath);
            if ($conflict === []) {
                $now = time();
                $conflict = [
                    'job_id' => $jobId, 'kind' => 'provider_conflict', 'path' => $path,
                    'source_device_id' => '', 'destination_device_id' => '',
                    'stale_device_ids' => array_values(array_unique($desiredIds)), 'status' => 'conflict',
                    'expected_size' => 0, 'expected_mtime' => 0, 'hash' => '',
                    'created_at' => $now, 'updated_at' => $now,
                    'error' => 'Provider versions are too close or contradictory to choose safely.',
                ];
                saveJsonFile($recordPath, $conflict);
            }
            return [$conflict];
        }
        $sourceId = (string) $ids[0];
    }
    if ($sourceId === '' || !isset($actual[$sourceId])) {
        return [];
    }
    $sourceFacts = locationFacts($actual[$sourceId]);
    $jobs = [];
    foreach (array_unique($desiredIds) as $destinationId) {
        $destinationId = normalizeDeviceId((string) $destinationId);
        if ($destinationId === '' || $destinationId === $sourceId || !array_key_exists($destinationId, $providers)) {
            continue;
        }
        $destination = $providers[$destinationId];
        if (is_array($destination) && sameLocationFacts(locationFacts($destination), $sourceFacts)) {
            foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
                $record = loadJsonMap($recordPath);
                if (normalizeRelativePath((string) ($record['path'] ?? '')) !== $path
                    || (string) ($record['destination_device_id'] ?? '') !== $destinationId
                    || !in_array((string) ($record['status'] ?? ''), ['queued_source', 'staged', 'ready', 'installed'], true)) {
                    continue;
                }
                $record['status'] = 'complete';
                $record['updated_at'] = time();
                saveJsonFile($recordPath, $record);
                @unlink($reconcileRoot . '/' . (string) ($record['job_id'] ?? '') . '.bin');
            }
            saveAcceptedVersion($path, $sourceFacts, $acceptedVersionsFile);
            continue;
        }
        if ($sourceId === 'web_server') {
            $job = planPhase3aJob($entry, $destinationId, $devices, $storageRoot, $reconcileRoot, $destination !== null);
        } elseif ($destinationId === 'web_server') {
            $job = planClientToWebJob($entry, $sourceId, $reconcileRoot);
        } else {
            $job = planClientToClientJob($entry, $sourceId, $destinationId, $reconcileRoot);
        }
        if ($job !== null) {
            $jobs[] = $job;
        }
    }
    if (count($actual) > 1) {
        $allSame = true;
        foreach ($actual as $location) {
            if (!sameLocationFacts(locationFacts($location), $sourceFacts)) {
                $allSame = false;
                break;
            }
        }
        if ($allSame) {
            saveAcceptedVersion($path, $sourceFacts, $acceptedVersionsFile);
        }
    }
    return $jobs;
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
$reconcileRoot = $metaRoot . '/reconciliation';
ensureProtectedDirectory($reconcileRoot);
$realStorageRoot = realpath($storageRoot);
if ($realStorageRoot === false) {
    jsonResponse(['ok' => false, 'error' => 'Could not prepare storage.'], 500);
}

$devicesFile = $metaRoot . '/devices.json';
$filesFile = $metaRoot . '/files.json';
$rulesFile = $metaRoot . '/sync_rules.json';
$desiredRulesFile = $metaRoot . '/desired_locations.json';
$acceptedVersionsFile = $metaRoot . '/accepted_versions.json';
$placementMigrationFile = $metaRoot . '/placement_adoption_v1.json';
$tombstonesFile = $metaRoot . '/tombstones.json';
$generationsFile = $metaRoot . '/generations.json';
$action = (string) ($_GET['action'] ?? $_POST['action'] ?? 'ping');
requireUtf8Request();
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

// Serialize catalog read-modify-write requests (manifest publication, placement
// changes, and device edits) so concurrent clients cannot overwrite each
// other's JSON updates. Read-only browsing remains unlocked.
$catalogWriteActions = [
    'register_device', 'heartbeat', 'rename_device', 'remove_device',
    'publish_manifest', 'set_desired_location', 'set_sync_rules', 'adopt_existing_locations',
    'global_delete', 'recreate_path', 'poll_reconciliation', 'upload_reconciliation', 'commit_reconciliation', 'ack_reconciliation', 'report_location_hash',
    'mkdir',
];
if (in_array($action, $catalogWriteActions, true)) {
    // jsonResponse exits the request, releasing the advisory lock. upload and
    // save take it themselves after receiving the body, so a slow transfer
    // never blocks other devices.
    acquireCatalogLock();
}

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
    $requestedName = trim((string) ($payload['name'] ?? $existing['name'] ?? 'FreeCloud Device'));
    $deviceName = (bool) ($existing['name_locked'] ?? false)
        ? trim((string) ($existing['name'] ?? $requestedName))
        : $requestedName;
    $devices[$deviceId] = [
        'device_id' => $deviceId,
        'name' => $deviceName,
        'type' => trim((string) ($payload['type'] ?? $existing['type'] ?? 'cli')),
        'capabilities' => is_array($payload['capabilities'] ?? null) ? array_values($payload['capabilities']) : array_values((array) ($existing['capabilities'] ?? [])),
        'first_seen_at' => (int) ($existing['first_seen_at'] ?? $now),
        'last_seen_at' => $now,
        'online' => true,
        'name_locked' => (bool) ($existing['name_locked'] ?? false),
    ];
    if (!saveJsonFile($devicesFile, $devices)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save device metadata.'], 500);
    }
    jsonResponse(['ok' => true, 'device' => $devices[$deviceId]]);
}

if ($action === 'global_delete') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'global_delete requires POST.'], 405);
    }
    $payload = requestPayload();
    $path = normalizeRelativePath((string) ($payload['path'] ?? ''));
    if ($path === '') jsonResponse(['ok' => false, 'error' => 'A file or folder path is required.'], 400);
    $stored = loadJsonMap($filesFile);
    $devices = loadJsonMap($devicesFile);
    $tombstones = loadJsonMap($tombstonesFile);
    try {
        $result = fc_begin_global_delete(
            $path,
            (bool) ($payload['recursive'] ?? false),
            $stored,
            metadataFileEntries($realStorageRoot),
            $devices,
            $tombstones,
            $reconcileRoot,
            $transferRoot
        );
    } catch (RuntimeException $error) {
        jsonResponse(['ok' => false, 'error' => $error->getMessage()], 404);
    }
    // Persist the global intent before publishing target work. A crash between
    // these writes can never leave an untracked destructive command.
    if (!saveJsonFile($tombstonesFile, $result['tombstones']) || !saveJsonFile($filesFile, $result['files'])) {
        jsonResponse(['ok' => false, 'error' => 'Could not persist global deletion.'], 500);
    }
    $result['jobs'] = fc_ensure_global_delete_jobs($result['tombstone'], $reconcileRoot);

    // The coordinator is always reachable; remove its managed bytes now.
    $webJob = $result['jobs']['web_server'] ?? null;
    $webError = '';
    if (is_array($webJob)) {
        $webTarget = resolveExistingPath($realStorageRoot, $path);
        if ($webTarget !== null && ($webTarget === $realStorageRoot || !deletePathRecursively($webTarget))) {
            $webError = 'Could not remove the Web Server copy; deletion remains queued.';
            $webJob['status'] = 'failed';
            $webJob['error'] = $webError;
            $webJob['updated_at'] = time();
            saveJsonFile($reconcileRoot . '/' . $webJob['job_id'] . '.json', $webJob);
        } else {
            fc_apply_global_delete_ack($webJob, 'web_server', $filesFile, $tombstonesFile,
                $reconcileRoot . '/' . $webJob['job_id'] . '.json');
            $remainingFiles = loadJsonMap($filesFile);
            pruneUnreferencedCatalogEntries($remainingFiles, $realStorageRoot, $reconcileRoot);
            saveJsonFile($filesFile, $remainingFiles);
        }
    }
    jsonResponse([
        'ok' => true,
        'path' => $path,
        'tombstone' => $result['tombstone'],
        'pending_devices' => array_values(array_filter(
            array_map('strval', (array) ($result['tombstone']['target_device_ids'] ?? [])),
            static fn(string $id): bool => $id !== 'web_server'
        )),
        'error' => $webError,
    ]);
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

if ($action === 'rename_device') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'rename_device requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $name = trim((string) ($payload['name'] ?? ''));
    $devices = loadJsonMap($devicesFile);
    if ($deviceId === '' || $deviceId === 'web_server' || !isset($devices[$deviceId]) || !is_array($devices[$deviceId])) {
        jsonResponse(['ok' => false, 'error' => 'Unknown or protected device.'], 404);
    }
    if ($name === '') {
        jsonResponse(['ok' => false, 'error' => 'Device name cannot be empty.'], 400);
    }
    $devices[$deviceId]['name'] = $name;
    $devices[$deviceId]['name_locked'] = true;
    $files = loadJsonMap($filesFile);
    foreach ($files as $path => $entry) {
        if (!is_array($entry)) {
            continue;
        }
        foreach ((array) ($entry['locations'] ?? []) as $index => $location) {
            if (is_array($location) && normalizeDeviceId((string) ($location['device_id'] ?? '')) === $deviceId) {
                $files[$path]['locations'][$index]['name'] = $name;
            }
        }
    }
    if (!saveJsonFile($devicesFile, $devices) || !saveJsonFile($filesFile, $files)) {
        jsonResponse(['ok' => false, 'error' => 'Could not rename device.'], 500);
    }
    jsonResponse(['ok' => true, 'device' => $devices[$deviceId]]);
}

if ($action === 'remove_device') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'remove_device requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $devices = loadJsonMap($devicesFile);
    if ($deviceId === '' || $deviceId === 'web_server' || !isset($devices[$deviceId])) {
        jsonResponse(['ok' => false, 'error' => 'Unknown or protected device.'], 404);
    }

    unset($devices[$deviceId]);
    $files = loadJsonMap($filesFile);
    foreach ($files as $path => $entry) {
        if (!is_array($entry)) {
            continue;
        }
        $locations = array_values(array_filter(
            (array) ($entry['locations'] ?? []),
            static fn($location): bool => !is_array($location)
                || normalizeDeviceId((string) ($location['device_id'] ?? '')) !== $deviceId
        ));
        $files[$path]['locations'] = uniqueLocations($locations);
    }
    $desiredRules = loadJsonMap($desiredRulesFile);
    $syncRules = loadJsonMap($rulesFile);
    unset($desiredRules[$deviceId], $syncRules[$deviceId]);

    if (!saveJsonFile($devicesFile, $devices)
        || !saveJsonFile($filesFile, $files)
        || !saveJsonFile($desiredRulesFile, $desiredRules)
        || !saveJsonFile($rulesFile, $syncRules)) {
        jsonResponse(['ok' => false, 'error' => 'Could not remove device metadata.'], 500);
    }
    jsonResponse(['ok' => true, 'device_id' => $deviceId]);
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

if ($action === 'poll_reconciliation') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'poll_reconciliation requires POST.'], 405);
    }
    $payload = requestPayload();
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $devices = loadJsonMap($devicesFile);
    $tombstones = loadJsonMap($tombstonesFile);
    $device = isset($devices[$deviceId]) && is_array($devices[$deviceId]) ? $devices[$deviceId] : [];
    if ($deviceId === '' || !reconciliationDeviceSupported($device)) {
        jsonResponse(['ok' => false, 'error' => 'Reconciliation is unavailable for this device.'], 404);
    }
    if ((int) ($device['last_seen_at'] ?? 0) < time() - 30) {
        $devices[$deviceId]['last_seen_at'] = time();
        $devices[$deviceId]['online'] = true;
        saveJsonFile($devicesFile, $devices);
        $device = $devices[$deviceId];
    }
    foreach (glob($reconcileRoot . '/pending-remove-*.json') ?: [] as $pendingPath) {
        $pending = loadJsonMap($pendingPath);
        if (($pending['kind'] ?? '') !== 'pending_removal'
            || normalizeDeviceId((string) ($pending['device_id'] ?? '')) !== $deviceId
            || !in_array((string) ($pending['status'] ?? ''), ['blocked', 'failed', 'waiting', 'hashing'], true)
            || (int) ($pending['next_retry_at'] ?? 0) > time()) continue;
        retryBlockedRemoval($pending, $pendingPath, $filesFile, $desiredRulesFile, $rulesFile, $devicesFile,
            $reconcileRoot, $realStorageRoot);
    }
    $tombstones = loadJsonMap($tombstonesFile);
    foreach ($tombstones as $tombstoneId => &$tombstone) {
        if (!is_array($tombstone)) continue;
        $targets = array_map('strval', (array) ($tombstone['target_device_ids'] ?? []));
        $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
        if (in_array($deviceId, $targets, true) && !in_array($deviceId, $acked, true)) {
            if (($tombstone['status'] ?? '') !== 'superseded') $tombstone['status'] = 'pending';
            $tombstone['updated_at'] = time();
            fc_make_global_delete_job($tombstone, $deviceId, $reconcileRoot);
        }
    }
    unset($tombstone);
    saveJsonFile($tombstonesFile, $tombstones);
    // The coordinator is an always-reachable storage provider. Retry its
    // tombstone deletions from ordinary device check-ins if the original
    // request could not remove its bytes.
    foreach (loadJsonMap($tombstonesFile) as $tombstone) {
        if (!is_array($tombstone)) continue;
        $targets = array_map('strval', (array) ($tombstone['target_device_ids'] ?? []));
        $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
        if (!in_array('web_server', $targets, true) || in_array('web_server', $acked, true)) continue;
        try {
            $webJob = fc_make_global_delete_job($tombstone, 'web_server', $reconcileRoot);
            $webPath = normalizeRelativePath((string) ($tombstone['path'] ?? ''));
            $webTarget = resolveExistingPath($realStorageRoot, $webPath);
            if ($webTarget !== null && ($webTarget === $realStorageRoot || !deletePathRecursively($webTarget))) {
                $webJob['status'] = 'failed';
                $webJob['error'] = 'Could not remove the Web Server copy; retrying later.';
                $webJob['updated_at'] = time();
                saveJsonFile($reconcileRoot . '/' . (string) $webJob['job_id'] . '.json', $webJob);
                continue;
            }
            fc_apply_global_delete_ack($webJob, 'web_server', $filesFile, $tombstonesFile,
                $reconcileRoot . '/' . (string) $webJob['job_id'] . '.json');
            $remainingFiles = loadJsonMap($filesFile);
            pruneUnreferencedCatalogEntries($remainingFiles, $realStorageRoot, $reconcileRoot);
            saveJsonFile($filesFile, $remainingFiles);
        } catch (Throwable $error) {
            // Keep the durable tombstone pending; another device check-in retries.
        }
    }
    $desiredRules = loadJsonMap($desiredRulesFile);
    $syncRules = loadJsonMap($rulesFile);
    // Desktop's recurring worker uses check_only to retrieve already queued
    // work without rescanning/planning against the complete metadata catalog.
    if (!(bool) ($payload['check_only'] ?? false)) {
        $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
        foreach ($entries as $entry) {
            if (!is_array($entry)) {
                continue;
            }
            $desired = desiredLocationsForPath((string) ($entry['path'] ?? ''), $desiredRules, $syncRules, $devices, $entry);
            $desiredIds = array_map(static fn(array $item): string => (string) ($item['device_id'] ?? ''), $desired);
            planMultiProviderReconciliation(
                $entry, $devices, $desiredIds, $realStorageRoot, $reconcileRoot, $acceptedVersionsFile
            );
        }
    }
    $jobs = [];
    $catalogForJobs = loadJsonMap($filesFile);
    foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
        $record = loadJsonMap($recordPath);
        $recordStatus = (string) ($record['status'] ?? '');
        $recordKind = (string) ($record['kind'] ?? '');
        $recordDestination = normalizeDeviceId((string) ($record['destination_device_id'] ?? ''));
        if (!in_array($recordKind, ['remove_copy', 'global_delete'], true)
            && in_array($recordStatus, ['queued_source', 'staged', 'ready', 'installed', 'conflict', 'failed'], true)) {
            $recordPathValue = normalizeRelativePath((string) ($record['path'] ?? ''));
            $desiredNow = desiredLocationsForPath(
                $recordPathValue, $desiredRules, $syncRules, $devices,
                isset($catalogForJobs[$recordPathValue]) && is_array($catalogForJobs[$recordPathValue])
                    ? $catalogForJobs[$recordPathValue] : null
            );
            $desiredIdsNow = array_map(static fn(array $item): string => (string) ($item['device_id'] ?? ''), $desiredNow);
            if (!in_array($recordDestination, $desiredIdsNow, true)) {
                $record['status'] = 'canceled';
                $record['error'] = 'Placement is no longer desired.';
                $record['updated_at'] = time();
                saveJsonFile($recordPath, $record);
                @unlink($reconcileRoot . '/' . (string) ($record['job_id'] ?? '') . '.bin');
                continue;
            }
        }
        $isDestination = (string) ($record['destination_device_id'] ?? '') === $deviceId
            && in_array((string) ($record['status'] ?? ''), ['ready', 'queued_remove', 'queued_delete', 'conflict'], true);
        if ($recordKind !== 'global_delete' && $isDestination
            && fc_generation_cleanup_pending(normalizeRelativePath((string) ($record['path'] ?? '')), $deviceId, $tombstones)) {
            // The old generation must be physically removed before the new
            // generation can be installed at the same path.
            continue;
        }
        $isSource = (string) ($record['source_device_id'] ?? '') === $deviceId
            && in_array((string) ($record['status'] ?? ''), ['queued_source', 'queued_hash'], true);
        if ($isDestination || $isSource) {
            $jobs[] = $record;
        }
    }
    jsonResponse(['ok' => true, 'jobs' => $jobs]);
}

if ($action === 'download_reconciliation') {
    $jobId = transferId((string) ($_GET['job_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($_GET['device_id'] ?? headerValue('X-FreeCloud-Device')));
    $record = $jobId !== '' ? loadJsonMap($reconcileRoot . '/' . $jobId . '.json') : [];
    $file = $reconcileRoot . '/' . $jobId . '.bin';
    if ($record !== [] && fc_catalog_path_is_tombstoned(
        normalizeRelativePath((string) ($record['path'] ?? '')), loadJsonMap($tombstonesFile)
    )) {
        jsonResponse(['ok' => false, 'error' => 'Path was globally deleted.'], 410);
    }
    $recordGeneration = fc_generation_for_path(normalizeRelativePath((string) ($record['path'] ?? '')), loadJsonMap($generationsFile));
    if ($recordGeneration !== null && !in_array($deviceId,
        array_map('strval', (array) ($recordGeneration['authorized_device_ids'] ?? [])), true)) {
        $transferPath = normalizeRelativePath((string) ($record['path'] ?? ''));
        $transferCatalog = loadJsonMap($filesFile);
        $desired = desiredLocationsForPath($transferPath,
            loadJsonMap($desiredRulesFile), loadJsonMap($rulesFile), loadJsonMap($devicesFile),
            isset($transferCatalog[$transferPath]) && is_array($transferCatalog[$transferPath])
                ? $transferCatalog[$transferPath] : null);
        $desiredIds = array_map(static fn(array $item): string => (string) ($item['device_id'] ?? ''), $desired);
        if (!in_array($deviceId, $desiredIds, true)
            || fc_generation_cleanup_pending(normalizeRelativePath((string) ($record['path'] ?? '')), $deviceId,
                loadJsonMap($tombstonesFile))) {
            jsonResponse(['ok' => false, 'error' => 'Destination must finish old-generation deletion before receiving this generation.'], 409);
        }
    }
    if ($record === [] || (string) ($record['destination_device_id'] ?? '') !== $deviceId
        || (string) ($record['status'] ?? '') !== 'ready' || !is_file($file)) {
        jsonResponse(['ok' => false, 'error' => 'Reconciliation download is unavailable.'], 404);
    }
    header('Content-Type: application/octet-stream');
    header('Content-Length: ' . (string) filesize($file));
    header('X-FreeCloud-SHA256: ' . (string) ($record['hash'] ?? ''));
    streamFile($file);
    exit;
}

if ($action === 'upload_reconciliation') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'upload_reconciliation requires POST.'], 405);
    }
    $jobId = transferId((string) ($_GET['job_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($_GET['device_id'] ?? headerValue('X-FreeCloud-Device')));
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $record = $jobId !== '' ? loadJsonMap($recordPath) : [];
    if ($record === [] || (string) ($record['source_device_id'] ?? '') !== $deviceId
        || (string) ($record['status'] ?? '') !== 'queued_source') {
        jsonResponse(['ok' => false, 'error' => 'Reconciliation upload is unavailable.'], 404);
    }
    $requestedPath = normalizeRelativePath((string) ($record['path'] ?? ''));
    if (fc_catalog_path_is_tombstoned($requestedPath, loadJsonMap($tombstonesFile))) {
        $record['status'] = 'cancelled';
        $record['error'] = 'Path was globally deleted before upload.';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => false, 'error' => $record['error']], 410);
    }
    if (!fc_generation_allows($requestedPath, $deviceId, loadJsonMap($generationsFile))) {
        jsonResponse(['ok' => false, 'error' => 'Source is not authorized for this item generation.'], 410);
    }
    $expectedSize = max(0, (int) ($record['expected_size'] ?? 0));
    $expectedMtime = max(0, (int) ($record['expected_mtime'] ?? 0));
    $contentLength = (int) ($_SERVER['CONTENT_LENGTH'] ?? 0);
    $clientHash = strtolower(headerValue('X-FreeCloud-SHA256'));
    if ($contentLength !== $expectedSize || preg_match('/^[a-f0-9]{64}$/', $clientHash) !== 1) {
        jsonResponse(['ok' => false, 'error' => 'Upload size or SHA-256 metadata is invalid.'], 422);
    }
    $part = $reconcileRoot . '/' . $jobId . '.part';
    $input = fopen('php://input', 'rb');
    $output = fopen($part, 'wb');
    if ($input === false || $output === false) {
        @unlink($part);
        jsonResponse(['ok' => false, 'error' => 'Could not create reconciliation staging file.'], 500);
    }
    $written = stream_copy_to_stream($input, $output);
    fclose($input);
    fclose($output);
    $actualHash = is_file($part) ? (hash_file('sha256', $part) ?: '') : '';
    if ($written === false || (int) $written !== $expectedSize || !hash_equals($clientHash, $actualHash)) {
        @unlink($part);
        jsonResponse(['ok' => false, 'error' => 'Uploaded bytes failed size or SHA-256 verification.'], 422);
    }

    $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
    $path = normalizeRelativePath((string) ($record['path'] ?? ''));
    $entry = isset($entries[$path]) && is_array($entries[$path]) ? $entries[$path] : [];
    $sourceLocation = $entry === [] ? null : locationForDevice($entry, $deviceId);
    if ($sourceLocation === null || (int) ($sourceLocation['size'] ?? -1) !== $expectedSize
        || abs((int) ($sourceLocation['mtime'] ?? 0) - $expectedMtime) > 2) {
        @unlink($part);
        $record['status'] = 'conflict';
        $record['error'] = 'Source metadata changed during transfer.';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => false, 'error' => $record['error']], 409);
    }
    if (!persistLocationHash($entries, $path, $deviceId, $actualHash, $expectedSize, $expectedMtime)
        || !saveVerifiedLocationHashes($filesFile, $entries, [$path => [$deviceId => $actualHash]])) {
        @unlink($part);
        jsonResponse(['ok' => false, 'error' => 'Could not persist the verified source hash.'], 500);
    }
    $ready = $reconcileRoot . '/' . $jobId . '.bin';
    if (!@rename($part, $ready)) {
        @unlink($part);
        jsonResponse(['ok' => false, 'error' => 'Could not finalize reconciliation staging file.'], 500);
    }
    @chmod($ready, 0600);
    $record['status'] = 'staged';
    $record['hash'] = $actualHash;
    $record['actual_size'] = $expectedSize;
    $record['updated_at'] = time();
    saveJsonFile($recordPath, $record);
    jsonResponse(['ok' => true, 'job' => $record]);
}

if ($action === 'commit_reconciliation') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'commit_reconciliation requires POST.'], 405);
    }
    $payload = requestPayload();
    $jobId = transferId((string) ($payload['job_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $record = $jobId !== '' ? loadJsonMap($recordPath) : [];
    $part = $reconcileRoot . '/' . $jobId . '.bin';
    if ($record === [] || (string) ($record['source_device_id'] ?? '') !== $deviceId
        || (string) ($record['status'] ?? '') !== 'staged' || !is_file($part)) {
        jsonResponse(['ok' => false, 'error' => 'Reconciliation commit is unavailable.'], 404);
    }
    if (!(bool) ($payload['source_unchanged'] ?? false)) {
        @unlink($part);
        $record['status'] = 'conflict';
        $record['error'] = 'Source changed during transfer; destination was not overwritten.';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => true, 'job' => $record]);
    }
    $path = normalizeRelativePath((string) ($record['path'] ?? ''));
    if (fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile))) {
        @unlink($part);
        $record['status'] = 'cancelled';
        $record['error'] = 'Path was globally deleted before commit.';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => false, 'error' => $record['error']], 410);
    }
    if ((string) ($record['destination_device_id'] ?? '') !== 'web_server') {
        $record['status'] = 'ready';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => true, 'job' => $record]);
    }
    $expectedSize = max(0, (int) ($record['expected_size'] ?? 0));
    $expectedMtime = max(0, (int) ($record['expected_mtime'] ?? 0));
    $actualHash = strtolower((string) ($record['hash'] ?? ''));
    if (filesize($part) !== $expectedSize || !hash_equals($actualHash, hash_file('sha256', $part) ?: '')) {
        @unlink($part);
        jsonResponse(['ok' => false, 'error' => 'Staged reconciliation bytes failed verification.'], 422);
    }
    $destination = resolveExistingPath($realStorageRoot, $path);
    $plannedDestination = $record['destination_facts'] ?? null;
    if (($plannedDestination === null && $destination !== null)
        || ($plannedDestination !== null && ($destination === null || !is_file($destination)
            || filesize($destination) !== (int) ($plannedDestination['size'] ?? -1)
            || abs((int) filemtime($destination) - (int) ($plannedDestination['mtime'] ?? 0)) > 2))) {
        @unlink($part);
        $record['status'] = 'conflict';
        $record['error'] = 'Web Server destination changed while transfer was pending.';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => false, 'error' => $record['error']], 409);
    }
    $target = pathToAbsolute($realStorageRoot, $path);
    $targetDir = dirname($target);
    if (!is_dir($targetDir) && !mkdir($targetDir, 0775, true) && !is_dir($targetDir)) {
        @unlink($part);
        jsonResponse(['ok' => false, 'error' => 'Could not create Web Server destination folder.'], 500);
    }
    $targetPart = $targetDir . '/.' . basename($target) . '.freecloud-part-' . $jobId;
    if (!@rename($part, $targetPart) || !@rename($targetPart, $target)) {
        @unlink($part);
        @unlink($targetPart);
        jsonResponse(['ok' => false, 'error' => 'Could not atomically install Web Server destination.'], 500);
    }
    @touch($target, $expectedMtime);
    $webEntries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
    if (persistLocationHash($webEntries, $path, 'web_server', $actualHash, $expectedSize, $expectedMtime)) {
        saveVerifiedLocationHashes($filesFile, $webEntries, [$path => ['web_server' => $actualHash]]);
    }
    $record['status'] = 'complete';
    $record['updated_at'] = time();
    saveJsonFile($recordPath, $record);
    saveAcceptedVersion($path, ['size' => $expectedSize, 'mtime' => $expectedMtime, 'hash' => $actualHash], $acceptedVersionsFile);
    $generations = loadJsonMap($generationsFile);
    $tombstones = loadJsonMap($tombstonesFile);
    $generation = fc_generation_for_path($path, $generations);
    if ($generation !== null) {
        fc_authorize_generation_device((string) ($generation['generation_id'] ?? ''), 'web_server', $generations, $tombstones);
        saveJsonFile($generationsFile, $generations);
        saveJsonFile($tombstonesFile, $tombstones);
    }
    jsonResponse(['ok' => true, 'job' => $record]);
}

if ($action === 'ack_reconciliation') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'ack_reconciliation requires POST.'], 405);
    }
    $payload = requestPayload();
    $jobId = transferId((string) ($payload['job_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $record = $jobId !== '' ? loadJsonMap($recordPath) : [];
    if ($record === [] || (string) ($record['destination_device_id'] ?? '') !== $deviceId) {
        jsonResponse(['ok' => false, 'error' => 'Unknown reconciliation job.'], 404);
    }
    $status = (string) ($payload['status'] ?? 'failed');
    if (($record['kind'] ?? '') === 'global_delete') {
        if (!in_array($status, ['deleted', 'failed'], true)) {
            jsonResponse(['ok' => false, 'error' => 'Invalid global-delete acknowledgment.'], 400);
        }
        if ($status === 'deleted') {
            if (!fc_apply_global_delete_ack($record, $deviceId, $filesFile, $tombstonesFile, $recordPath)) {
                jsonResponse(['ok' => false, 'error' => 'Could not persist global-delete acknowledgment.'], 500);
            }
            $remainingFiles = loadJsonMap($filesFile);
            pruneUnreferencedCatalogEntries($remainingFiles, $realStorageRoot, $reconcileRoot);
            saveJsonFile($filesFile, $remainingFiles);
            jsonResponse(['ok' => true, 'job' => loadJsonMap($recordPath)]);
        }
        $record['status'] = 'failed';
        $record['error'] = substr(trim((string) ($payload['error'] ?? 'Device could not delete the path.')), 0, 500);
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => true, 'job' => $record]);
    }
    if (!in_array($status, ['installed', 'removed', 'conflict', 'failed'], true)) {
        jsonResponse(['ok' => false, 'error' => 'Invalid reconciliation status.'], 400);
    }
    $record['status'] = $status;
    $record['error'] = substr(trim((string) ($payload['error'] ?? '')), 0, 500);
    $record['updated_at'] = time();
    if (!saveJsonFile($recordPath, $record)) {
        jsonResponse(['ok' => false, 'error' => 'Could not acknowledge reconciliation job.'], 500);
    }
    if ($status === 'installed' && normalizeDeviceId($deviceId) !== 'web_server') {
        $facts = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
        if (isset($facts[(string) ($record['path'] ?? '')]) && is_array($facts[(string) $record['path']])) {
            persistLocationHash($facts, (string) $record['path'], $deviceId, (string) ($record['hash'] ?? ''),
                (int) ($record['expected_size'] ?? -1), (int) ($record['expected_mtime'] ?? 0));
            saveVerifiedLocationHashes($filesFile, $facts, [(string) $record['path'] => [$deviceId => (string) ($record['hash'] ?? '')]]);
        }
    }
    if ($status === 'installed') {
        $generations = loadJsonMap($generationsFile);
        $tombstones = loadJsonMap($tombstonesFile);
        $generation = fc_generation_for_path(normalizeRelativePath((string) ($record['path'] ?? '')), $generations);
        if ($generation !== null) {
            fc_authorize_generation_device((string) ($generation['generation_id'] ?? ''), $deviceId, $generations, $tombstones);
            saveJsonFile($generationsFile, $generations);
            saveJsonFile($tombstonesFile, $tombstones);
            foreach ($tombstones as $oldTombstone) {
                if (!is_array($oldTombstone)
                    || (string) ($oldTombstone['superseded_by_generation'] ?? '') !== (string) ($generation['generation_id'] ?? '')) continue;
                $oldDeleteId = fc_global_delete_job_id((string) ($oldTombstone['tombstone_id'] ?? ''), $deviceId);
                $oldDeletePath = $reconcileRoot . '/' . $oldDeleteId . '.json';
                $oldDelete = loadJsonMap($oldDeletePath);
                if (($oldDelete['kind'] ?? '') === 'global_delete') {
                    $oldDelete['status'] = 'cancelled';
                    $oldDelete['error'] = 'New generation installed after old-generation cleanup.';
                    $oldDelete['updated_at'] = time();
                    saveJsonFile($oldDeletePath, $oldDelete);
                }
            }
        }
    }
    if ($status === 'removed') {
        $remainingFiles = loadJsonMap($filesFile);
        pruneUnreferencedCatalogEntries($remainingFiles, $realStorageRoot, $reconcileRoot);
        saveJsonFile($filesFile, $remainingFiles);
    }
    jsonResponse(['ok' => true, 'job' => $record]);
}

if ($action === 'report_location_hash') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') jsonResponse(['ok' => false, 'error' => 'report_location_hash requires POST.'], 405);
    $payload = requestPayload();
    $jobId = transferId((string) ($payload['job_id'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    $recordPath = $reconcileRoot . '/' . $jobId . '.json';
    $record = $jobId !== '' ? loadJsonMap($recordPath) : [];
    if (($record['kind'] ?? '') !== 'hash_location' || ($record['status'] ?? '') !== 'queued_hash'
        || normalizeDeviceId((string) ($record['source_device_id'] ?? '')) !== $deviceId) {
        jsonResponse(['ok' => false, 'error' => 'Unknown or stale hash request.'], 404);
    }
    $reportedError = substr(trim((string) ($payload['error'] ?? '')), 0, 500);
    if ($reportedError !== '') {
        $record['status'] = 'failed';
        $record['error'] = $reportedError;
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        foreach (glob($reconcileRoot . '/pending-remove-*.json') ?: [] as $pendingPath) {
            $pending = loadJsonMap($pendingPath);
            if (in_array(($pending['status'] ?? ''), ['hashing', 'waiting'], true) && in_array($jobId, (array) ($pending['hash_job_ids'] ?? []), true)) {
                finishPendingRemoval($pending, $pendingPath, $filesFile, $desiredRulesFile, $rulesFile,
                    $devicesFile, $reconcileRoot, $realStorageRoot);
            }
        }
        jsonResponse(['ok' => true, 'hash_verified' => false]);
    }
    $hash = strtolower((string) ($payload['hash'] ?? ''));
    $files = loadJsonMap($filesFile);
    $entries = normalizedMetadataEntries($files, $realStorageRoot);
    if (!persistLocationHash($entries, (string) $record['path'], $deviceId, $hash,
        (int) ($record['expected_size'] ?? -1), (int) ($record['expected_mtime'] ?? 0))) {
        $record['status'] = 'failed';
        $record['error'] = 'Provider copy changed or could not be verified; removal remains blocked.';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        jsonResponse(['ok' => false, 'error' => $record['error']], 409);
    }
    if (!saveVerifiedLocationHashes($filesFile, $entries, [(string) $record['path'] => [$deviceId => $hash]])) jsonResponse(['ok' => false, 'error' => 'Could not save verified location hash.'], 500);
    $record['status'] = 'complete';
    $record['hash'] = $hash;
    $record['updated_at'] = time();
    saveJsonFile($recordPath, $record);
    foreach (glob($reconcileRoot . '/pending-remove-*.json') ?: [] as $pendingPath) {
        $pending = loadJsonMap($pendingPath);
        if (in_array(($pending['status'] ?? ''), ['hashing', 'waiting'], true) && in_array($jobId, (array) ($pending['hash_job_ids'] ?? []), true)) {
            finishPendingRemoval($pending, $pendingPath, $filesFile, $desiredRulesFile, $rulesFile,
                $devicesFile, $reconcileRoot, $realStorageRoot);
        }
    }
    jsonResponse(['ok' => true, 'hash_verified' => true]);
}

if ($action === 'metadata_manifest') {
    $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
    $entries = addDesiredLocations($entries, loadJsonMap($desiredRulesFile), loadJsonMap($rulesFile), loadJsonMap($devicesFile));
    $entries = addReconciliationState($entries, $reconcileRoot);
    jsonResponse(['ok' => true, 'entries' => array_values($entries)]);
}

if ($action === 'metadata_list') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    $prefix = $path === '' ? '' : $path . '/';
    $storedDirectory = [];
    foreach (loadJsonMap($filesFile) as $storedPath => $storedEntry) {
        if (!is_array($storedEntry)) continue;
        $clean = normalizeRelativePath((string) ($storedEntry['path'] ?? $storedPath));
        if ($clean === '') continue;
        if ($path !== '') {
            if (!str_starts_with($clean, $prefix)) continue;
            $remainder = substr($clean, strlen($prefix));
        } else {
            $remainder = $clean;
        }
        if ($remainder !== '' && !str_contains($remainder, '/')) $storedDirectory[$clean] = $storedEntry;
    }
    // Normalize only this folder's stored metadata. The Web filesystem is
    // comparatively small and remains authoritative for its physical entries.
    $entries = normalizedMetadataEntries($storedDirectory, $realStorageRoot, $path);
    $listed = [];
    foreach (metadataListDirectory($entries, $path) as $entry) {
        $entryPath = normalizeRelativePath((string) ($entry['path'] ?? ''));
        if ($entryPath !== '') $listed[$entryPath] = $entry;
    }
    $listed = addDesiredLocations($listed, loadJsonMap($desiredRulesFile), loadJsonMap($rulesFile), loadJsonMap($devicesFile));
    $listed = addReconciliationState($listed, $reconcileRoot);
    jsonResponse([
        'ok' => true,
        'path' => $path,
        'entries' => array_values($listed),
    ]);
}

if ($action === 'get_placement') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    $tombstoned = fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile));
    $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
    $entry = isset($entries[$path]) && is_array($entries[$path]) ? $entries[$path] : null;
    $desiredRules = loadJsonMap($desiredRulesFile);
    $syncRules = loadJsonMap($rulesFile);
    $devices = loadJsonMap($devicesFile);
    jsonResponse([
        'ok' => true,
        'path' => $path,
        'actual_locations' => array_values((array) ($entry['locations'] ?? [])),
        'desired_locations' => $tombstoned ? [] : desiredLocationsForPath($path, $desiredRules, $syncRules, $devices, $entry),
        'entry' => $entry,
    ]);
}

if ($action === 'adopt_existing_locations') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'adopt_existing_locations requires POST.'], 405);
    }
    $completed = loadJsonMap($placementMigrationFile);
    $wasComplete = (bool) ($completed['complete'] ?? false);
    $devices = loadJsonMap($devicesFile);
    $desiredRules = loadJsonMap($desiredRulesFile);
    $syncRules = loadJsonMap($rulesFile);
    $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
    $entries = addReconciliationState($entries, $reconcileRoot);
    $providers = ['web_server'];
    foreach ($devices as $deviceId => $device) {
        if (is_array($device)) $providers[] = normalizeDeviceId((string) $deviceId);
    }
    $added = 0;
    foreach ($providers as $deviceId) {
        if ($deviceId === '') continue;
        $groups = [];
        foreach ($entries as $entryPath => $entry) {
            if (!is_array($entry)) continue;
            $path = normalizeRelativePath((string) ($entry['path'] ?? $entryPath));
            if ($path === '') continue;
            $top = explode('/', $path, 2)[0];
            $groups[$top][$path] = $entry;
        }
        foreach ($groups as $top => $group) {
            $rootEntry = $group[$top] ?? null;
            if (!is_array($rootEntry) || effectiveDesiredRule($top, $deviceId, $desiredRules, $syncRules) !== null) continue;
            $rootIsDir = (string) ($rootEntry['type'] ?? '') === 'dir';
            $allCovered = true;
            foreach ($group as $path => $entry) {
                if (effectiveDesiredRule($path, $deviceId, $desiredRules, $syncRules) !== null) continue;
                $location = locationForDevice($entry, $deviceId);
                $stale = array_map('strval', (array) ($entry['stale_locations'] ?? []));
                if ($location === null || in_array($deviceId, $stale, true)) {
                    $allCovered = false;
                    break;
                }
            }
            if (!$allCovered) {
                if ($rootIsDir) continue;
                $location = locationForDevice($rootEntry, $deviceId);
                if ($location === null || in_array($deviceId, array_map('strval', (array) ($rootEntry['stale_locations'] ?? [])), true)) continue;
            }
            $desiredRules[$deviceId][] = [
                'path' => $top, 'desired' => true, 'recursive' => $rootIsDir,
                'updated_at' => time(), 'source' => 'adopted_actual_v1',
            ];
            $added++;
        }
    }
    if ($added > 0 && is_file($desiredRulesFile)) {
        @copy($desiredRulesFile, $desiredRulesFile . '.pre-adoption-' . time() . '.json');
    }
    foreach ($desiredRules as &$rules) {
        if (is_array($rules)) usort($rules, static fn(array $a, array $b): int => strnatcasecmp((string) ($a['path'] ?? ''), (string) ($b['path'] ?? '')));
    }
    unset($rules);
    if (($added > 0 && !saveJsonFile($desiredRulesFile, $desiredRules))
        || !saveJsonFile($placementMigrationFile, [
            'complete' => true,
            'adopted_rules' => (int) ($completed['adopted_rules'] ?? 0) + $added,
            'completed_at' => time(),
        ])) {
        jsonResponse(['ok' => false, 'error' => 'Could not complete placement adoption.'], 500);
    }
    jsonResponse(['ok' => true, 'adopted_rules' => $added, 'already_complete' => $wasComplete && $added === 0]);
}

if ($action === 'placement_rules') {
    jsonResponse([
        'ok' => true,
        'desired_rules' => loadJsonMap($desiredRulesFile),
        'sync_rules' => loadJsonMap($rulesFile),
    ]);
}

if ($action === 'placement_ownership') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'placement_ownership requires POST.'], 405);
    }
    $payload = requestPayload();
    if (!array_key_exists('paths', $payload) || !is_array($payload['paths'])) {
        jsonResponse(['ok' => false, 'error' => 'placement_ownership requires a paths array.'], 400);
    }
    $paths = $payload['paths'];
    if (count($paths) > 20000) {
        jsonResponse(['ok' => false, 'error' => 'Too many paths in placement ownership request.'], 413);
    }
    $files = loadJsonMap($filesFile);
    $desiredRules = loadJsonMap($desiredRulesFile);
    $syncRules = loadJsonMap($rulesFile);
    $devices = loadJsonMap($devicesFile);
    $tombstones = loadJsonMap($tombstonesFile);
    $providerIds = array_map('strval', array_keys($devices));
    $providerIds[] = 'web_server';
    $managedPaths = [];
    foreach ($paths as $rawPath) {
        $path = normalizeRelativePath((string) $rawPath);
        if ($path === '') continue;
        $entry = isset($files[$path]) && is_array($files[$path]) ? $files[$path] : [];
        $managed = fc_catalog_path_is_tombstoned($path, $tombstones)
            || in_array((string) ($entry['placement_default'] ?? ''), ['all', 'source_only'], true)
            || (bool) ($entry['deleted'] ?? false);
        if (!$managed) {
            foreach ($providerIds as $providerId) {
                if (effectiveDesiredRule($path, normalizeDeviceId($providerId), $desiredRules, $syncRules) !== null) {
                    $managed = true;
                    break;
                }
            }
        }
        if ($managed) $managedPaths[] = $path;
    }
    jsonResponse(['ok' => true, 'managed_paths' => array_values(array_unique($managedPaths))]);
}

if ($action === 'set_desired_location') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'set_desired_location requires POST.'], 405);
    }
    $payload = requestPayload();
    $path = normalizeRelativePath((string) ($payload['path'] ?? ''));
    $deviceId = normalizeDeviceId((string) ($payload['device_id'] ?? ''));
    if (fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile))) {
        jsonResponse(['ok' => false, 'error' => 'This path has been globally deleted.'], 410);
    }
    $devices = loadJsonMap($devicesFile);
    if ($deviceId === '' || ($deviceId !== 'web_server' && !isset($devices[$deviceId]))) {
        jsonResponse(['ok' => false, 'error' => 'Unknown placement provider.'], 404);
    }
    $catalogForPlacement = loadJsonMap($filesFile);
    $placementEntry = isset($catalogForPlacement[$path]) && is_array($catalogForPlacement[$path])
        ? $catalogForPlacement[$path] : null;
    $rule = [
        'path' => $path,
        'desired' => (bool) ($payload['desired'] ?? true),
        'recursive' => (bool) ($payload['recursive'] ?? true),
        'updated_at' => time(),
    ];
    $allRules = loadJsonMap($desiredRulesFile);
    $deviceRules = [];
    foreach ((array) ($allRules[$deviceId] ?? []) as $existingRule) {
        if (is_array($existingRule) && normalizeRelativePath((string) ($existingRule['path'] ?? '')) !== $path) {
            $deviceRules[] = $existingRule;
        }
    }
    $deviceRules[] = $rule;
    usort($deviceRules, static fn(array $a, array $b): int => strnatcasecmp((string) ($a['path'] ?? ''), (string) ($b['path'] ?? '')));
    $allRules[$deviceId] = $deviceRules;
    $syncRules = loadJsonMap($rulesFile);
    // Persist the user's desired state before attempting any physical removal.
    // Safety failures below only block deletion, never the preference update.
    if (!saveJsonFile($desiredRulesFile, $allRules)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save desired placement.'], 500);
    }
    if ((bool) $rule['desired']) {
        foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
            $record = loadJsonMap($recordPath);
            $recordPathValue = normalizeRelativePath((string) ($record['path'] ?? ''));
            $recordDevice = normalizeDeviceId((string) (($record['kind'] ?? '') === 'pending_removal'
                ? ($record['device_id'] ?? '') : ($record['destination_device_id'] ?? '')));
            $kind = (string) ($record['kind'] ?? '');
            if ($recordDevice !== $deviceId || $recordPathValue === ''
                || !in_array((string) ($record['status'] ?? ''), ['hashing', 'waiting', 'blocked', 'failed', 'queued_remove'], true)) continue;
            $recordRecursive = (bool) ($record['recursive'] ?? false);
            $statePath = $recordPathValue;
            $covered = $recordPathValue === $path || ((bool) $rule['recursive'] && str_starts_with($recordPathValue, $path . '/'));
            if (!$covered && $recordRecursive && str_starts_with($path, $recordPathValue . '/')) {
                $covered = true;
                $statePath = $path;
            }
            if (!$covered) continue;
            $stateEntry = isset($catalogForPlacement[$statePath]) && is_array($catalogForPlacement[$statePath])
                ? $catalogForPlacement[$statePath] : $placementEntry;
            $effective = desiredLocationsForPath($statePath, $allRules, $syncRules, $devices, $stateEntry);
            $effectiveIds = array_map(static fn(array $item): string => (string) ($item['device_id'] ?? ''), $effective);
            if (!in_array($deviceId, $effectiveIds, true)) continue;
            $record['status'] = 'canceled';
            $record['error'] = 'Placement is desired again on this device.';
            $record['updated_at'] = time();
            saveJsonFile($recordPath, $record);
        }
    }
    $plannedJob = null;
    if ((bool) $rule['desired'] && !(bool) $rule['recursive']) {
        $entries = normalizedMetadataEntries(loadJsonMap($filesFile), $realStorageRoot);
        if (isset($entries[$path]) && is_array($entries[$path])) {
            $desiredNow = desiredLocationsForPath($path, $allRules, loadJsonMap($rulesFile), $devices, $placementEntry);
            $desiredIds = array_map(static fn(array $item): string => (string) ($item['device_id'] ?? ''), $desiredNow);
            $plannedJobs = planMultiProviderReconciliation(
                $entries[$path], $devices, $desiredIds, $realStorageRoot, $reconcileRoot, $acceptedVersionsFile
            );
            $plannedJob = $plannedJobs[0] ?? null;
        }
    } elseif (!(bool) $rule['desired']) {
        $requestId = substr(hash('sha256', "pending-remove\0{$path}\0{$deviceId}\0" . (int) $rule['recursive']), 0, 32);
        $requestPath = $reconcileRoot . '/pending-remove-' . $requestId . '.json';
        $pending = loadJsonMap($requestPath);
        if ($pending === [] || in_array((string) ($pending['status'] ?? ''), ['complete', 'cancelled'], true)) {
            $pending = [
                'job_id' => $requestId, 'kind' => 'pending_removal', 'path' => $path,
                'device_id' => $deviceId, 'recursive' => (bool) $rule['recursive'],
                'hash_job_ids' => [], 'status' => 'blocked',
                'created_at' => time(), 'updated_at' => time(), 'next_retry_at' => 0, 'error' => '',
            ];
        }
        if (!saveJsonFile($requestPath, $pending)) {
            jsonResponse(['ok' => true, 'path' => $path, 'device_id' => $deviceId, 'rule' => $rule,
                'desired_locations' => desiredLocationsForPath($path, $allRules, $syncRules, $devices, $placementEntry),
                'removal_status' => 'blocked', 'message' => 'Desired placement saved; removal could not be queued.']);
        }
        retryBlockedRemoval($pending, $requestPath, $filesFile, $desiredRulesFile, $rulesFile, $devicesFile,
            $reconcileRoot, $realStorageRoot);
        $pending = loadJsonMap($requestPath);
        $remainingFiles = loadJsonMap($filesFile);
        pruneUnreferencedCatalogEntries($remainingFiles, $realStorageRoot, $reconcileRoot);
        saveJsonFile($filesFile, $remainingFiles);
        $plannedJob = !empty($pending['removal_job_id'])
            ? loadJsonMap($reconcileRoot . '/' . (string) $pending['removal_job_id'] . '.json') : null;
        jsonResponse([
            'ok' => true, 'path' => $path, 'device_id' => $deviceId, 'rule' => $rule,
            'desired_locations' => desiredLocationsForPath($path, $allRules, $syncRules, $devices, $placementEntry),
            'pending' => in_array((string) ($pending['status'] ?? ''), ['hashing', 'waiting', 'blocked'], true),
            'removal_status' => (string) ($pending['status'] ?? 'blocked'),
            'message' => (string) (($pending['error'] ?? '') !== '' ? $pending['error'] : match ((string) ($pending['status'] ?? '')) {
                'hashing' => 'Verifying file copies before removal.',
                'waiting' => 'Waiting for an offline device to verify its copy.',
                'complete' => 'Removal queued or complete.',
                default => '',
            }),
            'reconciliation_job' => $plannedJob,
        ]);
    }
    jsonResponse([
        'ok' => true,
        'path' => $path,
        'device_id' => $deviceId,
        'rule' => $rule,
        'desired_locations' => desiredLocationsForPath($path, $allRules, loadJsonMap($rulesFile), $devices, $placementEntry),
        'reconciliation_job' => $plannedJob,
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

if ($action === 'recreate_path') {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        jsonResponse(['ok' => false, 'error' => 'recreate_path requires POST.'], 405);
    }
    $payload = requestPayload();
    $path = normalizeRelativePath((string) ($payload['path'] ?? ''));
    $sourceId = normalizeDeviceId((string) ($payload['source_device_id'] ?? ''));
    $destinationId = normalizeDeviceId((string) ($payload['desired_device_id'] ?? ''));
    $recursive = (bool) ($payload['recursive'] ?? false);
    if ($path === '' || $sourceId === '' || $destinationId === '') {
        jsonResponse(['ok' => false, 'error' => 'Path, source device, and desired device are required.'], 400);
    }
    $devices = loadJsonMap($devicesFile);
    if (($sourceId !== 'web_server' && !isset($devices[$sourceId]))
        || ($destinationId !== 'web_server' && !isset($devices[$destinationId]))) {
        jsonResponse(['ok' => false, 'error' => 'Unknown recreation provider.'], 404);
    }
    $tombstones = loadJsonMap($tombstonesFile);
    $tombstoneId = '';
    $tombstone = null;
    foreach ($tombstones as $id => $candidate) {
        if (is_array($candidate) && ($candidate['status'] ?? '') !== 'superseded'
            && empty($candidate['superseded_by_generation'])
            && (string) ($candidate['path'] ?? '') === $path) {
            $tombstoneId = (string) $id;
            $tombstone = $candidate;
            break;
        }
    }
    if (!is_array($tombstone) || (bool) ($tombstone['recursive'] ?? false) !== $recursive) {
        jsonResponse(['ok' => false, 'error' => 'Recreation must target the exact deleted file/folder and scope.'], 409);
    }
    if ($sourceId !== 'web_server' && (int) ($devices[$sourceId]['last_seen_at'] ?? 0) < time() - 300) {
        jsonResponse(['ok' => false, 'error' => 'The source device must reconnect and publish a fresh manifest before recreation.'], 409);
    }

    // The source must have positively reported the item after deletion.
    // An offline device's stale copy cannot authorize a new generation.
    $files = loadJsonMap($filesFile);
    $sourceEntries = [];
    foreach ($files as $entryPath => $entry) {
        if (!is_array($entry)) continue;
        $cleanPath = normalizeRelativePath((string) ($entry['path'] ?? $entryPath));
        if (!fc_scope_contains($cleanPath, $path, $recursive)) continue;
        $sourceLocation = locationForDevice($entry, $sourceId);
        if ($sourceLocation === null || (int) ($sourceLocation['updated_at'] ?? 0) < (int) ($tombstone['deleted_at'] ?? 0)) continue;
        $sourceEntries[$cleanPath] = $entry;
    }
    if (!isset($sourceEntries[$path])) {
        jsonResponse(['ok' => false, 'error' => 'No fresh source copy exists at this path. Publish the current device manifest, then retry.'], 409);
    }

    $generationId = bin2hex(random_bytes(16));
    $generation = [
        'generation_id' => $generationId,
        'path' => $path,
        'recursive' => $recursive,
        'source_device_id' => $sourceId,
        'authorized_device_ids' => [$sourceId],
        'created_at' => time(),
    ];
    $generations = loadJsonMap($generationsFile);
    $generations[$generationId] = $generation;
    foreach ($sourceEntries as $entryPath => $sourceEntry) {
        $sourceLocation = locationForDevice($sourceEntry, $sourceId);
        if ($sourceLocation !== null) $sourceLocation['hash'] = '';
        $sourceEntry['generation_id'] = $generationId;
        $sourceEntry['deleted'] = false;
        $sourceEntry['version'] = 1;
        $sourceEntry['hash'] = '';
        $sourceEntry['locations'] = $sourceLocation === null ? [] : [$sourceLocation];
        $files[$entryPath] = $sourceEntry;
    }

    // Drop stale placement rules within the new generation, then persist only
    // the source and the destination the user explicitly selected.
    $desiredRules = loadJsonMap($desiredRulesFile);
    $providerIds = array_values(array_unique(array_merge(['web_server'], array_map('strval', array_keys($devices)))));
    foreach ($providerIds as $providerId) {
        $kept = [];
        foreach ((array) ($desiredRules[$providerId] ?? []) as $rule) {
            if (!is_array($rule)) continue;
            $rulePath = normalizeRelativePath((string) ($rule['path'] ?? ''));
            if ($rulePath === $path || ($recursive && str_starts_with($rulePath, $path . '/'))) continue;
            $kept[] = $rule;
        }
        $desiredRules[$providerId] = $kept;
    }
    $selectedProviders = array_fill_keys(array_unique([$sourceId, $destinationId]), true);
    foreach ($providerIds as $providerId) {
        $desiredRules[$providerId][] = [
            'path' => $path, 'desired' => isset($selectedProviders[$providerId]), 'recursive' => $recursive,
            'updated_at' => time(), 'source' => 'explicit_recreation', 'generation_id' => $generationId,
        ];
    }

    $tombstone['status'] = 'superseded';
    $tombstone['superseded_by_generation'] = $generationId;
    $tombstone['updated_at'] = time();
    $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
    foreach (array_unique([$sourceId, 'web_server']) as $alreadyCurrent) {
        if (in_array($alreadyCurrent, (array) ($tombstone['target_device_ids'] ?? []), true)) $acked[] = $alreadyCurrent;
    }
    $tombstone['acknowledged_device_ids'] = array_values(array_unique($acked));
    $tombstones[$tombstoneId] = $tombstone;
    foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
        $record = loadJsonMap($recordPath);
        if (($record['kind'] ?? '') === 'global_delete'
            && (string) ($record['tombstone_id'] ?? '') === $tombstoneId
            && in_array((string) ($record['destination_device_id'] ?? ''), [$sourceId, 'web_server'], true)) {
            $record['status'] = 'cancelled';
            $record['error'] = 'Superseded by an explicitly authorized new generation.';
            $record['updated_at'] = time();
            saveJsonFile($recordPath, $record);
        } elseif (($record['kind'] ?? '') !== 'global_delete'
            && fc_scope_contains(normalizeRelativePath((string) ($record['path'] ?? '')), $path, $recursive)
            && !in_array((string) ($record['status'] ?? ''), ['complete', 'cancelled', 'canceled'], true)) {
            $record['status'] = 'cancelled';
            $record['error'] = 'Superseded by an explicitly authorized new generation.';
            $record['updated_at'] = time();
            saveJsonFile($recordPath, $record);
            @unlink($reconcileRoot . '/' . (string) ($record['job_id'] ?? '') . '.bin');
        }
    }
    $accepted = loadJsonMap($acceptedVersionsFile);
    foreach (array_keys($accepted) as $acceptedPath) {
        if (fc_scope_contains((string) $acceptedPath, $path, $recursive)) unset($accepted[$acceptedPath]);
    }
    if (!saveJsonFile($filesFile, $files) || !saveJsonFile($desiredRulesFile, $desiredRules)
        || !saveJsonFile($tombstonesFile, $tombstones) || !saveJsonFile($generationsFile, $generations)
        || !saveJsonFile($acceptedVersionsFile, $accepted)) {
        jsonResponse(['ok' => false, 'error' => 'Could not persist the new item generation safely.'], 500);
    }
    jsonResponse(['ok' => true, 'path' => $path, 'generation_id' => $generationId,
        'source_device_id' => $sourceId, 'desired_device_id' => $destinationId,
        'entries' => count($sourceEntries)]);
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
    // Absence only proves a provider no longer has bytes when its caller
    // explicitly completed a full, authoritative scan. Older/partial clients
    // remain additive and cannot erase location claims by omission.
    $completeManifest = ($payload['complete'] ?? false) === true;
    $stored = loadJsonMap($filesFile);
    $devices = loadJsonMap($devicesFile);
    $tombstones = loadJsonMap($tombstonesFile);
    $generations = loadJsonMap($generationsFile);
    $manifestSyncRules = loadJsonMap($rulesFile);
    $device = isset($devices[$deviceId]) && is_array($devices[$deviceId]) ? $devices[$deviceId] : [];
    if ($device === []) {
        jsonResponse(['ok' => false, 'error' => 'Unknown device. Register it first.'], 404);
    }
    $deviceName = trim((string) ($device['name'] ?? $deviceId));
    $locationKind = deviceLocationKind($device);
    $now = time();
    $seenPaths = [];
    $staleGenerationPaths = [];
    $verifiedTransferHashes = [];
    foreach (glob($reconcileRoot . '/*.json') ?: [] as $transferRecordPath) {
        $transferRecord = loadJsonMap($transferRecordPath);
        $transferPath = normalizeRelativePath((string) ($transferRecord['path'] ?? ''));
        if ($transferPath !== '' && ($transferRecord['status'] ?? '') === 'installed'
            && normalizeDeviceId((string) ($transferRecord['destination_device_id'] ?? '')) === $deviceId
            && preg_match('/^[a-f0-9]{64}$/i', (string) ($transferRecord['hash'] ?? '')) === 1) {
            $verifiedTransferHashes[$transferPath] = $transferRecord;
        }
    }

    // A file modified after a global delete is new content that reuses the
    // name, not a stale copy. Record it (and the folders leading to it) as
    // recreated so it is catalogued normally and never re-targeted for
    // deletion. Older copies stay under the tombstone, so stale devices
    // still cannot resurrect deleted items.
    foreach ($entries as $entry) {
        if (!is_array($entry)) continue;
        $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
        if ($path === '' || !fc_generation_allows($path, $deviceId, $generations)) continue;
        $covering = fc_tombstone_for_path($path, $tombstones);
        if ($covering === null || !fc_entry_postdates_tombstone($entry, $covering)) continue;
        $operationId = (string) ($covering['tombstone_id'] ?? '');
        if ($operationId === '' || !isset($tombstones[$operationId])) continue;
        $root = (string) ($covering['path'] ?? '');
        $parts = explode('/', $path);
        for ($index = count($parts); $index >= 1; $index--) {
            $itemPath = implode('/', array_slice($parts, 0, $index));
            if (!fc_scope_contains($itemPath, $root, (bool) ($covering['recursive'] ?? false))) break;
            $tombstones[$operationId]['recreated_paths'][$itemPath] ??= ['at' => $now, 'device_id' => $deviceId];
        }
        $tombstones[$operationId]['updated_at'] = $now;
    }

    foreach ($entries as $entry) {
        if (!is_array($entry)) {
            continue;
        }
        $path = normalizeRelativePath((string) ($entry['path'] ?? ''));
        if ($path === '') {
            continue;
        }
        // Positive reports from providers not authorized for the current
        // generation are stale old copies, not new Catalog state.
        if (!fc_generation_allows($path, $deviceId, $generations)) {
            $staleGenerationPaths[$path] = true;
            continue;
        }
        $seenPaths[$path] = true;
        $isRecreated = false;
        foreach ($tombstones as $tombstone) {
            if (is_array($tombstone) && fc_tombstone_recreated($tombstone, $path)) {
                $isRecreated = true;
                break;
            }
        }
        $matchingTombstone = fc_tombstone_for_path($path, $tombstones);
        $generation = fc_generation_for_path($path, $generations);
        if ($matchingTombstone !== null) {
            $operationId = (string) ($matchingTombstone['tombstone_id'] ?? '');
            if ($operationId !== '' && isset($tombstones[$operationId])) {
                $targets = array_map('strval', (array) ($tombstones[$operationId]['target_device_ids'] ?? []));
                if (!in_array($deviceId, $targets, true)) $targets[] = $deviceId;
                $tombstones[$operationId]['target_device_ids'] = $targets;
                $tombstones[$operationId]['acknowledged_device_ids'] = array_values(array_diff(
                    array_map('strval', (array) ($tombstones[$operationId]['acknowledged_device_ids'] ?? [])), [$deviceId]
                ));
                $tombstones[$operationId]['status'] = 'pending';
                $tombstones[$operationId]['updated_at'] = time();
            }
        }
        $existing = isset($stored[$path]) && is_array($stored[$path]) ? $stored[$path] : [];
        $incomingHash = (string) ($entry['hash'] ?? '');
        if (preg_match('/^[a-f0-9]{64}$/i', $incomingHash) !== 1) {
            foreach ((array) ($existing['locations'] ?? []) as $oldLocation) {
                if (is_array($oldLocation) && normalizeDeviceId((string) ($oldLocation['device_id'] ?? '')) === $deviceId
                    && (int) ($oldLocation['size'] ?? -1) === (int) ($entry['size'] ?? 0)
                    && abs((int) ($oldLocation['mtime'] ?? 0) - (int) ($entry['mtime'] ?? 0)) <= 2
                    && preg_match('/^[a-f0-9]{64}$/i', (string) ($oldLocation['hash'] ?? '')) === 1) {
                    $incomingHash = strtolower((string) $oldLocation['hash']);
                    break;
                }
            }
        }
        $transferFacts = $verifiedTransferHashes[$path] ?? null;
        if ($incomingHash === '' && is_array($transferFacts)
            && (int) ($transferFacts['expected_size'] ?? -1) === (int) ($entry['size'] ?? 0)
            && abs((int) ($transferFacts['expected_mtime'] ?? 0) - (int) ($entry['mtime'] ?? 0)) <= 2) {
            $incomingHash = strtolower((string) $transferFacts['hash']);
        }
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
            'kind' => $locationKind,
            'device_id' => $deviceId,
            'name' => $deviceName,
            'available' => true,
            'updated_at' => $now,
            'size' => max(0, (int) ($entry['size'] ?? 0)),
            'mtime' => max(0, (int) ($entry['mtime'] ?? 0)),
            'hash' => $incomingHash,
        ];
        $locations = uniqueLocations($locations);
        $placementFields = [];
        if (array_key_exists('placement_default', $existing)) {
            $placementFields['placement_default'] = (string) $existing['placement_default'];
            $placementFields['placement_source_device_id'] = normalizeDeviceId(
                (string) ($existing['placement_source_device_id'] ?? '')
            );
        } elseif (($existing === [] || $isRecreated) && $matchingTombstone === null) {
            $placementFields = newManifestPlacementDefault(
                $path, $deviceId, $manifestSyncRules, $devices
            );
        }
        $stored[$path] = [
            'path' => $path,
            'name' => (string) ($entry['name'] ?? basename($path)),
            'type' => (string) ($entry['type'] ?? 'file'),
            'size' => max(0, (int) ($entry['size'] ?? 0)),
            'mtime' => max(0, (int) ($entry['mtime'] ?? 0)),
            'hash' => $incomingHash,
            'version' => $version,
            'deleted' => $matchingTombstone !== null
                || (!$isRecreated && $generation === null && (bool) ($existing['deleted'] ?? false)),
            'generation_id' => (string) ($generation['generation_id'] ?? ($existing['generation_id'] ?? '')),
            'locations' => $locations,
        ] + $placementFields;
    }

    if ($completeManifest) {
        foreach ($stored as $path => $entry) {
            if (!is_array($entry) || isset($seenPaths[(string) $path])) {
                continue;
            }
            if (isset($staleGenerationPaths[(string) $path])) continue;
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
            // A missing device copy means only that this location disappeared.
            // It is not a global delete and must not create a tombstone.
        }
    }

    // Hold this device's recursive folder delete while it reports recreated
    // content inside that folder: existing clients delete the whole folder
    // without inspecting it, which would destroy the new files. Its stale
    // copies stay hidden by the tombstone in the meantime.
    foreach ($tombstones as $operationId => $tombstone) {
        if (!is_array($tombstone) || !is_array($tombstone['recreated_paths'] ?? null)) continue;
        $holdsRecreated = false;
        foreach ($entries as $entry) {
            if (!is_array($entry) || strtolower((string) ($entry['type'] ?? 'file')) === 'dir') continue;
            if (fc_tombstone_recreated($tombstone, normalizeRelativePath((string) ($entry['path'] ?? '')))) {
                $holdsRecreated = true;
                break;
            }
        }
        if ($holdsRecreated) {
            $tombstones[$operationId]['held_device_ids'][$deviceId] ??= ['since' => $now];
        } elseif ($completeManifest && isset($tombstone['held_device_ids'][$deviceId])) {
            unset($tombstones[$operationId]['held_device_ids'][$deviceId]);
        }
    }

    if (!saveJsonFile($filesFile, $stored)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save file metadata.'], 500);
    }
    if (!saveJsonFile($tombstonesFile, $tombstones)) {
        jsonResponse(['ok' => false, 'error' => 'Could not save tombstone metadata.'], 500);
    }
    foreach ($tombstones as $tombstone) {
        if (!is_array($tombstone)) continue;
        $targets = array_map('strval', (array) ($tombstone['target_device_ids'] ?? []));
        $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
        if (in_array($deviceId, $targets, true) && !in_array($deviceId, $acked, true)) {
            fc_make_global_delete_job($tombstone, $deviceId, $reconcileRoot);
        }
    }
    if ($completeManifest) {
        // A complete snapshot can confirm an out-of-band removal while an OFF
        // request waits for hash proof. Retire that work once no target copies
        // remain, rather than leaving a stale Removing... marker forever.
        foreach (glob($reconcileRoot . '/pending-remove-*.json') ?: [] as $pendingPath) {
            $pending = loadJsonMap($pendingPath);
            if (($pending['kind'] ?? '') !== 'pending_removal'
                || !in_array(($pending['status'] ?? ''), ['hashing', 'waiting', 'blocked'], true)
                || normalizeDeviceId((string) ($pending['device_id'] ?? '')) !== $deviceId) {
                continue;
            }
            $removePath = normalizeRelativePath((string) ($pending['path'] ?? ''));
            $recursive = (bool) ($pending['recursive'] ?? false);
            $targetStillPresent = false;
            foreach ($stored as $storedPath => $storedEntry) {
                if (!is_array($storedEntry)) continue;
                $clean = normalizeRelativePath((string) ($storedEntry['path'] ?? $storedPath));
                if (($clean === $removePath || ($recursive && str_starts_with($clean, $removePath . '/')))
                    && locationForDevice($storedEntry, $deviceId) !== null) {
                    $targetStillPresent = true;
                    break;
                }
            }
            if ($targetStillPresent) continue;
            foreach ((array) ($pending['hash_job_ids'] ?? []) as $hashJobId) {
                $hashJobId = transferId((string) $hashJobId);
                if ($hashJobId === '') continue;
                $hashJobPath = transferRecordPath($reconcileRoot, $hashJobId);
                $hashJob = loadJsonMap($hashJobPath);
                if (($hashJob['status'] ?? '') === 'queued_hash') {
                    $hashJob['status'] = 'cancelled';
                    $hashJob['error'] = 'Target absence confirmed by complete provider manifest.';
                    $hashJob['updated_at'] = time();
                    saveJsonFile($hashJobPath, $hashJob);
                }
            }
            $pending['status'] = 'complete';
            $pending['error'] = 'Target absence confirmed by complete provider manifest.';
            $pending['updated_at'] = time();
            saveJsonFile($pendingPath, $pending);
        }
        foreach ($tombstones as $tombstoneId => &$tombstone) {
            if (!is_array($tombstone)) continue;
            $targets = array_map('strval', (array) ($tombstone['target_device_ids'] ?? []));
            $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
            if (!in_array($deviceId, $targets, true) || in_array($deviceId, $acked, true)) continue;
            $reportedOldGenerationPath = false;
            foreach (array_keys($staleGenerationPaths) as $reportedPath) {
                if (fc_scope_contains($reportedPath, (string) ($tombstone['path'] ?? ''), (bool) ($tombstone['recursive'] ?? false))) {
                    $reportedOldGenerationPath = true;
                    break;
                }
            }
            if ($reportedOldGenerationPath) continue;
            $present = false;
            foreach ($stored as $storedPath => $storedEntry) {
                if (!is_array($storedEntry) || !fc_scope_contains(
                    normalizeRelativePath((string) ($storedEntry['path'] ?? $storedPath)),
                    (string) ($tombstone['path'] ?? ''), (bool) ($tombstone['recursive'] ?? false)
                )) continue;
                if (locationForDevice($storedEntry, $deviceId) !== null) { $present = true; break; }
            }
            if ($present) continue;
            $tombstone['acknowledged_device_ids'] = array_values(array_unique(array_merge($acked, [$deviceId])));
            $tombstone['updated_at'] = time();
            $targetsRemaining = array_diff($targets, $tombstone['acknowledged_device_ids']);
            $tombstone['status'] = $targetsRemaining === [] ? 'complete' : 'pending';
            $jobId = fc_global_delete_job_id((string) $tombstoneId, $deviceId);
            $jobPath = $reconcileRoot . '/' . $jobId . '.json';
            $job = loadJsonMap($jobPath);
            if ($job !== []) {
                $job['status'] = 'complete';
                $job['error'] = 'Complete manifest confirms the deleted path is absent.';
                $job['updated_at'] = time();
                saveJsonFile($jobPath, $job);
            }
        }
        unset($tombstone);
        saveJsonFile($tombstonesFile, $tombstones);
        pruneUnreferencedCatalogEntries($stored, $realStorageRoot, $reconcileRoot);
        saveJsonFile($filesFile, $stored);
    }
    foreach (glob($reconcileRoot . '/*.json') ?: [] as $recordPath) {
        $record = loadJsonMap($recordPath);
        $path = normalizeRelativePath((string) ($record['path'] ?? ''));
        if ((string) ($record['destination_device_id'] ?? '') === $deviceId
            && (string) ($record['status'] ?? '') === 'removed') {
            $stillSeen = isset($seenPaths[$path]);
            if (!$stillSeen && (bool) ($record['recursive'] ?? false)) {
                foreach (array_keys($seenPaths) as $seenPath) {
                    if (str_starts_with((string) $seenPath, $path . '/')) { $stillSeen = true; break; }
                }
            }
            if (!$stillSeen) {
                $record['status'] = 'complete';
                $record['updated_at'] = time();
                saveJsonFile($recordPath, $record);
            }
            continue;
        }
        if ((string) ($record['destination_device_id'] ?? '') !== $deviceId
            || (string) ($record['status'] ?? '') !== 'installed' || !isset($seenPaths[$path])) {
            continue;
        }
        $record['status'] = 'complete';
        $record['updated_at'] = time();
        saveJsonFile($recordPath, $record);
        saveAcceptedVersion($path, [
            'size' => (int) ($record['expected_size'] ?? 0),
            'mtime' => (int) ($record['expected_mtime'] ?? 0),
            'hash' => (string) ($record['hash'] ?? ''),
        ], $acceptedVersionsFile);
        @unlink($reconcileRoot . '/' . (string) ($record['job_id'] ?? '') . '.bin');
    }
    jsonResponse(['ok' => true, 'published' => count($seenPaths), 'complete' => $completeManifest]);
}

if ($action === 'manifest') {
    jsonResponse(['ok' => true, 'entries' => listRecursive($realStorageRoot)]);
}

if ($action === 'list') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    $tombstones = loadJsonMap($tombstonesFile);
    $entries = array_values(array_filter(listDirectory($realStorageRoot, $path), static function (array $entry) use ($tombstones): bool {
        return !fc_catalog_path_is_tombstoned((string) ($entry['path'] ?? ''), $tombstones);
    }));
    jsonResponse(['ok' => true, 'path' => $path, 'entries' => $entries]);
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
    if (fc_catalog_path_is_tombstoned(normalizeRelativePath($fromPath), loadJsonMap($tombstonesFile))
        || fc_catalog_path_is_tombstoned(normalizeRelativePath($toPath), loadJsonMap($tombstonesFile))) {
        jsonResponse(['ok' => false, 'error' => 'This path has been globally deleted.'], 410);
    }
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
    if (fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile))) {
        jsonResponse(['ok' => false, 'error' => 'This path has been globally deleted.'], 410);
    }
    $target = pathToAbsolute($realStorageRoot, $path);
    if (!isWithinRoot($target, $realStorageRoot)) {
        jsonResponse(['ok' => false, 'error' => 'Path rejected.'], 400);
    }
    if (!is_dir($target) && !mkdir($target, 0775, true) && !is_dir($target)) {
        jsonResponse(['ok' => false, 'error' => 'Could not create folder.'], 500);
    }
    if (!recordNewWebPlacementDefaults($path)) {
        jsonResponse(['ok' => false, 'error' => 'Folder was created but its placement metadata could not be saved.'], 500);
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
    if (fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile))) {
        jsonResponse(['ok' => false, 'error' => 'This path has been globally deleted.'], 410);
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

    // Never open the live destination for writing: receive into a temp
    // sibling, verify, then rename. A failed or short upload keeps the
    // existing file byte-for-byte.
    $mtime = (int) ($_GET['mtime'] ?? $_POST['mtime'] ?? 0);
    $expectedHash = headerValue('X-FreeCloud-SHA256');
    if (isset($_FILES['file']) && is_uploaded_file((string) $_FILES['file']['tmp_name'])) {
        $temp = fc_upload_temp_path($destination);
        if (!move_uploaded_file((string) $_FILES['file']['tmp_name'], $temp)) {
            @unlink($temp);
            jsonResponse(['ok' => false, 'error' => 'Could not save upload.'], 500);
        }
        $size = filesize($temp);
        $hash = $expectedHash !== '' ? (hash_file('sha256', $temp) ?: '') : '';
        if ($size === false || $size !== (int) ($_FILES['file']['size'] ?? -1)
            || ($expectedHash !== '' && !hash_equals(strtolower($expectedHash), $hash))) {
            @unlink($temp);
            jsonResponse(['ok' => false, 'error' => 'Upload failed verification; the existing file was kept.'], 422);
        }
        acquireCatalogLock();
        if (!fc_commit_upload_temp($temp, $destination, $mtime)) {
            @unlink($temp);
            jsonResponse(['ok' => false, 'error' => 'Could not save upload.'], 500);
        }
    } else {
        $input = fopen('php://input', 'rb');
        if ($input === false) {
            jsonResponse(['ok' => false, 'error' => 'Could not open upload stream.'], 500);
        }
        $result = fc_install_stream_atomically($destination, $input, declaredContentLength(), $expectedHash, $mtime);
        fclose($input);
        if (!$result['ok']) {
            jsonResponse(['ok' => false, 'error' => $result['error']], $result['status']);
        }
        acquireCatalogLock();
    }
    if (!recordNewWebPlacementDefaults($path)) {
        jsonResponse(['ok' => false, 'error' => 'Upload completed but its placement metadata could not be saved.'], 500);
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
    if (fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile))) {
        jsonResponse(['ok' => false, 'error' => 'This path has been globally deleted.'], 410);
    }
    $destination = pathToAbsolute($realStorageRoot, $path);
    $destinationDir = dirname($destination);
    if (!is_dir($destinationDir) && !mkdir($destinationDir, 0775, true) && !is_dir($destinationDir)) {
        jsonResponse(['ok' => false, 'error' => 'Could not create target folder.'], 500);
    }
    if (!isWithinRoot($destination, $realStorageRoot)) {
        jsonResponse(['ok' => false, 'error' => 'Path rejected.'], 400);
    }
    $input = fopen('php://input', 'rb');
    if ($input === false) {
        jsonResponse(['ok' => false, 'error' => 'Could not save file.'], 500);
    }
    $result = fc_install_stream_atomically($destination, $input, declaredContentLength(), headerValue('X-FreeCloud-SHA256'), 0);
    fclose($input);
    if (!$result['ok']) {
        jsonResponse(['ok' => false, 'error' => $result['error']], $result['status']);
    }
    acquireCatalogLock();
    if (!recordNewWebPlacementDefaults($path)) {
        jsonResponse(['ok' => false, 'error' => 'File was saved but its placement metadata could not be saved.'], 500);
    }
    jsonResponse(['ok' => true, 'path' => $path]);
}

if ($action === 'download') {
    $path = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    if (fc_catalog_path_is_tombstoned($path, loadJsonMap($tombstonesFile))) {
        http_response_code(410);
        exit('This path has been globally deleted.');
    }
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
