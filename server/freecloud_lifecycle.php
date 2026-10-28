<?php
declare(strict_types=1);

/** Shared lifecycle helpers used by the API and authenticated web UI. */

// Bump when an entry file starts depending on a new helper below, so a
// partially deployed server answers with a clear 503 instead of a fatal 500.
const FC_LIFECYCLE_VERSION = 2;

// Metadata whose loss would drop placements, locations or tombstones. These
// must never be treated as empty when present but unreadable.
const FC_CRITICAL_METADATA = [
    'files.json', 'tombstones.json', 'generations.json', 'desired_locations.json',
    'sync_rules.json', 'devices.json', 'accepted_versions.json', 'placement_adoption_v1.json',
];

final class FcCorruptMetadataException extends RuntimeException
{
}

function fc_scope_contains(string $path, string $root, bool $recursive): bool
{
    return $path === $root || ($recursive && str_starts_with($path, $root . '/'));
}

function fc_is_valid_utf8(string $value): bool
{
    return $value === '' || preg_match('//u', $value) === 1;
}

/** True for FreeCloud's own in-flight temp files, which are never user content. */
function fc_is_internal_temp_name(string $name): bool
{
    return preg_match('/^\..+\.freecloud-(?:part|upload)-[A-Za-z0-9]+$/', $name) === 1;
}

/**
 * Decode a JSON metadata file. A missing file is empty; a present file that
 * cannot be decoded (truncated, zero bytes, invalid UTF-8) throws for critical
 * catalog files so callers can never save an empty map over real metadata.
 */
function fc_read_json_file(string $path, ?bool $critical = null): array
{
    if (!is_file($path)) return [];
    $raw = @file_get_contents($path);
    $decoded = is_string($raw) ? json_decode($raw, true) : null;
    if (is_array($decoded)) return $decoded;
    $critical ??= in_array(basename($path), FC_CRITICAL_METADATA, true);
    if ($critical) {
        throw new FcCorruptMetadataException('FreeCloud metadata file is unreadable: ' . basename($path));
    }
    return [];
}

function fc_read_json_map(string $path): array
{
    return fc_read_json_file($path);
}

/** Encode metadata, or return null rather than an empty string on failure. */
function fc_encode_json(array $data): ?string
{
    $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
    if (!is_string($json) || $json === '') {
        error_log('FreeCloud refused to write metadata: ' . json_last_error_msg());
        return null;
    }
    return $json;
}

/** Write bytes to a temp sibling, verify the length, then rename over $path. */
function fc_write_file_atomic(string $path, string $contents, ?int $mode = 0600): bool
{
    $dir = dirname($path);
    if (!is_dir($dir) && !mkdir($dir, 0775, true) && !is_dir($dir)) return false;
    $tmp = $path . '.' . bin2hex(random_bytes(6)) . '.tmp';
    $written = @file_put_contents($tmp, $contents, LOCK_EX);
    if ($written !== strlen($contents)) {
        @unlink($tmp);
        return false;
    }
    if ($mode !== null) @chmod($tmp, $mode);
    if (!@rename($tmp, $path)) {
        @unlink($tmp);
        return false;
    }
    return true;
}

function fc_upload_temp_path(string $destination): string
{
    return dirname($destination) . '/.' . basename($destination) . '.freecloud-upload-' . bin2hex(random_bytes(8));
}

/** Rename a verified temp file over $destination, keeping an existing file's mode. */
function fc_commit_upload_temp(string $temp, string $destination, int $mtime): bool
{
    if (is_dir($destination)) return false;
    if (is_file($destination)) {
        $perms = @fileperms($destination);
        if ($perms !== false) @chmod($temp, $perms & 0777);
    }
    if ($mtime > 0) @touch($temp, $mtime);
    return @rename($temp, $destination);
}

/**
 * Stream an upload body into a temp sibling of $destination and only rename it
 * into place once the byte count (and SHA-256, when supplied) is verified.
 * The destination is never opened for writing, so a failed, short, or
 * interrupted upload leaves any existing file untouched.
 *
 * @param resource $input
 * @return array{ok: bool, status: int, error: string, size: int, hash: string}
 */
