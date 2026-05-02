const fs = require('fs');
const path = require('path');
const https = require('https');

const TOKEN = process.env.GITHUB_PERSONAL_ACCESS_TOKEN2;
const OWNER = 'aichaoukdour';
const REPO  = 'FallahAlert';

function apiRequest(method, endpoint, body) {
  return new Promise((resolve, reject) => {
    const data = body ? JSON.stringify(body) : null;
    const options = {
      hostname: 'api.github.com',
      path: endpoint,
      method,
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'FellahAlert-Push',
        ...(data ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) } : {}),
      },
    };
    const req = https.request(options, (res) => {
      let buf = '';
      res.on('data', c => buf += c);
      res.on('end', () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(buf) }); }
        catch { resolve({ status: res.statusCode, data: buf }); }
      });
    });
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

const DIR = path.join(__dirname, '..');
const FILES = [
  '.github/workflows/deploy.yml',
];

async function upsertFile(relPath) {
  const fullPath = path.join(DIR, 'fellahalert', relPath);
  if (!fs.existsSync(fullPath)) { console.log('  SKIP (not found):', relPath); return; }

  const content = Buffer.from(fs.readFileSync(fullPath, 'utf8')).toString('base64');
  const endpoint = `/repos/${OWNER}/${REPO}/contents/${relPath}`;

  const getRes = await apiRequest('GET', endpoint);
  const body = { message: `Add ${relPath}`, content, branch: 'main' };
  if (getRes.status === 200 && getRes.data.sha) body.sha = getRes.data.sha;

  const putRes = await apiRequest('PUT', endpoint, body);
  if (putRes.status === 201 || putRes.status === 200) {
    console.log('  ✓', relPath);
  } else {
    console.error('  ✗', relPath, putRes.status, JSON.stringify(putRes.data).slice(0, 150));
  }
}

async function main() {
  console.log('Pushing GitHub Actions workflow...\n');
  for (const f of FILES) await upsertFile(f);
  console.log('\n✅ Done! → https://github.com/' + OWNER + '/' + REPO + '/actions');
}

main().catch(e => { console.error('Fatal:', e); process.exit(1); });
