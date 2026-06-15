<?php
declare(strict_types=1);

$baseDir = __DIR__;
$sessionDir = $baseDir . '/freecloud_sessions';
if (!is_dir($sessionDir)) {
    mkdir($sessionDir, 0775, true);
}
if (!is_file($sessionDir . '/.htaccess')) {
    @file_put_contents($sessionDir . '/.htaccess', "Require all denied\nDeny from all\n", LOCK_EX);
}
session_save_path($sessionDir);
ini_set('session.cookie_httponly', '1');
ini_set('session.cookie_samesite', 'Strict');
if (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') {
    ini_set('session.cookie_secure', '1');
}
session_start();

$storageRoot = $baseDir . '/freecloud_files';
$configFile = $baseDir . '/config.json';

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

    $saved = file_put_contents($configFile, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX) !== false;
    if ($saved) {
        @chmod($configFile, 0600);
    }
    return $saved;
}

function authRequired(?array $config): bool
{
    return is_array($config) && (string) ($config['password_hash'] ?? '') !== '';
}

function isAuthed(?array $config): bool
{
    return !authRequired($config) || (bool) ($_SESSION['auth'] ?? false);
}

function redirectToDrive(string $path = '', string $message = '', string $type = 'success', string $sort = ''): never
{
    $query = [];
    if ($path !== '') {
        $query['path'] = $path;
    }
    if ($sort !== '') {
        $query['sort'] = $sort;
    }
    if ($message !== '') {
        $query['message'] = $message;
        $query['type'] = $type;
    }

    $location = 'freecloud.php';
    if ($query !== []) {
        $location .= '?' . http_build_query($query);
    }

    header('Location: ' . $location);
    exit;
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

function normalizeUploads(array $files): array
{
    if (!isset($files['name']) || !is_array($files['name'])) {
        return [];
    }

    $uploads = [];
    $count = count($files['name']);
    for ($index = 0; $index < $count; $index += 1) {
        $uploads[] = [
            'name' => (string) ($files['name'][$index] ?? ''),
            'tmp_name' => (string) ($files['tmp_name'][$index] ?? ''),
            'error' => (int) ($files['error'][$index] ?? UPLOAD_ERR_NO_FILE),
            'size' => (int) ($files['size'][$index] ?? 0),
        ];
    }

    return $uploads;
}

function uploadErrorMessage(int $error): string
{
    return match ($error) {
        UPLOAD_ERR_INI_SIZE, UPLOAD_ERR_FORM_SIZE => 'A file was too large for this server.',
        UPLOAD_ERR_PARTIAL => 'A file upload was interrupted.',
        UPLOAD_ERR_NO_TMP_DIR => 'The server is missing a temporary upload folder.',
        UPLOAD_ERR_CANT_WRITE => 'The server could not write an uploaded file.',
        UPLOAD_ERR_EXTENSION => 'A PHP extension stopped the upload.',
        default => 'A file could not be uploaded.',
    };
}

function formatBytes(int $bytes): string
{
    if ($bytes < 1024) {
        return $bytes . ' B';
    }

    $units = ['KB', 'MB', 'GB', 'TB'];
    $value = $bytes;
    $unitIndex = -1;
    while ($value >= 1024 && $unitIndex < count($units) - 1) {
        $value /= 1024;
        $unitIndex += 1;
    }

    return number_format($value, $value < 10 ? 1 : 0) . ' ' . $units[$unitIndex];
}

function isPreviewableImageFile(string $filename): bool
{
    return in_array(strtolower(pathinfo($filename, PATHINFO_EXTENSION)), ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp'], true);
}

function isPlayableVideoFile(string $filename): bool
{
    return in_array(strtolower(pathinfo($filename, PATHINFO_EXTENSION)), ['mp4', 'm4v', 'mov', 'webm', 'ogv', 'ogg'], true);
}

function isEditableFile(string $filename): bool
{
    return in_array(strtolower(pathinfo($filename, PATHINFO_EXTENSION)), [
        'txt', 'php', 'html', 'htm', 'css', 'js', 'json', 'md', 'csv', 'ini',
        'yaml', 'yml', 'xml', 'sh', 'conf', 'log', 'sql', 'env', 'htaccess',
    ], true);
}

function videoMimeType(string $filename): string
{
    return match (strtolower(pathinfo($filename, PATHINFO_EXTENSION))) {
        'mp4', 'm4v' => 'video/mp4',
        'mov' => 'video/quicktime',
        'webm' => 'video/webm',
        'ogv', 'ogg' => 'video/ogg',
        default => 'video/mp4',
    };
}

function listEntries(string $directory, string $displayPath, string $sort): array
{
    $items = array_values(array_filter(scandir($directory) ?: [], static function (string $item): bool {
        return $item !== '.' && $item !== '..' && $item !== '.htaccess';
    }));

    $entries = [];
    foreach ($items as $item) {
        $absolutePath = $directory . '/' . $item;
        $realPath = realpath($absolutePath);
        if ($realPath === false) {
            continue;
        }

        $isDir = is_dir($realPath);
        $relativePath = normalizeRelativePath($displayPath === '' ? $item : $displayPath . '/' . $item);
        $isImage = !$isDir && isPreviewableImageFile($item);
        $isVideo = !$isDir && isPlayableVideoFile($item);
        $previewUrl = ($isImage || $isVideo) ? 'freecloud_preview.php?path=' . rawurlencode($relativePath) : null;

        $entries[] = [
            'name' => $item,
            'path' => $relativePath,
            'isDir' => $isDir,
            'isImage' => $isImage,
            'isVideo' => $isVideo,
            'isEditable' => !$isDir && isEditableFile($item),
            'previewUrl' => $previewUrl,
            'videoMimeType' => $isVideo ? videoMimeType($item) : null,
            'size' => $isDir ? null : (filesize($realPath) ?: 0),
            'modifiedAt' => filemtime($realPath) ?: 0,
        ];
    }

    usort($entries, static function (array $a, array $b) use ($sort): int {
        if ($a['isDir'] !== $b['isDir']) {
            return $a['isDir'] ? -1 : 1;
        }

        return match ($sort) {
            'newest' => ((int) $b['modifiedAt'] <=> (int) $a['modifiedAt']) ?: strnatcasecmp($a['name'], $b['name']),
            'oldest' => ((int) $a['modifiedAt'] <=> (int) $b['modifiedAt']) ?: strnatcasecmp($a['name'], $b['name']),
            default => strnatcasecmp($a['name'], $b['name']),
        };
    });

    return $entries;
}

function buildBreadcrumbs(string $relativePath): array
{
    $crumbs = [['label' => 'Root', 'path' => '']];
    if ($relativePath === '') {
        return $crumbs;
    }

    $current = [];
    foreach (explode('/', $relativePath) as $part) {
        $current[] = $part;
        $crumbs[] = ['label' => $part, 'path' => implode('/', $current)];
    }

    return $crumbs;
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

function h(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES, 'UTF-8');
}

function csrfToken(): string
{
    if (!isset($_SESSION['csrf_token']) || !is_string($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    return $_SESSION['csrf_token'];
}

function requireCsrfToken(): void
{
    $submitted = (string) ($_POST['csrf_token'] ?? '');
    if ($submitted === '' || !hash_equals(csrfToken(), $submitted)) {
        http_response_code(403);
        exit('Invalid request token. Reload the page and try again.');
    }
}

function resolveAssetUrl(string $filename): ?string
{
    if (is_file(__DIR__ . '/' . $filename)) {
        return $filename;
    }
    if (is_file(__DIR__ . '/../' . $filename)) {
        return '../' . $filename;
    }
    return null;
}

function sharedCss(): string
{
    return <<<'CSS'
:root {
    --bg: #e6f2fb;
    --surface: #ffffff;
    --ink: #1a2c3d;
    --muted: #6b8ca8;
    --border: #c8dff0;
    --accent: #2b7fc4;
    --accent-light: #deeeff;
    --accent-hover: #1c68a8;
    --danger: #c83232;
    --danger-light: #fff0f0;
    --success-bg: #edfbf4;
    --success-border: #7dd4a0;
    --shadow: 0 2px 10px rgba(20,80,150,.09), 0 1px 3px rgba(20,80,150,.06);
    --shadow-lg: 0 8px 32px rgba(20,80,150,.14), 0 2px 8px rgba(20,80,150,.08);
    --radius: 14px;
    --radius-sm: 8px;
    --radius-xs: 6px;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    min-height: 100vh;
    background: var(--bg);
    color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    font-size: 15px;
    line-height: 1.5;
}

/* ── Page wrapper ── */
.page {
    width: min(1060px, calc(100% - 32px));
    margin: 0 auto;
    padding: 24px 0 60px;
}

/* ── Header ── */
.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    background: var(--surface);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 14px 22px;
    margin-bottom: 18px;
    border: 1px solid rgba(180,215,240,.6);
}
.header-brand {
    display: flex;
    align-items: center;
    gap: 12px;
}
.header-logo {
    height: 40px;
    width: auto;
    display: block;
}
.header-text h1 {
    font-size: 20px;
    font-weight: 700;
    margin: 0;
    color: var(--accent);
    letter-spacing: -.2px;
}
.header-text p {
    margin: 0;
    color: var(--muted);
    font-size: 12px;
}
.top-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
}

/* ── Flash messages ── */
.message {
    padding: 11px 16px;
    border-radius: var(--radius-sm);
    margin-bottom: 16px;
    background: var(--success-bg);
    border: 1px solid var(--success-border);
    color: #1a6640;
    font-weight: 500;
    font-size: 14px;
}
.message.error {
    background: var(--danger-light);
    border-color: #f5a0a0;
    color: #8a1515;
}

/* ── Panels ── */
.panel {
    background: var(--surface);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    padding: 18px 20px;
    margin-bottom: 16px;
    border: 1px solid rgba(180,215,240,.6);
}

/* ── Toolbar ── */
.toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 10px;
}
.toolbar.breadcrumb-bar {
    padding-bottom: 12px;
    margin-bottom: 16px;
    border-bottom: 1px solid rgba(190, 220, 240, 0.9);
}
.toolbar h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 700;
    color: var(--ink);
    letter-spacing: -.1px;
}

/* ── Breadcrumbs ── */
.breadcrumbs {
    display: flex;
    flex-wrap: wrap;
    gap: 2px;
    align-items: center;
}
.breadcrumbs a {
    color: var(--accent);
    text-decoration: none;
    font-weight: 600;
    font-size: 20px;
    padding: 3px 8px;
    border-radius: 99px;
    transition: background .15s;
}
.breadcrumbs a:hover { background: var(--accent-light); }
.breadcrumbs .sep {
    color: var(--muted);
    font-size: 20px;
    user-select: none;
    padding: 0 1px;
}

/* ── Buttons ── */
.button, .button-link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font: inherit;
    font-size: 13px;
    font-weight: 600;
    padding: 6px 14px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--ink);
    text-decoration: none;
    cursor: pointer;
    border-radius: 99px;
    transition: all .14s;
    white-space: nowrap;
    line-height: 1.4;
}
.button:hover, .button-link:hover {
    background: var(--accent-light);
    border-color: rgba(43,127,196,.45);
    color: var(--accent);
}
.button.primary, .button-link.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
}
.button.primary:hover, .button-link.primary:hover {
    background: var(--accent-hover);
    border-color: var(--accent-hover);
    color: #fff;
}
.button.danger {
    color: var(--danger);
    border-color: #f0b0b0;
}
.button.danger:hover {
    background: var(--danger-light);
    border-color: var(--danger);
}