function fc_install_stream_atomically(string $destination, $input, ?int $expectedSize, string $expectedSha256, int $mtime): array
{
    $fail = static fn(int $status, string $error): array => ['ok' => false, 'status' => $status, 'error' => $error, 'size' => 0, 'hash' => ''];
    $expectedSha256 = strtolower(trim($expectedSha256));
    if ($expectedSha256 !== '' && preg_match('/^[a-f0-9]{64}$/', $expectedSha256) !== 1) {
        return $fail(422, 'X-FreeCloud-SHA256 must be a hex SHA-256 digest.');
    }
    $temp = fc_upload_temp_path($destination);
    $output = @fopen($temp, 'xb');
    if ($output === false) return $fail(500, 'Could not create a temporary upload file.');
    $context = hash_init('sha256');
    $written = 0;
    $ok = true;
    while (!feof($input)) {
        $chunk = fread($input, 1024 * 1024);
        if ($chunk === false) { $ok = false; break; }
        if ($chunk === '') continue;
        $length = strlen($chunk);
        if (fwrite($output, $chunk) !== $length) { $ok = false; break; }
        hash_update($context, $chunk);
        $written += $length;
        if ($expectedSize !== null && $written > $expectedSize) break;
    }
    if ($ok && !fflush($output)) $ok = false;
    if ($ok && function_exists('fsync')) @fsync($output);
    fclose($output);
    $hash = hash_final($context);
    if (!$ok) {
        @unlink($temp);
        return $fail(507, 'Upload could not be written completely (disk full or I/O error); the existing file was kept.');
    }
    if ($expectedSize !== null && $written !== $expectedSize) {
        @unlink($temp);
        return $fail(422, "Upload ended early or was oversized ({$written} of {$expectedSize} bytes); the existing file was kept.");
    }
    if ($expectedSha256 !== '' && !hash_equals($expectedSha256, $hash)) {
        @unlink($temp);
        return $fail(422, 'Uploaded bytes did not match X-FreeCloud-SHA256; the existing file was kept.');
    }
    if (!fc_commit_upload_temp($temp, $destination, $mtime)) {
        @unlink($temp);
        return $fail(500, 'Could not install the verified upload.');
    }
    return ['ok' => true, 'status' => 200, 'error' => '', 'size' => $written, 'hash' => $hash];
}

/**
 * Exact paths inside a tombstone scope that hold content created after the
 * deletion. They belong to a new life of the name, not to the deleted item.
 */
function fc_tombstone_recreated(array $tombstone, string $path): bool
{
    $recreated = $tombstone['recreated_paths'] ?? null;
    return is_array($recreated) && isset($recreated[$path]);
}

function fc_tombstone_for_path(string $path, array $tombstones): ?array
{
    foreach ($tombstones as $tombstone) {
        if (!is_array($tombstone)) continue;
        // Keep superseded tombstones as history, but let a deliberately
        // recreated generation own this path from this point forward.
        if (($tombstone['status'] ?? '') === 'superseded'
            || trim((string) ($tombstone['superseded_by_generation'] ?? '')) !== '') continue;
        $root = (string) ($tombstone['path'] ?? '');
        if ($root !== '' && fc_scope_contains($path, $root, (bool) ($tombstone['recursive'] ?? false))
            && !fc_tombstone_recreated($tombstone, $path)) {
            return $tombstone;
        }
    }
    return null;
}

/**
 * Classify a reported manifest entry under a live tombstone. A file whose
 * modification time is later than the deletion is new content that reuses the
 * name; anything else is treated as a stale pre-deletion copy. The 2-second
 * tolerance matches the rest of the catalog's mtime comparisons.
 */
function fc_entry_postdates_tombstone(array $entry, array $tombstone): bool
{
    $deletedAt = (int) ($tombstone['deleted_at'] ?? 0);
    return $deletedAt > 0
        && strtolower((string) ($entry['type'] ?? 'file')) !== 'dir'
        && (int) ($entry['mtime'] ?? 0) > $deletedAt + 2;
}

function fc_generation_for_path(string $path, array $generations): ?array
{
    $match = null;
    $matchLength = -1;
    foreach ($generations as $generation) {
        if (!is_array($generation)) continue;
        $root = (string) ($generation['path'] ?? '');
        if ($root === '' || !fc_scope_contains($path, $root, (bool) ($generation['recursive'] ?? false))) continue;
        if (strlen($root) > $matchLength) {
            $match = $generation;
            $matchLength = strlen($root);
        }
    }
    return $match;
}

