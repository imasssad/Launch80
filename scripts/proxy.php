<?php
/**
 * Reverse proxy: public domain -> the local Launch80 FastAPI monitor.
 *
 * For hosts (shared Plesk) that expose no nginx/Apache proxy configuration.
 * It forwards method, path, query, body, cookies and the CSRF header, and
 * passes the response status, content type and Set-Cookie back unchanged.
 *
 * This adds NO authentication of its own and bypasses none: the app behind it
 * requires a session on every data route, so an unauthenticated request
 * proxied through here still gets a 401. It only moves bytes.
 *
 * Install into the docroot as index.php, with .htaccess routing all paths to
 * it (see scripts/htaccess-proxy).
 */
declare(strict_types=1);

const UPSTREAM = 'http://127.0.0.1:8088';

// Only forward paths the app actually serves. An explicit list keeps a
// traversal or a stray docroot file from being reachable through the proxy.
const ALLOWED = ['/', '/health', '/stats', '/positions', '/events', '/pause', '/resume',
                 '/api/auth/session', '/api/auth/login', '/api/auth/logout',
                 '/api/auth/logout-everywhere', '/api/auth/sessions'];

$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
if (!in_array($path, ALLOWED, true)) {
    http_response_code(404);
    header('Content-Type: text/plain');
    exit("Not found\n");
}

$query  = $_SERVER['QUERY_STRING'] ?? '';
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$url    = UPSTREAM . $path . ($query !== '' ? '?' . $query : '');

$headers = ['Accept: ' . ($_SERVER['HTTP_ACCEPT'] ?? '*/*')];
if (!empty($_SERVER['HTTP_COOKIE']))        $headers[] = 'Cookie: ' . $_SERVER['HTTP_COOKIE'];
if (!empty($_SERVER['HTTP_X_CSRF_TOKEN']))  $headers[] = 'X-CSRF-Token: ' . $_SERVER['HTTP_X_CSRF_TOKEN'];
if (!empty($_SERVER['CONTENT_TYPE']))       $headers[] = 'Content-Type: ' . $_SERVER['CONTENT_TYPE'];
// The app trusts this only because the connection comes from loopback; it is
// what makes the per-IP login throttle see real clients.
$headers[] = 'X-Forwarded-For: ' . ($_SERVER['REMOTE_ADDR'] ?? '');
$headers[] = 'X-Forwarded-Proto: https';

$ch = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HEADER         => true,
    CURLOPT_TIMEOUT        => 20,
    CURLOPT_HTTPHEADER     => $headers,
    CURLOPT_CUSTOMREQUEST  => $method,
    CURLOPT_FOLLOWLOCATION => false,
]);
if ($method === 'POST' || $method === 'PUT' || $method === 'PATCH') {
    curl_setopt($ch, CURLOPT_POSTFIELDS, file_get_contents('php://input') ?: '');
}

$raw = curl_exec($ch);
if ($raw === false) {
    http_response_code(502);
    header('Content-Type: application/json');
    exit(json_encode(['ok' => false, 'error' => 'bot unreachable']));
}
$status     = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$headerSize = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
curl_close($ch);

$rawHeaders = substr($raw, 0, $headerSize);
$body       = substr($raw, $headerSize);

http_response_code($status);
foreach (explode("\r\n", $rawHeaders) as $line) {
    if (!str_contains($line, ':')) continue;
    [$name, $value] = explode(':', $line, 2);
    $name = strtolower(trim($name));
    $value = trim($value);
    // Set-Cookie must pass through for sessions; the rest is a small, explicit
    // allowlist so nothing unexpected is relayed to the browser.
    if ($name === 'set-cookie') {
        header('Set-Cookie: ' . $value, false);
    } elseif (in_array($name, ['content-type', 'cache-control', 'www-authenticate',
                               'x-frame-options', 'x-content-type-options',
                               'referrer-policy', 'content-security-policy'], true)) {
        header(ucfirst($name) . ': ' . $value);
    }
}
echo $body;
