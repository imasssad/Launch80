<?php
/**
 * Launch80 private dashboard.
 *
 * Serves the bot's full monitor (stats, positions, events) over the public
 * domain, behind HTTP Basic Auth. The bot itself stays bound to 127.0.0.1;
 * this page is the only thing the internet can reach.
 *
 * Install: copy to <docroot>/index.php and create the password file ABOVE the
 * docroot (so it can never be served):
 *
 *   php -r 'echo password_hash("your-strong-password", PASSWORD_DEFAULT), "\n";' \
 *     > /var/www/vhosts/launch80.everests.fund/dashboard.pass
 *   chmod 600 /var/www/vhosts/launch80.everests.fund/dashboard.pass
 *
 * Fails closed: with no password file it serves 503 rather than exposing data.
 */
declare(strict_types=1);

const BOT      = 'http://127.0.0.1:8088';
const PASSFILE = __DIR__ . '/../dashboard.pass';

// ---- never cache: an authenticated page must not sit in a shared cache -----
// Cloudflare (or any proxy) fronting this would otherwise be able to serve one
// authorised response to everybody. Sent before the auth check so the 401 is
// uncacheable too.
header('Cache-Control: no-store, no-cache, must-revalidate, private, max-age=0');
header('Pragma: no-cache');
header('Vary: Authorization');

// ---- auth (fail closed) ----------------------------------------------------
if (!is_readable(PASSFILE)) {
    http_response_code(503);
    header('Content-Type: text/plain');
    exit("Dashboard not configured: password file missing. Refusing to serve data.\n");
}
$hash = trim((string)file_get_contents(PASSFILE));

/**
 * Read Basic credentials.
 *
 * PHP_AUTH_USER/PW are only populated when PHP runs as an Apache module. Under
 * CGI/FastCGI/PHP-FPM the Authorization header is not forwarded into them, so
 * the raw header is parsed as a fallback (REDIRECT_ prefix appears when
 * mod_rewrite forwards it). Without this the page can never authenticate on a
 * FastCGI host.
 */
$pass = $_SERVER['PHP_AUTH_PW'] ?? '';
if ($pass === '') {
    $header = $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '';
    if (stripos($header, 'basic ') === 0) {
        $decoded = base64_decode(substr($header, 6), true);
        if ($decoded !== false && str_contains($decoded, ':')) {
            [, $pass] = explode(':', $decoded, 2);
        }
    }
}
if ($hash === '' || $pass === '' || !password_verify($pass, $hash)) {
    header('WWW-Authenticate: Basic realm="Launch80", charset="UTF-8"');
    http_response_code(401);
    exit("Authentication required.\n");
}

// ---- data ------------------------------------------------------------------
function bot(string $path) {
    $ch = curl_init(BOT . $path);
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 6]);
    $body = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    if ($body === false || $code !== 200) return null;
    return json_decode($body, true);
}
function e($v): string { return htmlspecialchars((string)$v, ENT_QUOTES, 'UTF-8'); }
function usd(int $u): string { return number_format($u / 1_000_000, 6); }

$health    = bot('/health');
$stats     = bot('/stats');
$positions = bot('/positions') ?? [];
$events    = bot('/events') ?? [];
$offline   = $health === null;
$live      = ($health['mode'] ?? '') === 'live';
?><!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Launch80 Dashboard</title>
<style>
  :root {
    --bg:#f6f7f9; --card:#fff; --text:#16191d; --muted:#646b75; --line:#e3e6ea;
    --ok:#1a7f46; --okbg:#e7f5ed; --warn:#8a6100; --warnbg:#fdf3dc;
    --bad:#a3242b; --badbg:#fbeaea;
  }
  @media (prefers-color-scheme: dark) { :root:not([data-theme=light]) {
    --bg:#101316; --card:#171b20; --text:#e8eaed; --muted:#9aa2ad; --line:#2a3038;
    --ok:#58d08a; --okbg:#15291f; --warn:#e3b341; --warnbg:#2a2314;
    --bad:#f08a8a; --badbg:#2d1718;
  }}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:20px 16px}
  .wrap{max-width:1000px;margin:0 auto}
  header{display:flex;flex-wrap:wrap;gap:10px;align-items:baseline;margin-bottom:16px}
  h1{margin:0;font-size:20px;letter-spacing:-.01em}
  .muted{color:var(--muted);font-size:13px}
  .tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .tag.paper{background:var(--okbg);color:var(--ok)} .tag.live{background:var(--badbg);color:var(--bad)}
  .state{display:flex;align-items:center;gap:9px;padding:12px 14px;border-radius:10px;
    font-weight:600;margin-bottom:16px}
  .state.ok{background:var(--okbg);color:var(--ok)} .state.warn{background:var(--warnbg);color:var(--warn)}
  .state.bad{background:var(--badbg);color:var(--bad)}
  .dot{width:9px;height:9px;border-radius:50%;background:currentColor;flex:none}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .k{color:var(--muted);font-size:12px;margin-bottom:3px}
  .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
  .v.pos{color:var(--ok)} .v.neg{color:var(--bad)}
  h2{font-size:14px;margin:20px 0 8px;color:var(--muted);font-weight:600;
     text-transform:uppercase;letter-spacing:.04em}
  .scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{padding:9px 12px;text-align:left;white-space:nowrap}
  th{color:var(--muted);font-weight:600;font-size:12px;border-bottom:1px solid var(--line)}
  tr+tr td{border-top:1px solid var(--line)}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  .pill{padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600;background:var(--line)}
  .pill.OPEN{background:var(--warnbg);color:var(--warn)}
  .pill.CLOSED{background:var(--okbg);color:var(--ok)}
  .pill.ABANDONED,.pill.RECOVERY_BUY,.pill.RECOVERY_SELL,.pill.MANUAL_EXIT{background:var(--badbg);color:var(--bad)}
  .empty{padding:16px;color:var(--muted);font-size:13px}
  footer{margin-top:18px;color:var(--muted);font-size:12px;text-align:center}