function fc_generation_allows(string $path, string $deviceId, array $generations): bool
{
    $generation = fc_generation_for_path($path, $generations);
    return $generation === null || in_array($deviceId, array_map('strval', (array) ($generation['authorized_device_ids'] ?? [])), true);
}

function fc_generation_cleanup_pending(string $path, string $deviceId, array $tombstones): bool
{
    foreach ($tombstones as $tombstone) {
        if (!is_array($tombstone) || ($tombstone['status'] ?? '') !== 'superseded') continue;
        if (!fc_scope_contains($path, (string) ($tombstone['path'] ?? ''), (bool) ($tombstone['recursive'] ?? false))) continue;
        $targets = array_map('strval', (array) ($tombstone['target_device_ids'] ?? []));
        $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
        if (in_array($deviceId, $targets, true) && !in_array($deviceId, $acked, true)) return true;
    }
    return false;
}

function fc_authorize_generation_device(string $generationId, string $deviceId, array &$generations, array &$tombstones): bool
{
    if (!isset($generations[$generationId]) || !is_array($generations[$generationId])) return false;
    $allowed = array_map('strval', (array) ($generations[$generationId]['authorized_device_ids'] ?? []));
    if (!in_array($deviceId, $allowed, true)) $allowed[] = $deviceId;
    $generations[$generationId]['authorized_device_ids'] = $allowed;
    foreach ($tombstones as &$tombstone) {
        if (!is_array($tombstone) || (string) ($tombstone['superseded_by_generation'] ?? '') !== $generationId) continue;
        $targets = array_map('strval', (array) ($tombstone['target_device_ids'] ?? []));
        $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
        if (in_array($deviceId, $targets, true) && !in_array($deviceId, $acked, true)) {
            $acked[] = $deviceId;
            $tombstone['acknowledged_device_ids'] = $acked;
            $tombstone['updated_at'] = time();
        }
    }
    unset($tombstone);
    return true;
}

function fc_global_delete_job_id(string $tombstoneId, string $deviceId): string
{
    return substr(hash('sha256', "global-delete-job\0{$tombstoneId}\0{$deviceId}"), 0, 32);
}

function fc_make_global_delete_job(array $tombstone, string $deviceId, string $reconcileRoot): array
{
    $id = fc_global_delete_job_id((string) $tombstone['tombstone_id'], $deviceId);
    $recordPath = rtrim($reconcileRoot, '/') . '/' . $id . '.json';
    $old = fc_read_json_file($recordPath, false);
    $now = time();
    // A device that also holds content created after the deletion inside this
    // scope must not receive a recursive folder delete: older clients remove
    // the whole folder without inspecting what is inside it.
    $held = is_array($tombstone['held_device_ids'][$deviceId] ?? null);
    $job = [
        'job_id' => $id,
        'kind' => 'global_delete',
        'tombstone_id' => (string) $tombstone['tombstone_id'],
        'path' => (string) $tombstone['path'],
        'recursive' => (bool) ($tombstone['recursive'] ?? false),
        'deleted_at' => (int) ($tombstone['deleted_at'] ?? 0),
        'source_device_id' => '',
        'destination_device_id' => $deviceId,
        'status' => $held ? 'held_new_content' : 'queued_delete',
        'created_at' => (int) ($old['created_at'] ?? $now),
        'updated_at' => $now,
        'error' => $held ? 'Held: this device holds new content created after the deletion inside this folder.' : '',
    ];
    if (!is_dir($reconcileRoot) && !mkdir($reconcileRoot, 0775, true) && !is_dir($reconcileRoot)) {
        throw new RuntimeException('Could not create global-delete work directory.');
    }
    $json = fc_encode_json($job);
    if ($json === null || !fc_write_file_atomic($recordPath, $json)) {
        throw new RuntimeException('Could not persist global-delete work.');
    }
    return $job;
}

