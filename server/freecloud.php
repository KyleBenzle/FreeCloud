<?php
declare(strict_types=1);

$baseDir = __DIR__;
$sessionDir = $baseDir . '/freecloud_sessions';
if (!is_dir($sessionDir)) {
    mkdir($sessionDir, 0775, true);
}
session_save_path($sessionDir);
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

    return file_put_contents($configFile, json_encode($config, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), LOCK_EX) !== false;
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

ensureStorageRoot($storageRoot);
$realStorageRoot = realpath($storageRoot);
if ($realStorageRoot === false) {
    http_response_code(500);
    exit('Could not prepare freecloud_files.');
}

$config = loadConfig($configFile);
$setupError = '';
$loginError = '';

if ($config === null && $_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['action'] ?? '') === 'setup') {
    if (saveConfig($configFile, (string) ($_POST['drive_name'] ?? 'FreeCloud'), (string) ($_POST['password'] ?? ''))) {
        session_regenerate_id(true);
        $_SESSION['auth'] = true;
        redirectToDrive('', 'FreeCloud is ready.');
    }
    $setupError = 'Could not save config.json. Check folder permissions.';
}

if ($config !== null && $_SERVER['REQUEST_METHOD'] === 'POST' && ($_POST['action'] ?? '') === 'login') {
    $password = (string) ($_POST['password'] ?? '');
    if (password_verify($password, (string) ($config['password_hash'] ?? ''))) {
        session_regenerate_id(true);
        $_SESSION['auth'] = true;
        redirectToDrive();
    }
    $loginError = 'Incorrect password.';
}

