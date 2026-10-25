// Prefill the request form from an image (model vision) or pasted text.
const form = document.getElementById('extract-form');
if (form) {
  const textarea = document.getElementById('extract-text');
  const file = document.getElementById('extract-image');
  const msg = document.getElementById('extract-msg');
  const items = form.closest('main').querySelector('textarea[name="items"]');

  // Shrink to <=1400px before base64: keeps the request small and the cost low.
  function toDataUrl(f) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(f);
      img.onload = () => {
        const scale = Math.min(1, 1400 / Math.max(img.width, img.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        resolve(canvas.toDataURL('image/jpeg', 0.85));
      };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('gagal membaca gambar')); };
      img.src = url;
    });
  }

  form.addEventListener('submit', async e => {
    e.preventDefault();
    const hasImage = file.files && file.files[0];
    if (!hasImage && !textarea.value.trim()) {
      msg.textContent = 'Isi teks atau pilih gambar dulu.';
      return;
    }
    msg.textContent = hasImage ? 'AI membaca gambar…' : 'memproses…';
    try {
      const body = { text: textarea.value };
      if (hasImage) body.image = await toDataUrl(file.files[0]);
      const res = await fetch('/api/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'gagal');
      if (!data.items.length) { msg.textContent = 'Tidak ada item terbaca.'; return; }
      items.value = data.items
        .map(i => `${i.name} | ${i.qty} | ${i.unit} | ${i.est_price}`)
        .join('\n');
      msg.textContent = `${data.items.length} item dimasukkan (${data.source === 'model' ? 'AI' : 'parser'}).`;
      items.focus();
    } catch (err) {
      msg.textContent = 'Gagal: ' + err.message;
    }
  });
}
