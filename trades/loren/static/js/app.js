/** TelVictory — shared UI utilities */
const AppUI = {
  toast(message, type = 'success') {
    let root = document.getElementById('toast-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'toast-root';
      document.body.appendChild(root);
    }
    const el = document.createElement('div');
    el.className = 'toast ' + type;
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => el.remove(), 3500);
  },

  initSettingsTabs() {
    const tabs = document.querySelectorAll('.settings-tab');
    const panels = document.querySelectorAll('.settings-panel');
    if (!tabs.length) return;

    const activate = (id) => {
      tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === id));
      panels.forEach((p) => p.classList.toggle('active', p.id === 'panel-' + id));
      try { history.replaceState(null, '', '#tab-' + id); } catch (e) {}
    };

    tabs.forEach((tab) => {
      tab.addEventListener('click', () => activate(tab.dataset.tab));
    });

    const hash = location.hash.replace(/^#tab-/, '').split('&')[0];
    if (hash && document.getElementById('panel-' + hash)) activate(hash);
    else if (tabs[0]) activate(tabs[0].dataset.tab);
  },

  setButtonLoading(button, text = 'Loading...') {
    if (!button) return;
    button.dataset.originalHtml = button.innerHTML;
    button.classList.add('btn-loading');
    button.innerHTML = '<span class="btn-text">' + text + '</span><div class="btn-spinner"></div>';
    button.disabled = true;
  },

  resetButton(button) {
    if (!button) return;
    button.classList.remove('btn-loading');
    button.innerHTML = button.dataset.originalHtml || button.innerText;
    button.disabled = false;
  },
};

// Alias for legacy templates
const LoadingUtils = {
  setButtonLoading: (btn, t) => AppUI.setButtonLoading(btn, t),
  resetButton: (btn) => AppUI.resetButton(btn),
  showPageLoader: (text) => {
    const l = document.getElementById('page-loader');
    if (l) {
      const tEl = l.querySelector('.page-loader-text');
      if (tEl) tEl.textContent = text || 'Loading...';
      l.classList.add('active');
    }
  },
  hidePageLoader: () => {
    const l = document.getElementById('page-loader');
    if (l) l.classList.remove('active');
  },
};

function showNotification(message, type) {
  AppUI.toast(message, type === 'error' ? 'error' : type === 'warning' ? 'warning' : 'success');
}

document.addEventListener('DOMContentLoaded', () => {
  AppUI.initSettingsTabs();
  const notifBadge = document.getElementById('notif-count');
  if (notifBadge) {
    const load = () =>
      fetch('/api/notifications')
        .then((r) => r.json())
        .then((d) => { notifBadge.textContent = d.count || 0; })
        .catch(() => {});
    load();
    setInterval(load, 30000);
  }
});
