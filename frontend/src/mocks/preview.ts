/* Mock /items/preview: a few canned outcomes so every quick-check state can be seen in VITE_MOCK=1.
 *   bestbuy.com / target.com / … (registry)  → dedicated          amazon.com → third-party seller
 *   apple.com                                 → Apple              …shopify… / myshopify.com → platform
 *   …blocked…                                 → bot protection     …404… / …missing… → dead link
 *   …unclear…                                 → generic, unknown   anything else → generic, out of stock
 *   …slow… (any of the above)                 → 8 s delay (see install.ts)
 */
import type { Preview, Support } from '@/lib/types';
import { IMG } from './images';
import { retailerForUrl } from './retailers';

const host = (u: string) => {
  try {
    return new URL(u).hostname.replace(/^www\./, '');
  } catch {
    return u;
  }
};

const BLOCKED = "Store blocked our checker — try again later or use a real-browser setup.";
const RULE_HINT =
  'Add it anyway and switch the stock rule to CSS selector or Text match so it knows what “in stock” looks like on this page.';

function base(u: string): Preview {
  return {
    name: null,
    image_url: null,
    price: null,
    status: 'unknown',
    is_apple: false,
    retailer: retailerForUrl(u),
    error: null,
    status_text: null,
    adapter: null,
    fetched_via: 'http',
    seller: null,
    third_party: null,
    cart_url: null,
    signals: [],
    blocked: false,
    queued: false,
  };
}

const support = (level: Support['level'], label: string, detail: string): Support => ({ level, label, detail });

export function mockPreview(u: string): Preview {
  const p = base(u);
  const h = host(u);
  if (/blocked/.test(u)) {
    return {
      ...p,
      status: 'error',
      error: `Blocked by bot protection on ${h}`,
      status_text: 'Blocked by bot protection',
      fetched_via: 'browser',
      blocked: true,
      support: support('blocked', 'Blocked by bot protection', BLOCKED),
    };
  }
  if (/404|missing/.test(u)) {
    const err = 'Page not found (HTTP 404) — update the link';
    return { ...p, status: 'error', error: err, status_text: 'Page not found (HTTP 404)', support: support('unsupported', 'Page not found', err) };
  }
  if (/apple\.com/.test(u)) {
    return {
      ...p,
      name: 'iPhone 17 Pro',
      image_url: IMG.iphone,
      price: '$1,099.00',
      is_apple: true,
      support: support('dedicated', 'Dedicated support', 'Apple Store integration — pick models and watch delivery or pickup near you.'),
    };
  }
  if (/amazon\./.test(h)) {
    return {
      ...p,
      name: 'Sony WH-1000XM6 Wireless Noise Canceling Headphones',
      image_url: IMG.headphones,
      price: '$429.99',
      status: 'in_stock',
      status_text: 'In Stock',
      adapter: 'amazon',
      fetched_via: 'curl',
      seller: 'GadgetDeals LLC',
      third_party: true,
      signals: ["buybox: 'Add to Cart' (enabled)", "merchant: 'GadgetDeals LLC' (third-party)", 'offer-listing: 3 new offers'],
      support: support('dedicated', 'Dedicated support', 'Amazon has a dedicated integration.'),
    };
  }
  if (p.retailer) {
    return {
      ...p,
      name: 'Nintendo Switch 2 Console',
      image_url: IMG.switch2,
      price: '$749.99',
      status: 'in_stock',
      status_text: 'Add to Cart',
      adapter: p.retailer.key,
      fetched_via: /slow/.test(u) ? 'browser' : 'curl',
      seller: p.retailer.name,
      third_party: false,
      cart_url: `https://${p.retailer.domain}/cart?add=6614313`,
      signals: ["fulfillment: shipping 'Get it by Fri, Oct 3'", "button: 'Add to Cart' (enabled)"],
      support: support(
        'dedicated',
        'Dedicated support',
        `${p.retailer.name} has a dedicated integration${p.retailer.note ? ` — ${p.retailer.note}.` : '.'}`,
      ),
    };
  }
  if (/shopify/.test(u)) {
    return {
      ...p,
      name: 'Keychron Q1 Max QMK/VIA Wireless Custom Mechanical Keyboard',
      image_url: IMG.framework,
      price: '$219.00',
      status: 'in_stock',
      status_text: 'In stock (3 variants available)',
      adapter: 'shopify',
      cart_url: `https://${h}/cart/44871234567:1`,
      signals: ['shopify: /products/q1-max.js', 'variant 44871234567: available=true', 'inventory_policy: deny'],
      support: support(
        'platform',
        'Auto-detected Shopify store',
        'Checked with the built-in Shopify recipe, which works for any Shopify store.',
      ),
    };
  }
  if (/unclear/.test(u)) {
    return {
      ...p,
      name: 'Handmade Ceramic Pour-Over Set',
      image_url: IMG.airpods,
      price: '$64.00',
      status: 'unknown',
      adapter: 'generic',
      signals: ["json-ld: Product 'Handmade Ceramic Pour-Over Set' (no availability)", "button: 'Notify me' (enabled)"],
      support: support('generic', 'Stock status unclear', `The page loaded but generic detection couldn't read stock status. ${RULE_HINT}`),
    };
  }
  return {
    ...p,
    name: 'LEGO Icons Botanical Garden 10345',
    image_url: IMG.lego,
    price: '$99.99',
    status: 'out_of_stock',
    status_text: 'Sold out',
    adapter: 'generic',
    signals: ['json-ld: OutOfStock', "button: 'Sold out' (disabled)"],
    support: support(
      'generic',
      'Works with generic detection',
      "Not a dedicated integration, but the page's stock signals were clear enough to read.",
    ),
  };
}
