// Uptime check, run by CloudWatch Synthetics every 15 minutes (Chapter 3).
//
// It calls the site the way a user does: over HTTPS, through CloudFront, the
// load balancer and the app, to /readyz (which also checks the database).
// Server-side metrics can't see a broken CDN, certificate or DNS; this can.
//
// Plain fetch, no browser: a page load would cost more and test nothing extra.
// Throwing marks the run as failed; the SuccessPercent metric drives an alarm.

exports.handler = async () => {
  const url = `${process.env.TARGET_URL}/readyz`;
  const started = Date.now();
  const response = await fetch(url, { signal: AbortSignal.timeout(10_000) });
  const body = await response.text();
  console.log(JSON.stringify({ url, status: response.status, duration_ms: Date.now() - started }));
  if (response.status !== 200) {
    throw new Error(`${url} returned ${response.status}: ${body.slice(0, 200)}`);
  }
  return "ok";
};
