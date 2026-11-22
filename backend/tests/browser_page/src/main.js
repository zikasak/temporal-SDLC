// A page whose requirement popup is drawn as `?popup=` says: `inside` the scrolling panel,
// where the panel cuts it off; `portaled` to the body, fully visible; `narrow-bug` inside
// the panel below 600 px only. It lists the items of GET /api/items.
const variant = new URLSearchParams(location.search).get('popup') || 'inside';
const reference = document.getElementById('ref');
let popup = null;

function inside() {
  return variant === 'inside'
    || (variant === 'narrow-bug' && matchMedia('(max-width: 600px)').matches);
}

function show() {
  if (popup) return;
  popup = document.createElement('span');
  popup.setAttribute('role', 'tooltip');
  popup.textContent = 'FR-1: requirement popups are fully visible, on top, inside the window.';
  if (inside()) {
    popup.className = 'popup inside';
    reference.parentElement.append(popup);
  } else {
    popup.className = 'popup portaled';
    document.body.append(popup);
  }
}

reference.addEventListener('mouseenter', show);
reference.addEventListener('click', show);

fetch('/api/items')
  .then((response) => (response.ok ? response.json() : Promise.reject(new Error(String(response.status)))))
  .then((body) => { document.getElementById('items').textContent = body.items.join(', '); })
  .catch(() => { document.getElementById('items').textContent = 'unavailable'; });
