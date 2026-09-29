// Tiny SVG "product photos" for the dev mock (white background like real shop photos).
const wrap = (inner: string) =>
  `data:image/svg+xml;utf8,${encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400"><rect width="400" height="400" fill="#fff"/>${inner}</svg>`,
  )}`;

export const IMG = {
  iphone: wrap(`<defs><linearGradient id="a" x1="0" x2="1"><stop offset="0" stop-color="#f28c38"/><stop offset="1" stop-color="#c85a14"/></linearGradient></defs>
    <rect x="120" y="40" width="160" height="320" rx="30" fill="url(#a)"/><rect x="135" y="55" width="80" height="80" rx="20" fill="#b24f10"/>
    <circle cx="160" cy="80" r="16" fill="#222"/><circle cx="192" cy="110" r="16" fill="#222"/><circle cx="160" cy="112" r="10" fill="#333"/>
    <text x="200" y="250" font-family="Arial" font-size="40" text-anchor="middle" fill="#9a3f0c"></text>`),
  ps5: wrap(`<path d="M150 50 C140 150 130 300 140 350 L200 350 L200 50Z" fill="#f4f4f5" stroke="#d4d4d8" stroke-width="3"/>
    <path d="M250 50 C260 150 270 300 260 350 L200 350 L200 50Z" fill="#fafafa" stroke="#d4d4d8" stroke-width="3"/>
    <rect x="186" y="50" width="28" height="300" fill="#18181b"/><circle cx="200" cy="120" r="5" fill="#3b82f6"/>`),
  switch2: wrap(`<rect x="60" y="120" width="60" height="170" rx="28" fill="#00b8e6"/><rect x="280" y="120" width="60" height="170" rx="28" fill="#ff4d5a"/>
    <rect x="115" y="120" width="170" height="170" rx="8" fill="#27272a"/><rect x="128" y="132" width="144" height="146" rx="4" fill="#3f3f46"/>
    <circle cx="90" cy="170" r="10" fill="#1d1d1f"/><circle cx="310" cy="240" r="10" fill="#1d1d1f"/>`),
  airpods: wrap(`<rect x="110" y="120" width="180" height="160" rx="60" fill="#fafafa" stroke="#e4e4e7" stroke-width="4"/>
    <line x1="112" y1="190" x2="288" y2="190" stroke="#e4e4e7" stroke-width="3"/><circle cx="200" cy="230" r="5" fill="#a1a1aa"/>`),
  lego: wrap(`<rect x="90" y="170" width="220" height="140" rx="8" fill="#84cc16"/><rect x="120" y="120" width="160" height="60" rx="6" fill="#65a30d"/>
    <rect x="150" y="70" width="40" height="60" fill="#a16207"/><rect x="220" y="80" width="30" height="50" fill="#a16207"/>
    <circle cx="140" cy="165" r="12" fill="#4d7c0f"/><circle cx="200" cy="165" r="12" fill="#4d7c0f"/><circle cx="260" cy="165" r="12" fill="#4d7c0f"/>`),
  headphones: wrap(`<path d="M110 230 C110 110 290 110 290 230" fill="none" stroke="#3f3f46" stroke-width="22" stroke-linecap="round"/>
    <rect x="85" y="210" width="60" height="100" rx="28" fill="#27272a"/><rect x="255" y="210" width="60" height="100" rx="28" fill="#27272a"/>`),
  framework: wrap(`<rect x="80" y="100" width="240" height="160" rx="10" fill="#d4d4d8"/><rect x="92" y="112" width="216" height="136" rx="4" fill="#18181b"/>
    <path d="M60 270 L340 270 L320 300 L80 300Z" fill="#a1a1aa"/><rect x="170" y="272" width="60" height="8" rx="3" fill="#71717a"/>`),
};