function fc_begin_global_delete(
    string $path,
    bool $recursive,
    array $files,
    array $webEntries,
    array $devices,
    array $tombstones,
    string $reconcileRoot,
    string $transferRoot = ''
): array {
    $found = false;
    $rootIsDirectory = false;
    foreach ($webEntries as $entryPath => $entry) {
        if (is_array($entry) && fc_scope_contains((string) ($entry['path'] ?? $entryPath), $path, $recursive)) {
            $found = true;
            if ((string) ($entry['path'] ?? $entryPath) === $path && strtolower((string) ($entry['type'] ?? '')) === 'dir') {
                $rootIsDirectory = true;
            }
            if (!isset($files[$entryPath]) || !is_array($files[$entryPath])) {
                $files[$entryPath] = $entry;
            } else {
                $files[$entryPath]['locations'] = array_values(array_merge(
                    (array) ($files[$entryPath]['locations'] ?? []), (array) ($entry['locations'] ?? [])
                ));
            }
        }
    }
    foreach ($files as $entryPath => $entry) {
        if (is_array($entry) && fc_scope_contains((string) ($entry['path'] ?? $entryPath), $path, $recursive)) {
            $found = true;
            if ((string) ($entry['path'] ?? $entryPath) === $path && strtolower((string) ($entry['type'] ?? '')) === 'dir') {
                $rootIsDirectory = true;
            }
        }
    }
    if (!$found) throw new RuntimeException('Logical item not found.');
    if ($rootIsDirectory) $recursive = true;

    $tombstone = null;
    foreach ($tombstones as $existing) {
        if (is_array($existing) && ($existing['status'] ?? '') !== 'superseded'
            && empty($existing['superseded_by_generation']) && (string) ($existing['path'] ?? '') === $path
            && (bool) ($existing['recursive'] ?? false) === $recursive) {
            $tombstone = $existing;
            break;
        }
    }
    if ($tombstone !== null) {
        // Deleting the same name again is a new, explicit decision covering
        // everything there now, including content recreated since last time.
        $tombstone['deleted_at'] = time();
        unset($tombstone['recreated_paths'], $tombstone['held_device_ids']);
    }
    if ($tombstone === null) {
        $now = time();
        $id = substr(hash('sha256', "global-delete\0{$path}\0" . (int) $recursive . "\0{$now}\0" . bin2hex(random_bytes(8))), 0, 32);
        $targets = array_values(array_unique(array_merge(['web_server'], array_map('strval', array_keys($devices)))));
        $tombstone = [
            'tombstone_id' => $id,
            'kind' => 'global_delete',
            'path' => $path,
            'recursive' => $recursive,
            'deleted_at' => $now,
            'created_at' => $now,
            'updated_at' => $now,
            'target_device_ids' => $targets,
            'acknowledged_device_ids' => [],
            'status' => 'pending',
        ];
        $tombstones[$id] = $tombstone;
    }

    // A repeated delete also covers bytes observed again after the original
    // request (for example, a stale manifest or an out-of-band Web file).
    $observedProviders = [];
    foreach ($files as $entryPath => $entry) {
        if (!is_array($entry) || !fc_scope_contains((string) ($entry['path'] ?? $entryPath), $path, $recursive)) continue;
        foreach ((array) ($entry['locations'] ?? []) as $location) {
            if (is_array($location)) {
                $provider = (string) ($location['device_id'] ?? '');
                if ($provider !== '') $observedProviders[$provider] = true;
            }
        }
    }
    $targets = array_values(array_unique(array_merge(
        (array) ($tombstone['target_device_ids'] ?? []),
        ['web_server'], array_map('strval', array_keys($devices)), array_keys($observedProviders)
    )));
    $acked = array_values(array_diff(
        array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? [])),
        array_keys($observedProviders)
    ));
    $tombstone['target_device_ids'] = $targets;
    $tombstone['acknowledged_device_ids'] = $acked;
    $tombstone['status'] = 'pending';
    $tombstone['updated_at'] = time();
    $tombstones[(string) $tombstone['tombstone_id']] = $tombstone;

    foreach ($files as $entryPath => &$entry) {
        if (is_array($entry) && fc_scope_contains((string) ($entry['path'] ?? $entryPath), $path, $recursive)) {
            $entry['deleted'] = true;
        }
    }
    unset($entry);

    // Stop transfers that could otherwise install/expose bytes after deletion.
    foreach (array_filter([$reconcileRoot, $transferRoot]) as $workRoot) {
        foreach (glob(rtrim($workRoot, '/') . '/*.json') ?: [] as $recordPath) {
            $record = fc_read_json_file($recordPath, false);
            if (!is_array($record) || !in_array((string) ($record['status'] ?? ''), ['queued', 'uploading', 'queued_source', 'staged', 'ready'], true)) continue;
            if (!fc_scope_contains((string) ($record['path'] ?? ''), $path, $recursive)) continue;
            $record['status'] = 'cancelled';
            $record['error'] = 'Path globally deleted.';
            $record['updated_at'] = time();
            fc_save_json_atomic($recordPath, $record);
            $jobId = (string) ($record['job_id'] ?? $record['transfer_id'] ?? '');
            if ($jobId !== '') { @unlink(rtrim($workRoot, '/') . '/' . $jobId . '.bin'); @unlink(rtrim($workRoot, '/') . '/' . $jobId . '.part'); }
        }
    }

    return ['files' => $files, 'tombstones' => $tombstones, 'tombstone' => $tombstone, 'jobs' => []];
}

