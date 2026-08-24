(function () {
  const links = [
    { label: 'Style References', href: '/stylereferences.html' },
    { label: 'Shelf Palette', href: '/shelf-colors.html' },
    { label: 'Color Comparison Tool', href: '/color-comparison.html' }
  ];

  const style = document.createElement('style');
  style.textContent = `
    #devtools-btn {
      position: fixed;
      bottom: 20px;
      right: 20px;
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: var(--bg-card, #2C2326);
      border: 2px solid var(--border, #3B2B2F);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      z-index: 9999;
      transition: border-color 0.2s;
    }
    #devtools-btn:hover {
      border-color: var(--accent, #807b4d);
    }
    #devtools-btn svg {
      width: 20px;
      height: 20px;
      fill: var(--text-secondary, #CDC9CA);
    }
    #devtools-btn:hover svg {
      fill: var(--accent, #807b4d);
    }
    #devtools-panel {
      position: fixed;
      bottom: 74px;
      right: 20px;
      background: var(--bg-card, #2C2326);
      border: 2px solid var(--border, #3B2B2F);
      border-radius: 10px;
      padding: 8px;
      min-width: 200px;
      display: none;
      flex-direction: column;
      gap: 4px;
      z-index: 9999;
    }
    #devtools-panel.open {
      display: flex;
    }
    #devtools-panel .devtools-title {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-secondary, #CDC9CA);
      padding: 6px 10px 4px;
    }
    #devtools-panel a {
      color: var(--text-primary, #E6E4E4);
      text-decoration: none;
      font-size: 0.9rem;
      padding: 8px 10px;
      border-radius: 6px;
      transition: background 0.15s;
    }
    #devtools-panel a:hover {
      background: rgba(128, 123, 77, 0.15);
      color: var(--accent, #807b4d);
    }
  `;
  document.head.appendChild(style);

  const btn = document.createElement('div');
  btn.id = 'devtools-btn';
  btn.title = 'Dev tools';
  btn.innerHTML = `
    <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
      <path d="M22.7 19l-9.1-9.1c.9-2.3.4-5-1.5-6.9-2-2-5-2.4-7.4-1.3L9 6l-3 3-4.3-4.3C.6 7.1 1 10.1 3 12.1c1.9 1.9 4.6 2.4 6.9 1.5l9.1 9.1c.4.4 1 .4 1.4 0l2.3-2.3c.4-.4.4-1 0-1.4z"/>
    </svg>
  `;

  const panel = document.createElement('div');
  panel.id = 'devtools-panel';
  panel.innerHTML = `<div class="devtools-title">Dev tools</div>` +
    links.map(l => `<a href="${l.href}">${l.label}</a>`).join('');

  btn.addEventListener('click', () => {
    panel.classList.toggle('open');
  });

  document.addEventListener('click', (e) => {
    if (!panel.contains(e.target) && !btn.contains(e.target)) {
      panel.classList.remove('open');
    }
  });

  document.body.appendChild(btn);
  document.body.appendChild(panel);
})();
