const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); const dir = process.cwd();
  const p = await b.newPage({ viewport: { width: 800, height: 800 } });
  await p.goto('file://' + dir + '/banner.html'); await p.setContent(`<style>body{margin:0}</style><img src="file://${dir}/icon.svg" width=800 height=800>`);
  await p.waitForTimeout(300); await p.screenshot({ path: 'profile-800.png' });
  // circle-crop preview to check how it looks on YouTube/TikTok
  await p.setContent(`<body style="margin:0;background:#fff;display:flex;gap:30px;padding:30px"><img src="file://${dir}/icon.svg" width=200 style="border-radius:50%"><img src="file://${dir}/icon.svg" width=64 style="border-radius:50%;align-self:center"><img src="file://${dir}/icon.svg" width=32 style="border-radius:50%;align-self:center"></body>`);
  await p.setViewportSize({ width: 420, height: 260 }); await p.waitForTimeout(300); await p.screenshot({ path: '/tmp/claude-0/-home-user-Ttttwsttwi-/0a44daa8-5d5d-5e5c-b5d8-2fab99a02a87/scratchpad/circle.png' });
  const q = await b.newPage({ viewport: { width: 2560, height: 1440 } });
  await q.goto('file://' + dir + '/banner.html'); await q.waitForTimeout(500); await q.screenshot({ path: 'youtube-banner-2560x1440.png' });
  await b.close();
})();
// check: content must fit YouTube's mobile-safe area (center 1546x423)
(async () => {
  const b = await chromium.launch(); const q = await b.newPage({ viewport: { width: 2560, height: 1440 } });
  await q.goto('file://' + process.cwd() + '/banner.html'); await q.waitForTimeout(300);
  const r = await q.evaluate(() => { const w = document.querySelector('.wrap > img').getBoundingClientRect(); const t = document.querySelector('.wrap > div').getBoundingClientRect(); return { left: w.left, right: t.right, top: Math.min(w.top, t.top), bottom: Math.max(w.bottom, t.bottom) }; });
  console.log('content box', r, 'safe area x 507-2053, y 508-931');
  await b.close();
})();