if ($config !== null && ($_GET['action'] ?? '') === 'logout') {
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
    <title><?= h($driveName) ?> Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body{margin:0;background:#f7f7f4;color:#222;font-family:Arial,Helvetica,sans-serif}.box{width:min(420px,calc(100% - 32px));margin:12vh auto;padding:24px;background:#fff;border:1px solid #bbb}.field{display:grid;gap:6px;margin:14px 0}.input{font:inherit;padding:10px;border:1px solid #888}.button{font:inherit;font-weight:700;padding:9px 14px;border:1px solid #555;background:#eee;color:#111;cursor:pointer}.error{color:#a00000}
    </style>
</head>
<body>
    <main class="box">
        <h1><?= h($driveName) ?></h1>
        <p>Enter the drive password.</p>
        <?php if ($loginError !== ''): ?><p class="error"><?= h($loginError) ?></p><?php endif; ?>
        <form method="post">
            <input type="hidden" name="action" value="login">
            <label class="field">Password <input class="input" type="password" name="password" autocomplete="current-password" autofocus></label>
            <button class="button" type="submit">Log In</button>
        </form>
    </main>
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
    <style>
        body{margin:0;background:#f7f7f4;color:#222;font-family:Arial,Helvetica,sans-serif}.box{width:min(520px,calc(100% - 32px));margin:10vh auto;padding:24px;background:#fff;border:1px solid #bbb}.field{display:grid;gap:6px;margin:14px 0}.input{font:inherit;padding:10px;border:1px solid #888}.button{font:inherit;font-weight:700;padding:9px 14px;border:1px solid #555;background:#eee;color:#111;cursor:pointer}.note{color:#555;line-height:1.45}.error{color:#a00000}
    </style>
</head>
<body>
    <main class="box">
        <h1>Set up FreeCloud</h1>
        <p class="note">Create the local config file. A password is optional, but recommended.</p>
        <?php if ($setupError !== ''): ?><p class="error"><?= h($setupError) ?></p><?php endif; ?>
        <form method="post">
            <input type="hidden" name="action" value="setup">
            <label class="field">Drive name <input class="input" type="text" name="drive_name" value="FreeCloud" required></label>
            <label class="field">Password <input class="input" type="password" name="password" autocomplete="new-password"></label>
            <button class="button" type="submit">Create Drive</button>
        </form>
    </main>
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
    <style>
        :root{color-scheme:light;--bg:#f7f7f4;--panel:#fff;--ink:#222;--muted:#606060;--line:#bdbdbd;--button:#ededed;--accent:#1f5f9f;--danger:#a03030}
        *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Arial,Helvetica,sans-serif;font-size:16px}.page{width:min(1040px,calc(100% - 24px));margin:0 auto;padding:20px 0 40px}.header{display:flex;align-items:end;justify-content:space-between;gap:16px;border-bottom:2px solid var(--line);padding-bottom:12px;margin-bottom:14px}.header h1{font-size:34px;line-height:1;margin:0}.header p{margin:6px 0 0;color:var(--muted)}.top-actions{display:flex;gap:8px;flex-wrap:wrap}.panel{background:var(--panel);border:1px solid var(--line);padding:14px;margin-bottom:14px}.breadcrumbs{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.breadcrumbs a{color:var(--accent);font-weight:700;text-decoration:none}.button,.button-link{display:inline-block;font:inherit;font-weight:700;padding:7px 11px;border:1px solid #777;background:var(--button);color:#111;text-decoration:none;cursor:pointer;border-radius:2px}.button.primary,.button-link.primary{background:#dbeaff;border-color:#6f9dcc}.button.danger{background:#ffe1e1;border-color:#c98686;color:#6f0000}.message{border:1px solid #7aa57a;background:#ecfaec;padding:10px;margin-bottom:14px}.message.error{border-color:#b77;background:#fff0f0}.upload{border:2px dashed #999;background:#fafafa;text-align:center;padding:18px}.upload.is-dragover{background:#edf5ff;border-color:#1f5f9f}.upload-actions{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:10px 0}.fine{color:var(--muted);font-size:14px}.hidden{display:none}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.sort{display:flex;gap:6px;align-items:center}.file-list{border-top:1px solid var(--line)}.row{display:grid;grid-template-columns:42px minmax(0,1fr) auto;gap:10px;align-items:center;border-bottom:1px solid var(--line);padding:9px 0}.row-main{min-width:0}.name{font-weight:700;word-break:break-word}.meta{color:var(--muted);font-size:14px;margin-top:3px}.icon{width:38px;height:38px;border:1px solid #999;background:#f1f1f1;display:grid;place-items:center;font-size:12px;font-weight:700}.icon.folder{background:#fff2bd}.thumb{width:100%;height:100%;object-fit:cover}.actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end}.actions form{margin:0}.preview{display:none;grid-column:2 / -1;padding:8px 0 4px}.preview.open{display:block}.preview img,.preview video{max-width:100%;max-height:65vh;border:1px solid var(--line);background:#000}.empty{color:var(--muted);padding:12px 0}.progress{display:grid;gap:6px;margin-top:10px;text-align:left}.bar{height:8px;background:#ddd;border:1px solid #bbb}.bar span{display:block;height:100%;width:0;background:#1f5f9f}.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);align-items:center;justify-content:center;padding:16px}.modal.open{display:flex}.modal-box{width:min(960px,100%);height:min(760px,calc(100vh - 32px));background:#fff;border:1px solid #777;padding:12px;display:grid;grid-template-rows:auto 1fr auto;gap:10px}.editor-head,.editor-foot{display:flex;align-items:center;justify-content:space-between;gap:10px}.editor-name{font-weight:700;word-break:break-word}.editor{width:100%;height:100%;resize:none;border:1px solid #888;font-family:"Courier New",monospace;font-size:14px;line-height:1.45;padding:10px}.status{color:var(--muted)}.status.error{color:var(--danger)}.status.saved{color:#127012}@media(max-width:760px){.header{align-items:flex-start;flex-direction:column}.row{grid-template-columns:42px minmax(0,1fr)}.actions,.preview{grid-column:1 / -1;justify-content:flex-start}.button,.button-link{padding:8px 10px}}
    </style>
</head>
<body>
<main class="page">
    <header class="header">
        <div>
            <h1><?= h($driveName) ?></h1>
            <p>Free Cloud Drive</p>
        </div>
        <div class="top-actions">
            <?php if (authRequired($config)): ?><a class="button-link" href="freecloud.php?action=logout">Log Out</a><?php endif; ?>
        </div>
    </header>

    <?php if ($message !== ''): ?><div class="message <?= $messageType === 'error' ? 'error' : '' ?>"><?= h($message) ?></div><?php endif; ?>

    <section class="panel">
        <div class="toolbar">
            <nav class="breadcrumbs" aria-label="Breadcrumb">
                <?php foreach ($breadcrumbs as $index => $crumb): ?>
                    <?php if ($index > 0): ?><span>/</span><?php endif; ?>
                    <a href="freecloud.php?path=<?= rawurlencode($crumb['path']) ?>&sort=<?= rawurlencode($sort) ?>"><?= h($crumb['label']) ?></a>
                <?php endforeach; ?>
            </nav>
            <div class="top-actions">
                <?php if ($parentPath !== null): ?><a class="button-link" href="freecloud.php<?= $parentPath === '' ? '?sort=' . rawurlencode($sort) : '?path=' . rawurlencode($parentPath) . '&sort=' . rawurlencode($sort) ?>">Up One Folder</a><?php endif; ?>
                <?php if ($currentPath !== ''): ?><a class="button-link primary" href="freecloud_download.php?path=<?= rawurlencode($currentPath) ?>">Download Folder ZIP</a><?php endif; ?>
            </div>
        </div>
    </section>

    <section class="panel">
        <form class="upload" id="upload-zone" method="post" enctype="multipart/form-data">
            <input type="hidden" name="current_path" value="<?= h($currentPath) ?>">
            <input type="hidden" name="sort" value="<?= h($sort) ?>">
            <strong>Drop files or folders here</strong>
            <div class="upload-actions">
                <button class="button primary" type="button" id="pick-files">Upload Files</button>
                <button class="button primary" type="button" id="pick-folder">Upload Folder</button>
            </div>
            <div class="fine" id="upload-status">Files are stored inside <code>freecloud_files/</code>.</div>
            <div class="progress" id="upload-progress" hidden></div>
            <input class="hidden" id="file-input" type="file" multiple>
            <input class="hidden" id="folder-input" type="file" webkitdirectory directory multiple>
        </form>
    </section>

    <section class="panel">
        <div class="toolbar" style="margin-bottom:10px">
            <h2 style="margin:0;font-size:22px">Files</h2>
            <form class="sort" method="get">
                <input type="hidden" name="path" value="<?= h($currentPath) ?>">
                <label for="sort">Sort</label>
                <select id="sort" name="sort">
                    <option value="name" <?= $sort === 'name' ? 'selected' : '' ?>>Name</option>
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
                    ?>
                    <div class="row">
                        <div class="icon <?= $entry['isDir'] ? 'folder' : '' ?>">
                            <?php if ($entry['isImage'] && $entry['previewUrl'] !== null): ?>
                                <img class="thumb" src="<?= h($entry['previewUrl']) ?>" alt="">
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
                                <?php if ((int) $entry['modifiedAt'] > 0): ?> | <?= h(date('M j, Y g:i A', (int) $entry['modifiedAt'])) ?><?php endif; ?>
                            </div>
                        </div>
                        <div class="actions">
                            <?php if ($entry['isImage'] || $entry['isVideo']): ?><button class="button preview-toggle" type="button" data-target="<?= h($previewId) ?>"><?= $entry['isVideo'] ? 'Play' : 'View' ?></button><?php endif; ?>
                            <?php if ($entry['isEditable']): ?><button class="button edit-button" type="button" data-path="<?= h($entry['path']) ?>" data-name="<?= h($entry['name']) ?>">Edit</button><?php endif; ?>
                            <a class="button-link" href="freecloud_download.php?path=<?= rawurlencode($entry['path']) ?>">Download</a>
                            <form method="post">
                                <input type="hidden" name="action" value="delete">
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
            body: new URLSearchParams({action: 'save_file', target_path: editorPath, content: editorTextarea.value})
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