</style>
</head>
<body><div class="wrap">

<header>
  <h1>Launch80</h1>
  <?php if (!$offline): ?>
    <span class="tag <?= $live ? 'live' : 'paper' ?>"><?= e($health['mode'] ?? '?') ?></span>
  <?php endif; ?>
  <span class="muted">private dashboard</span>
</header>

<?php
$cls = 'ok'; $msg = $live ? 'Running — LIVE MODE' : 'Running — paper mode';
if ($offline)                        { $cls = 'bad';  $msg = 'Offline — bot not responding'; }
elseif (!empty($health['unreconciled'])) { $cls = 'bad';  $msg = 'Needs attention — unreconciled trade'; }
elseif (!empty($health['paused']))   { $cls = 'warn'; $msg = 'Running — new buys paused'; }
?>
<div class="state <?= $cls ?>"><span class="dot"></span><?= e($msg) ?></div>

<?php if ($stats): $r = (float)($stats['utc_daily_realized_usd'] ?? 0); ?>
<div class="grid">
  <div class="card"><div class="k">Realized today</div>
    <div class="v <?= $r > 0 ? 'pos' : ($r < 0 ? 'neg' : '') ?>"><?= ($r >= 0 ? '+' : '') . number_format($r, 4) ?></div></div>
  <div class="card"><div class="k">Exposure</div>
    <div class="v">$<?= number_format((float)($stats['exposure_usd'] ?? 0), 2) ?></div></div>
  <div class="card"><div class="k">New buys</div>
    <div class="v"><?= !empty($stats['paused']) ? 'paused' : 'on' ?></div></div>
  <div class="card"><div class="k">Positions</div>
    <div class="v"><?= array_sum($stats['status'] ?? []) ?></div></div>
</div>
<?php endif; ?>

<h2>Positions</h2>
<div class="scroll">
<?php if (!$positions): ?><div class="empty">No positions yet.</div><?php else: ?>
<table><thead><tr>
  <th>Status</th><th>Symbol</th><th>Chain</th><th class="num">Spent</th>
  <th class="num">Received</th><th class="num">PnL</th><th class="num">UW</th><th>Note</th>
</tr></thead><tbody>
<?php foreach (array_slice($positions, 0, 40) as $p):
  $sp = (int)($p['buy_spent_units'] ?? 0); $rc = (int)($p['sell_received_units'] ?? 0);
  $done = in_array($p['status'] ?? '', ['CLOSED', 'ABANDONED'], true);
  $pnl = $done ? ($rc - $sp) / 1_000_000 : null; ?>
  <tr>
    <td><span class="pill <?= e($p['status'] ?? '') ?>"><?= e($p['status'] ?? '') ?></span></td>
    <td><?= e($p['symbol'] ?? '') ?></td>
    <td class="muted"><?= e($p['chain'] ?? '') ?></td>
    <td class="num"><?= usd($sp) ?></td>
    <td class="num"><?= $rc ? usd($rc) : '—' ?></td>
    <td class="num"><?= $pnl === null ? '—' : ($pnl >= 0 ? '+' : '') . number_format($pnl, 4) ?></td>
    <td class="num"><?= (int)($p['underwater_checks'] ?? 0) ?></td>
    <td class="muted"><?= e(mb_strimwidth((string)($p['last_error'] ?? ''), 0, 46, '…')) ?></td>
  </tr>
<?php endforeach; ?>
</tbody></table>
<?php endif; ?>
</div>

<h2>Recent events</h2>
<div class="scroll">
<?php if (!$events): ?><div class="empty">No events yet.</div><?php else: ?>
<table><thead><tr><th>Time (UTC)</th><th>Event</th><th>Detail</th></tr></thead><tbody>
<?php foreach (array_slice($events, 0, 30) as $ev): ?>
  <tr>
    <td class="muted"><?= e(gmdate('H:i:s', (int)($ev['at'] ?? 0))) ?></td>
    <td><?= e($ev['kind'] ?? '') ?></td>
    <td class="muted"><?= e(mb_strimwidth((string)($ev['detail'] ?? ''), 0, 70, '…')) ?></td>
  </tr>
<?php endforeach; ?>
</tbody></table>
<?php endif; ?>
</div>

<footer>Generated <?= gmdate('Y-m-d H:i:s') ?> UTC · reload to refresh</footer>
</div></body></html>