/* ── Upload zone ── */
.upload {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    background: linear-gradient(160deg, #f4faff 0%, #eaf4ff 100%);
    text-align: center;
    padding: 30px 20px 24px;
    transition: all .18s;
    cursor: default;
}
.upload.is-dragover {
    background: var(--accent-light);
    border-color: var(--accent);
    box-shadow: 0 0 0 4px rgba(43,127,196,.10);
}
.upload-cloud {
    display: block;
    font-size: 44px;
    line-height: 1;
    margin-bottom: 8px;
    color: var(--accent);
    opacity: .7;
}
.upload strong {
    display: block;
    font-size: 15px;
    font-weight: 600;
    color: var(--ink);
    margin-bottom: 14px;
}
.upload-actions {
    display: flex;
    gap: 8px;
    justify-content: center;
    flex-wrap: wrap;
    margin-bottom: 10px;
}
.fine { color: var(--muted); font-size: 12px; }

/* ── Progress bars ── */
.progress { display: grid; gap: 6px; margin-top: 12px; text-align: left; }
.bar { height: 5px; background: var(--accent-light); border-radius: 99px; overflow: hidden; }
.bar span { display: block; height: 100%; width: 0; background: var(--accent); border-radius: 99px; transition: width .15s; }

/* ── Sort form ── */
.sort {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 13px;
    color: var(--muted);
}
.sort label { font-weight: 600; }
.sort select {
    font: inherit;
    font-size: 13px;
    padding: 5px 10px;
    border: 1px solid var(--border);
    border-radius: 99px;
    background: var(--surface);
    color: var(--ink);
    cursor: pointer;
    outline: none;
    transition: border-color .14s;
}
.sort select:focus { border-color: var(--accent); }

/* ── File list ── */
.file-list { margin-top: 4px; }
.row {
    display: grid;
    grid-template-columns: 46px minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    padding: 8px 6px;
    border-radius: var(--radius-xs);
    transition: background .12s;
    border-bottom: 1px solid rgba(190,220,240,.45);
}
.row:last-child { border-bottom: none; }
.row:hover { background: #f2f8ff; }
.row-main { min-width: 0; }

.name {
    font-weight: 600;
    word-break: break-word;
    font-size: 14px;
    color: var(--ink);
}
a.name, .name a { color: var(--accent); text-decoration: none; }
a.name:hover, .name a:hover { text-decoration: underline; }

.meta { color: var(--muted); font-size: 12px; margin-top: 2px; }

/* ── File type icon badges ── */
.icon {
    width: 42px;
    height: 42px;
    border-radius: var(--radius-xs);
    display: grid;
    place-items: center;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .3px;
    text-transform: uppercase;
    background: #e8ecf2;
    color: #5a6878;
    overflow: hidden;
    flex-shrink: 0;
}
.icon.folder { background: linear-gradient(140deg,#fff0c0 0%,#ffd84a 100%); color: #7a5800; font-size: 19px; }
.icon.img    { background: linear-gradient(140deg,#d0eeff 0%,#96ccf8 100%); color: #0a5a90; }
.icon.vid    { background: linear-gradient(140deg,#e8d0ff 0%,#c498f8 100%); color: #480e98; }
.icon.txt, .icon.md, .icon.log, .icon.csv
             { background: linear-gradient(140deg,#ccf8e0 0%,#88e8b0 100%); color: #0a5828; }
.icon.php, .icon.html, .icon.htm
             { background: linear-gradient(140deg,#ffd0d0 0%,#f89898 100%); color: #780808; }
.icon.js     { background: linear-gradient(140deg,#fff8c0 0%,#f8e040 100%); color: #644800; }
.icon.css    { background: linear-gradient(140deg,#d0e4ff 0%,#90b8f8 100%); color: #0c3880; }
.icon.json, .icon.xml, .icon.yaml, .icon.yml, .icon.ini
             { background: linear-gradient(140deg,#ffe0c0 0%,#f8b870 100%); color: #603008; }
.icon.zip, .icon.gz, .icon.tar, .icon.rar
             { background: linear-gradient(140deg,#e0e4e8 0%,#b8c0cc 100%); color: #344050; }
.icon.pdf    { background: linear-gradient(140deg,#ffd0d0 0%,#f87878 100%); color: #600808; }
.icon.sql, .icon.db
             { background: linear-gradient(140deg,#d0d4ff 0%,#9098f8 100%); color: #141888; }
.icon.sh, .icon.conf, .icon.env
             { background: linear-gradient(140deg,#d0f0d0 0%,#80d880 100%); color: #084018; }

.thumb { width: 100%; height: 100%; object-fit: cover; }

.actions { display: flex; gap: 5px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.actions form { margin: 0; }

/* ── Inline preview ── */
.preview { display: none; grid-column: 2 / -1; padding: 10px 0 6px; }
.preview.open { display: block; }
.preview img, .preview video {
    max-width: 100%;
    max-height: 65vh;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: #000;
}

.empty { color: var(--muted); padding: 28px 0; text-align: center; font-size: 14px; }

/* ── Text-editor modal ── */
.modal {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(10,25,55,.45);
    align-items: center;
    justify-content: center;
    padding: 16px;
    backdrop-filter: blur(3px);
}
.modal.open { display: flex; }
.modal-box {
    width: min(960px, 100%);
    height: min(760px, calc(100vh - 32px));
    background: var(--surface);
    border-radius: var(--radius);
    box-shadow: var(--shadow-lg);
    padding: 16px;
    display: grid;
    grid-template-rows: auto 1fr auto;
    gap: 12px;
    border: 1px solid rgba(180,215,240,.7);
}
.editor-head, .editor-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.editor-name { font-weight: 700; word-break: break-word; font-size: 14px; }
.editor {
    width: 100%;
    height: 100%;
    resize: none;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    font-family: 'SF Mono','Cascadia Code','Fira Code','Consolas','Courier New',monospace;
    font-size: 13px;
    line-height: 1.6;
    padding: 12px 14px;
    color: var(--ink);
    background: #f6faff;
    outline: none;
    transition: border-color .14s, box-shadow .14s;
}
.editor:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(43,127,196,.12); }

.status { font-size: 13px; color: var(--muted); }
.status.error { color: var(--danger); }
.status.saved { color: #1a8a50; font-weight: 600; }

.hidden { display: none; }

/* ── Auth pages (login / setup) ── */
.auth-page {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 24px;
}
.auth-card {
    width: min(420px, 100%);
    background: var(--surface);
    border-radius: var(--radius);
    box-shadow: var(--shadow-lg);
    padding: 38px 32px 32px;
    border: 1px solid rgba(180,215,240,.6);
    text-align: center;
}
.auth-logo { height: 64px; width: auto; margin-bottom: 18px; }
.auth-card h1 { font-size: 22px; font-weight: 700; margin: 0 0 6px; color: var(--accent); }
.auth-card > p { color: var(--muted); font-size: 14px; margin: 0 0 22px; }
.auth-error {
    color: #8a1515;
    background: var(--danger-light);
    border: 1px solid #f5a0a0;
    border-radius: var(--radius-xs);
    padding: 9px 12px;
    font-size: 13px;
    margin-bottom: 14px;
    text-align: left;
}
.field { display: grid; gap: 5px; margin: 14px 0; text-align: left; }
.field label { font-size: 13px; font-weight: 600; color: var(--ink); }
.input {
    font: inherit;
    font-size: 14px;
    padding: 10px 13px;
    border: 1px solid var(--border);
    border-radius: var(--radius-xs);
    color: var(--ink);
    background: var(--surface);
    outline: none;
    transition: border-color .14s, box-shadow .14s;
    width: 100%;
}
.input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(43,127,196,.12); }
.auth-submit {
    width: 100%;
    justify-content: center;
    margin-top: 10px;
    padding: 10px 14px;
    font-size: 14px;
    border-radius: var(--radius-xs);
}
.note { color: var(--muted); font-size: 13px; line-height: 1.5; text-align: left; margin: 0 0 20px; }

/* ── Responsive ── */
@media (max-width: 760px) {
    .header { flex-direction: column; align-items: flex-start; gap: 12px; }
    .header-brand { width: 100%; }
    .row { grid-template-columns: 42px minmax(0, 1fr); }
    .actions, .preview { grid-column: 1 / -1; justify-content: flex-start; }
    .button, .button-link { padding: 7px 12px; }
    .panel { padding: 14px; }
    .auth-card { padding: 28px 20px; }
}
CSS;
}

ensureStorageRoot($storageRoot);
$realStorageRoot = realpath($storageRoot);
if ($realStorageRoot === false) {
    http_response_code(500);
    exit('Could not prepare freecloud_files.');
}

$config = loadConfig($configFile);
$setupError = '';
$loginError = '';
$logoUrl = resolveAssetUrl('logo.png');
$iconUrl  = resolveAssetUrl('icon.png') ?? resolveAssetUrl('favicon.ico');

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    requireCsrfToken();
}

if ($config === null && $_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['action'] ?? '') === 'setup') {
    $setupPassword = (string) ($_POST['password'] ?? '');
    if (strlen($setupPassword) < 8) {
        $setupError = 'Use a password with at least 8 characters.';
    } elseif (saveConfig($configFile, (string) ($_POST['drive_name'] ?? 'FreeCloud'), $setupPassword)) {
        session_regenerate_id(true);
        $_SESSION['auth'] = true;
        redirectToDrive('', 'FreeCloud is ready.');
    } else {
        $setupError = 'Could not save config.json. Check folder permissions.';
    }
}

if ($config !== null && $_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['action'] ?? '') === 'login') {
    $password = (string) ($_POST['password'] ?? '');
    if (password_verify($password, (string) ($config['password_hash'] ?? ''))) {
        session_regenerate_id(true);
        $_SESSION['auth'] = true;
        unset($_SESSION['login_failures']);
        redirectToDrive();
    }
    $_SESSION['login_failures'] = min(10, (int) ($_SESSION['login_failures'] ?? 0) + 1);
    if ((int) $_SESSION['login_failures'] >= 5) {
        sleep(1);
    }
    $loginError = 'Incorrect password.';
}

if ($config !== null && $_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['action'] ?? '') === 'logout') {
    $_SESSION = [];
    if (ini_get('session.use_cookies')) {
        $params = session_get_cookie_params();
        setcookie(session_name(), '', time() - 42000, $params['path'], $params['domain'], (bool) $params['secure'], (bool) $params['httponly']);
    }
    session_destroy();
    header('Location: freecloud.php');
    exit;
}

if ($config !== null && !isAuthed($config)) {
    $driveName = (string) ($config['name'] ?? 'FreeCloud');
    ?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title><?= h($driveName) ?> — Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <?php if ($iconUrl !== null): ?><link rel="icon" href="<?= h($iconUrl) ?>"><?php endif; ?>
    <style><?= sharedCss() ?></style>
</head>
<body>
<div class="auth-page">
    <div class="auth-card">
        <?php if ($logoUrl !== null): ?>
            <img src="<?= h($logoUrl) ?>" alt="FreeCloud" class="auth-logo">
        <?php endif; ?>
        <h1><?= h($driveName) ?></h1>
        <p>Enter your password to access the drive.</p>
        <?php if ($loginError !== ''): ?><div class="auth-error"><?= h($loginError) ?></div><?php endif; ?>
        <form method="post">
            <input type="hidden" name="action" value="login">
            <input type="hidden" name="csrf_token" value="<?= h(csrfToken()) ?>">
            <div class="field">
                <label for="pw">Password</label>
                <input class="input" id="pw" type="password" name="password" autocomplete="current-password" autofocus>
            </div>
            <button class="button primary auth-submit" type="submit">Log In</button>
        </form>
    </div>
</div>
</body>
</html>
    <?php
    exit;
}

if ($config === null) {
    ?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Set up FreeCloud</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <?php if ($iconUrl !== null): ?><link rel="icon" href="<?= h($iconUrl) ?>"><?php endif; ?>
    <style><?= sharedCss() ?></style>
</head>
<body>
<div class="auth-page">
    <div class="auth-card">
        <?php if ($logoUrl !== null): ?>
            <img src="<?= h($logoUrl) ?>" alt="FreeCloud" class="auth-logo">
        <?php endif; ?>
        <h1>Set up FreeCloud</h1>
        <p class="note">Give your drive a name and use a password with at least 8 characters.</p>
        <?php if ($setupError !== ''): ?><div class="auth-error"><?= h($setupError) ?></div><?php endif; ?>
        <form method="post">
            <input type="hidden" name="action" value="setup">
            <input type="hidden" name="csrf_token" value="<?= h(csrfToken()) ?>">
            <div class="field">
                <label for="drive_name">Drive name</label>
                <input class="input" id="drive_name" type="text" name="drive_name" value="FreeCloud" required>
            </div>
            <div class="field">
                <label for="pw">Password</label>
                <input class="input" id="pw" type="password" name="password" minlength="8" autocomplete="new-password" required>
            </div>
            <button class="button primary auth-submit" type="submit">Create Drive</button>
        </form>
    </div>
</div>
</body>
</html>
    <?php
    exit;
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = (string) ($_POST['action'] ?? 'upload');
    $currentPath = normalizeRelativePath((string) ($_POST['current_path'] ?? ''));
    $sort = in_array((string) ($_POST['sort'] ?? ''), ['name', 'newest', 'oldest'], true) ? (string) $_POST['sort'] : '';

    if ($action === 'save_file') {
        header('Content-Type: application/json; charset=utf-8');
        $targetPath = normalizeRelativePath((string) ($_POST['target_path'] ?? ''));
        $absoluteTarget = $targetPath === '' ? null : resolveExistingPath($realStorageRoot, $targetPath);
        if ($absoluteTarget === null || !is_file($absoluteTarget)) {
            http_response_code(404);
            echo json_encode(['error' => 'File not found.']);
            exit;
        }
        if (!isEditableFile(basename($absoluteTarget))) {
            http_response_code(403);
            echo json_encode(['error' => 'This file type cannot be edited.']);
            exit;
        }
        if (file_put_contents($absoluteTarget, (string) ($_POST['content'] ?? ''), LOCK_EX) === false) {
            http_response_code(500);
            echo json_encode(['error' => 'Could not save file.']);
            exit;
        }
        echo json_encode(['ok' => true]);
        exit;
    }

    if ($action === 'delete') {
        $targetPath = normalizeRelativePath((string) ($_POST['target_path'] ?? ''));
        $absoluteTarget = $targetPath === '' ? null : resolveExistingPath($realStorageRoot, $targetPath);
        if ($absoluteTarget === null || $absoluteTarget === $realStorageRoot) {
            redirectToDrive($currentPath, 'That item does not exist anymore.', 'error', $sort);
        }
        if (!deletePathRecursively($absoluteTarget)) {
            redirectToDrive($currentPath, 'Could not delete that item.', 'error', $sort);
        }
        redirectToDrive($currentPath, 'Deleted ' . basename($absoluteTarget) . '.', 'success', $sort);
    }

    $targetDirectory = resolveExistingPath($realStorageRoot, $currentPath);
    if ($targetDirectory === null || !is_dir($targetDirectory)) {
        redirectToDrive('', 'That folder does not exist anymore.', 'error', $sort);
    }

    $uploads = normalizeUploads($_FILES['files'] ?? []);
    if ($uploads === []) {
        redirectToDrive($currentPath, 'Nothing was uploaded.', 'error', $sort);
    }

    $relativePaths = $_POST['relative_paths'] ?? [];
    if (!is_array($relativePaths)) {
        $relativePaths = [];
    }

    $savedCount = 0;
    foreach ($uploads as $index => $upload) {
        if ($upload['error'] === UPLOAD_ERR_NO_FILE) {
            continue;
        }
        if ($upload['error'] !== UPLOAD_ERR_OK) {
            redirectToDrive($currentPath, uploadErrorMessage($upload['error']), 'error', $sort);
        }

        $relativeName = normalizeRelativePath((string) ($relativePaths[$index] ?? $upload['name']));
        if ($relativeName === '') {
            continue;
        }

        $destination = $targetDirectory . '/' . $relativeName;
        $destinationDir = dirname($destination);
        if (!is_dir($destinationDir) && !mkdir($destinationDir, 0775, true) && !is_dir($destinationDir)) {
            redirectToDrive($currentPath, 'Could not create the target folder.', 'error', $sort);
        }

        $realDestinationDir = realpath($destinationDir);
        if ($realDestinationDir === false || !isWithinRoot($realDestinationDir, $realStorageRoot) || !isWithinRoot($destination, $realStorageRoot)) {
            redirectToDrive($currentPath, 'An upload path was rejected.', 'error', $sort);
        }

        if (!move_uploaded_file($upload['tmp_name'], $destination)) {
            redirectToDrive($currentPath, 'A file could not be saved.', 'error', $sort);
        }

        $savedCount += 1;
    }

    redirectToDrive($currentPath, $savedCount === 1 ? 'Uploaded 1 file.' : 'Uploaded ' . $savedCount . ' files.', 'success', $sort);
}

if ($_SERVER['REQUEST_METHOD'] === 'GET' && ($_GET['action'] ?? '') === 'load_file') {
    header('Content-Type: application/json; charset=utf-8');
    $targetPath = normalizeRelativePath((string) ($_GET['path'] ?? ''));
    $absoluteTarget = $targetPath === '' ? null : resolveExistingPath($realStorageRoot, $targetPath);
    if ($absoluteTarget === null || !is_file($absoluteTarget)) {
        http_response_code(404);
        echo json_encode(['error' => 'File not found.']);
        exit;
    }
    if (!isEditableFile(basename($absoluteTarget))) {
        http_response_code(403);
        echo json_encode(['error' => 'This file type cannot be edited.']);
        exit;
    }
    $content = file_get_contents($absoluteTarget);
    if ($content === false) {
        http_response_code(500);
        echo json_encode(['error' => 'Could not read file.']);
        exit;
    }
    echo json_encode(['content' => $content]);
    exit;
}

$driveName = (string) ($config['name'] ?? 'FreeCloud');
$currentPath = normalizeRelativePath((string) ($_GET['path'] ?? ''));
$sort = in_array((string) ($_GET['sort'] ?? 'name'), ['name', 'newest', 'oldest'], true) ? (string) $_GET['sort'] : 'name';
$currentDirectory = resolveExistingPath($realStorageRoot, $currentPath);
if ($currentDirectory === null || !is_dir($currentDirectory)) {
    $currentPath = '';
    $currentDirectory = $realStorageRoot;
}

$entries = listEntries($currentDirectory, $currentPath, $sort);
$breadcrumbs = buildBreadcrumbs($currentPath);
$message = isset($_GET['message']) ? trim((string) $_GET['message']) : '';
$messageType = (string) ($_GET['type'] ?? 'success');
$parentPath = $currentPath === '' ? null : dirname($currentPath);
if ($parentPath === '.') {
    $parentPath = '';
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title><?= h($driveName) ?></title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <?php if ($iconUrl !== null): ?><link rel="icon" href="<?= h($iconUrl) ?>"><?php endif; ?>
    <style><?= sharedCss() ?></style>
</head>
<body>
<main class="page">

    <header class="header">
        <div class="header-brand">
            <?php if ($logoUrl !== null): ?>
                <img src="<?= h($logoUrl) ?>" alt="<?= h($driveName) ?>" class="header-logo">
            <?php endif; ?>
            <div class="header-text">
                <h1><?= h($driveName) ?></h1>
                <p>Your personal cloud drive</p>
            </div>
        </div>
        <div class="top-actions">
            <?php if (authRequired($config)): ?>
                <form method="post">
                    <input type="hidden" name="action" value="logout">
                    <input type="hidden" name="csrf_token" value="<?= h(csrfToken()) ?>">
                    <button class="button" type="submit">Log Out</button>
                </form>
            <?php endif; ?>
        </div>
    </header>

    <?php if ($message !== ''): ?>
        <div class="message <?= $messageType === 'error' ? 'error' : '' ?>"><?= h($message) ?></div>
    <?php endif; ?>

    <section class="panel">
        <form class="upload" id="upload-zone" method="post" enctype="multipart/form-data">
            <input type="hidden" name="csrf_token" value="<?= h(csrfToken()) ?>">
            <input type="hidden" name="current_path" value="<?= h($currentPath) ?>">
            <input type="hidden" name="sort" value="<?= h($sort) ?>">
            <span class="upload-cloud">&#9729;</span>
            <strong>Drop files or folders here</strong>
            <div class="upload-actions">
                <button class="button primary" type="button" id="pick-files">Upload Files</button>
                <button class="button" type="button" id="pick-folder">Upload Folder</button>
            </div>
            <div class="fine" id="upload-status">Files are stored in <code>freecloud_files/</code></div>
            <div class="progress" id="upload-progress" hidden></div>
            <input class="hidden" id="file-input" type="file" multiple>
            <input class="hidden" id="folder-input" type="file" webkitdirectory directory multiple>
        </form>
    </section>

    <section class="panel">
        <div class="toolbar breadcrumb-bar">
            <nav class="breadcrumbs" aria-label="Breadcrumb">
                <?php foreach ($breadcrumbs as $index => $crumb): ?>
                    <?php if ($index > 0): ?><span class="sep">›</span><?php endif; ?>
                    <a href="freecloud.php?path=<?= rawurlencode($crumb['path']) ?>&sort=<?= rawurlencode($sort) ?>"><?= h($crumb['label']) ?></a>
                <?php endforeach; ?>
            </nav>
            <div class="top-actions">
                <?php if ($parentPath !== null): ?>
                    <a class="button-link" href="freecloud.php<?= $parentPath === '' ? '?sort=' . rawurlencode($sort) : '?path=' . rawurlencode($parentPath) . '&sort=' . rawurlencode($sort) ?>">Up One Folder</a>
                <?php endif; ?>
                <?php if ($currentPath !== ''): ?>
                    <a class="button-link primary" href="freecloud_download.php?path=<?= rawurlencode($currentPath) ?>">Download ZIP</a>
                <?php endif; ?>
            </div>
        </div>

        <div class="toolbar">
            <h2>Files</h2>
            <form class="sort" method="get">
                <input type="hidden" name="path" value="<?= h($currentPath) ?>">
                <label for="sort-select">Sort</label>
                <select id="sort-select" name="sort">
                    <option value="name"   <?= $sort === 'name'   ? 'selected' : '' ?>>Name</option>
                    <option value="newest" <?= $sort === 'newest' ? 'selected' : '' ?>>Newest</option>
                    <option value="oldest" <?= $sort === 'oldest' ? 'selected' : '' ?>>Oldest</option>
                </select>
                <button class="button" type="submit">Apply</button>
            </form>
        </div>

        <?php if ($entries === []): ?>
            <div class="empty">This folder is empty.</div>
        <?php else: ?>
            <div class="file-list">
                <?php foreach ($entries as $entry): ?>
                    <?php
                    $kind = $entry['isDir'] ? 'DIR' : strtoupper(substr(pathinfo($entry['name'], PATHINFO_EXTENSION) ?: 'FILE', 0, 4));
                    $previewId = 'preview-' . md5($entry['path']);
                    $ext = strtolower(pathinfo($entry['name'], PATHINFO_EXTENSION));
                    $iconTypeClass = match(true) {
                        $entry['isDir']   => 'folder',
                        $entry['isImage'] => 'img',
                        $entry['isVideo'] => 'vid',
                        default           => ($ext !== '' ? $ext : 'file'),
                    };
                    ?>
                    <div class="row">
                        <div class="icon <?= h($iconTypeClass) ?>">
                            <?php if ($entry['isImage'] && $entry['previewUrl'] !== null): ?>
                                <img class="thumb" src="<?= h($entry['previewUrl']) ?>" alt="">
                            <?php elseif ($entry['isDir']): ?>
                                &#128193;
                            <?php else: ?>
                                <?= h($kind) ?>
                            <?php endif; ?>
                        </div>
                        <div class="row-main">
                            <?php if ($entry['isDir']): ?>
                                <a class="name" href="freecloud.php?path=<?= rawurlencode($entry['path']) ?>&sort=<?= rawurlencode($sort) ?>"><?= h($entry['name']) ?></a>
                            <?php else: ?>
                                <div class="name"><?= h($entry['name']) ?></div>
                            <?php endif; ?>
                            <div class="meta">
                                <?= $entry['isDir'] ? 'Folder' : formatBytes((int) $entry['size']) ?>
                                <?php if ((int) $entry['modifiedAt'] > 0): ?>&nbsp;·&nbsp;<?= h(date('M j, Y g:i A', (int) $entry['modifiedAt'])) ?><?php endif; ?>
                            </div>
                        </div>
                        <div class="actions">
                            <?php if ($entry['isImage'] || $entry['isVideo']): ?>
                                <button class="button preview-toggle" type="button" data-target="<?= h($previewId) ?>"><?= $entry['isVideo'] ? 'Play' : 'View' ?></button>
                            <?php endif; ?>
                            <?php if ($entry['isEditable']): ?>
                                <button class="button edit-button" type="button" data-path="<?= h($entry['path']) ?>" data-name="<?= h($entry['name']) ?>">Edit</button>
                            <?php endif; ?>
                            <a class="button-link" href="freecloud_download.php?path=<?= rawurlencode($entry['path']) ?>">Download</a>
                            <form method="post">
                                <input type="hidden" name="action" value="delete">
                                <input type="hidden" name="csrf_token" value="<?= h(csrfToken()) ?>">
                                <input type="hidden" name="current_path" value="<?= h($currentPath) ?>">
                                <input type="hidden" name="target_path" value="<?= h($entry['path']) ?>">
                                <input type="hidden" name="sort" value="<?= h($sort) ?>">
                                <button class="button danger delete-button" type="submit" data-name="<?= h($entry['name']) ?>">Delete</button>
                            </form>
                        </div>
                        <?php if (($entry['isImage'] || $entry['isVideo']) && $entry['previewUrl'] !== null): ?>
                            <div class="preview" id="<?= h($previewId) ?>">
                                <?php if ($entry['isVideo']): ?>
                                    <video controls preload="metadata"><source src="<?= h($entry['previewUrl']) ?>" type="<?= h((string) $entry['videoMimeType']) ?>">This video cannot be played in this browser.</video>
                                <?php else: ?>
                                    <img src="<?= h($entry['previewUrl']) ?>" alt="<?= h($entry['name']) ?>">
                                <?php endif; ?>
                            </div>
                        <?php endif; ?>
                    </div>
                <?php endforeach; ?>
            </div>
        <?php endif; ?>
    </section>

</main>

<div class="modal" id="editor-modal">
    <div class="modal-box">
        <div class="editor-head">
            <div class="editor-name" id="editor-name"></div>
            <button class="button" type="button" id="editor-close">Close</button>
        </div>
        <textarea class="editor" id="editor-textarea" spellcheck="false"></textarea>
        <div class="editor-foot">
            <div class="status" id="editor-status"></div>
            <button class="button primary" type="button" id="editor-save" disabled>Save</button>
        </div>
    </div>
</div>

<script>
const currentPath = <?= json_encode($currentPath, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?>;
const currentSort = <?= json_encode($sort) ?>;
const csrfToken = <?= json_encode(csrfToken()) ?>;
const zone = document.getElementById('upload-zone');
const statusEl = document.getElementById('upload-status');
const progressEl = document.getElementById('upload-progress');
const fileInput = document.getElementById('file-input');
const folderInput = document.getElementById('folder-input');

function setStatus(text, error = false) {
    statusEl.textContent = text;
    statusEl.style.color = error ? '#a03030' : '';
}

function cleanClientPath(path) {
    return String(path || '').replace(/^\/+|\/+$/g, '').split('/').filter(part => part && part !== '.' && part !== '..').join('/');
}

function uploadRow(name) {
    const item = document.createElement('div');
    item.innerHTML = `<div class="fine"></div><div class="bar"><span></span></div>`;
    item.querySelector('.fine').textContent = name;
    progressEl.appendChild(item);
    return item.querySelector('.bar span');
}

function uploadFile(file) {
    return new Promise((resolve, reject) => {
        const relativePath = cleanClientPath(file.relativePath || file.webkitRelativePath || file.name);
        const formData = new FormData();
        const xhr = new XMLHttpRequest();
        const bar = uploadRow(relativePath || file.name);
        formData.append('current_path', currentPath);
        formData.append('sort', currentSort);
        formData.append('csrf_token', csrfToken);
        formData.append('files[]', file, file.name);
        formData.append('relative_paths[]', relativePath || file.name);
        xhr.open('POST', 'freecloud.php');
        xhr.upload.addEventListener('progress', event => {
            if (event.lengthComputable) {
                bar.style.width = Math.round((event.loaded / event.total) * 100) + '%';
            }
        });
        xhr.addEventListener('load', () => {
            bar.style.width = '100%';
            if (xhr.status >= 200 && xhr.status < 400) {
                resolve(xhr.responseURL || 'freecloud.php');
            } else {
                reject(new Error('Upload failed.'));
            }
        });
        xhr.addEventListener('error', () => reject(new Error('Upload failed.')));
        xhr.send(formData);
    });
}

async function uploadFiles(files) {
    files = Array.from(files || []);
    if (!files.length) return;
    progressEl.hidden = false;
    progressEl.innerHTML = '';
    try {
        let redirectUrl = 'freecloud.php';
        for (let i = 0; i < files.length; i += 1) {
            setStatus(`Uploading ${i + 1} of ${files.length}...`);
            redirectUrl = await uploadFile(files[i]);
        }
        window.location.href = redirectUrl;
    } catch (error) {
        setStatus(error.message || 'Upload failed.', true);
    }
}

function readEntry(entry, prefix = '') {
    return new Promise((resolve, reject) => {
        if (entry.isFile) {
            entry.file(file => {
                file.relativePath = prefix ? `${prefix}/${file.name}` : file.name;
                resolve([file]);
            }, reject);
            return;
        }
        if (!entry.isDirectory) {
            resolve([]);
            return;
        }
        const reader = entry.createReader();
        const entries = [];
        const readBatch = () => reader.readEntries(async batch => {
            if (!batch.length) {
                const nested = await Promise.all(entries.map(child => readEntry(child, prefix ? `${prefix}/${entry.name}` : entry.name)));
                resolve(nested.flat());
                return;
            }
            entries.push(...batch);
            readBatch();
        }, reject);
        readBatch();
    });
}

async function filesFromDrop(items, fallbackFiles) {
    const entries = Array.from(items || []).map(item => typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null).filter(Boolean);
    if (!entries.length) return Array.from(fallbackFiles || []);
    const nested = await Promise.all(entries.map(entry => readEntry(entry)));
    return nested.flat();
}

document.getElementById('pick-files').addEventListener('click', () => fileInput.click());
document.getElementById('pick-folder').addEventListener('click', () => folderInput.click());
fileInput.addEventListener('change', () => { uploadFiles(fileInput.files); fileInput.value = ''; });
folderInput.addEventListener('change', () => { uploadFiles(folderInput.files); folderInput.value = ''; });
['dragenter','dragover'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.add('is-dragover'); }));
['dragleave','dragend','drop'].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); if (name !== 'drop') zone.classList.remove('is-dragover'); }));
zone.addEventListener('drop', async event => {
    zone.classList.remove('is-dragover');
    try {
        await uploadFiles(await filesFromDrop(event.dataTransfer?.items, event.dataTransfer?.files));
    } catch (error) {
        setStatus('That drop could not be read in this browser.', true);
    }
});

document.querySelectorAll('.preview-toggle').forEach(button => {
    button.addEventListener('click', () => {
        const target = document.getElementById(button.dataset.target || '');
        if (!target) return;
        const open = target.classList.toggle('open');
        button.textContent = open ? 'Hide' : (target.querySelector('video') ? 'Play' : 'View');
        if (!open) target.querySelector('video')?.pause();
    });
});

document.querySelectorAll('.delete-button').forEach(button => {
    button.addEventListener('click', event => {
        if (!confirm(`Delete ${button.dataset.name || 'this item'}? This cannot be undone.`)) {
            event.preventDefault();
        }
    });
});

const editorModal = document.getElementById('editor-modal');
const editorName = document.getElementById('editor-name');
const editorTextarea = document.getElementById('editor-textarea');
const editorSave = document.getElementById('editor-save');
const editorStatus = document.getElementById('editor-status');
let editorPath = '';

function editorStatusText(text, cls = '') {
    editorStatus.textContent = text;
    editorStatus.className = 'status' + (cls ? ' ' + cls : '');
}

function closeEditor() {
    editorModal.classList.remove('open');
    editorPath = '';
    editorTextarea.value = '';
    editorSave.disabled = true;
}

document.querySelectorAll('.edit-button').forEach(button => {
    button.addEventListener('click', async () => {
        editorPath = button.dataset.path || '';
        editorName.textContent = button.dataset.name || editorPath;
        editorTextarea.value = '';
        editorStatusText('Loading...');
        editorModal.classList.add('open');
        try {
            const response = await fetch('freecloud.php?action=load_file&path=' + encodeURIComponent(editorPath), { cache: 'no-store' });
            const data = await response.json();
            if (!response.ok) {
                editorStatusText(data.error || 'Could not load file.', 'error');
                return;
            }
            editorTextarea.value = data.content;
            editorSave.disabled = false;
            editorStatusText('');
            editorTextarea.focus();
        } catch (error) {
            editorStatusText('Could not load file.', 'error');
        }
    });
});

editorSave.addEventListener('click', async () => {
    editorSave.disabled = true;
    editorStatusText('Saving...');
    try {
        const response = await fetch('freecloud.php', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
            body: new URLSearchParams({action: 'save_file', csrf_token: csrfToken, target_path: editorPath, content: editorTextarea.value})
        });
        const data = await response.json();
        if (!response.ok) {
            editorStatusText(data.error || 'Could not save.', 'error');
        } else {
            editorStatusText('Saved.', 'saved');
        }
    } catch (error) {
        editorStatusText('Could not save.', 'error');
    } finally {
        editorSave.disabled = false;
    }
});
document.getElementById('editor-close').addEventListener('click', closeEditor);
editorModal.addEventListener('click', event => { if (event.target === editorModal) closeEditor(); });
editorTextarea.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 's') {
        event.preventDefault();
        if (!editorSave.disabled) editorSave.click();
    }
});
</script>
</body>
</html>
