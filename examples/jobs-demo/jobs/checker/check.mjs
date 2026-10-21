// The checker job of jobs-demo: proves what a test checker relies on, and answers with its exit code (0 passed, 3 failed).
// It reads the worker's result from the bucket (BUCKET_RUNS, read only) and checks that the test image holds the base
// application's packages and npm, that /work is writable, and that the internet is out of reach.
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, writeFileSync } from 'node:fs';
import { connect } from 'node:net';

const runId = process.env.RUN_ID;
const bucket = process.env.BUCKET_RUNS;
const token = async () => (await (await fetch('http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token',
  { headers: { 'Metadata-Flavor': 'Google' } })).json()).access_token;
const reachable = () => new Promise((done) => {
  const socket = connect({ host: 'example.com', port: 443, timeout: 5000 });
  socket.on('connect', () => { socket.destroy(); done(true); });
  socket.on('error', () => done(false));
  socket.on('timeout', () => { socket.destroy(); done(false); });
});

const checks = {};
const name = encodeURIComponent(`runs/${runId}/worker.json`);
const answer = await fetch(`https://storage.googleapis.com/storage/v1/b/${bucket}/o/${name}?alt=media`, { headers: { Authorization: `Bearer ${await token()}` } });
checks.worker_result_read = answer.ok && (await answer.json()).run === runId;
checks.base_packages = existsSync('/deps/node_modules') && readdirSync('/deps/node_modules').length > 0;
checks.npm = execFileSync('npm', ['--version']).toString().trim().length > 0;
try { writeFileSync('/work/probe', 'ok'); checks.work_writable = true; } catch { checks.work_writable = false; }
checks.no_internet = !(await reachable());
const passed = Object.values(checks).every(Boolean);
console.log(JSON.stringify({ severity: passed ? 'INFO' : 'ERROR', message: `run ${runId}: checker ${passed ? 'passed' : 'failed'}`, checks }));
process.exit(passed ? 0 : 3);
