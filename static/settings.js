// Test the saved AI config with one tiny prompt.
const btn = document.getElementById('test-btn');
if (btn) {
  const msg = document.getElementById('test-msg');
  btn.addEventListener('click', async () => {
    msg.textContent = 'menguji…';
    try {
      const res = await fetch('/settings/test', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'gagal');
      msg.textContent = `OK — ${data.sample.length} item terbaca dari contoh.`;
    } catch (err) {
      msg.textContent = 'Gagal: ' + err.message;
    }
  });
}