function fc_ensure_global_delete_jobs(array $tombstone, string $reconcileRoot): array
{
    $jobs = [];
    foreach ((array) ($tombstone['target_device_ids'] ?? []) as $deviceId) {
        $deviceId = (string) $deviceId;
        $acked = array_map('strval', (array) ($tombstone['acknowledged_device_ids'] ?? []));
        if (in_array($deviceId, $acked, true)) continue;
        $jobs[$deviceId] = fc_make_global_delete_job($tombstone, $deviceId, $reconcileRoot);
    }
    return $jobs;
}

function fc_save_json_atomic(string $path, array $data): bool
{
    // Never replace metadata with an empty or partial file: an encoding
    // failure (for example a non-UTF-8 name) leaves the previous file intact.
    $json = fc_encode_json($data);
    return $json !== null && fc_write_file_atomic($path, $json, 0600);
}

function fc_apply_global_delete_ack(
    array $job,
    string $deviceId,
    string $filesFile,
    string $tombstonesFile,
    string $jobPath
): bool {
    try {
        $tombstones = fc_read_json_file($tombstonesFile, true);
        $files = fc_read_json_file($filesFile, true);
    } catch (FcCorruptMetadataException $error) {
        error_log('FreeCloud global-delete ack refused: ' . $error->getMessage());
        return false;
    }
    $tombstoneId = (string) ($job['tombstone_id'] ?? '');
    if (!isset($tombstones[$tombstoneId]) || !is_array($tombstones[$tombstoneId])) return false;
    $root = (string) ($job['path'] ?? '');
    $recursive = (bool) ($job['recursive'] ?? false);
    $superseded = ($tombstones[$tombstoneId]['status'] ?? '') === 'superseded'
        || !empty($tombstones[$tombstoneId]['superseded_by_generation']);
    if (!$superseded) {
        foreach ($files as $path => &$entry) {
            if (!is_array($entry) || !fc_scope_contains((string) ($entry['path'] ?? $path), $root, $recursive)) continue;
            // Content recreated after the deletion is not part of it.
            if (fc_tombstone_recreated($tombstones[$tombstoneId], (string) ($entry['path'] ?? $path))) continue;
            $entry['deleted'] = true;
            $entry['locations'] = array_values(array_filter((array) ($entry['locations'] ?? []), static function ($location) use ($deviceId): bool {
                return !is_array($location) || (string) ($location['device_id'] ?? '') !== $deviceId;
            }));
        }
        unset($entry);
    }
    if (!fc_save_json_atomic($filesFile, $files)) return false;
    $operation = $tombstones[$tombstoneId];
    $acked = array_values(array_unique(array_merge((array) ($operation['acknowledged_device_ids'] ?? []), [$deviceId])));
    $operation['acknowledged_device_ids'] = $acked;
    $operation['updated_at'] = time();
    $targets = array_values(array_unique((array) ($operation['target_device_ids'] ?? [])));
    if (!$superseded) $operation['status'] = array_diff($targets, $acked) === [] ? 'complete' : 'pending';
    $tombstones[$tombstoneId] = $operation;
    if (!fc_save_json_atomic($tombstonesFile, $tombstones)) return false;
    $job['status'] = 'complete';
    $job['error'] = '';
    $job['updated_at'] = time();
    return fc_save_json_atomic($jobPath, $job);
}

function fc_catalog_path_is_tombstoned(string $path, array $tombstones): bool
{
    return fc_tombstone_for_path($path, $tombstones) !== null;
}
