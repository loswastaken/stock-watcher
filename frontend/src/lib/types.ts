// Mirrors the REST contract in PLAN.md. Keep in sync.

export type ItemStatus = 'in_stock' | 'out_of_stock' | 'unknown' | 'error';
export type ItemKind = 'generic' | 'apple';
export type Theme = 'dark' | 'light' | 'system';

export interface User {
  id: number;
  username: string;
  is_admin: boolean;
  created_at: string;
}

export interface AuthStatus {
  setup_required: boolean;
  user: User | null;
}

export interface Health {
  status: 'ok';
  version: string;
}

export interface Settings {
  ntfy_server: string;
  ntfy_topic: string | null;
  ntfy_token_set: boolean;
  ntfy_priority: number;
  default_interval_minutes: number;
  default_zip: string | null;
  default_max_distance_miles: number;
  notify_on_out_of_stock: boolean;
  theme: Theme;
  /** Retailer keys whose restocks never alert. */
  muted_retailers: string[];
  /** Keep alerts armed after an alert (alert again on every new restock). */
  auto_rearm: boolean;
  /** Beep in the web app when a new alert arrives. */
  alert_sound: boolean;
}

/** PUT /settings is partial. `ntfy_token: ""` clears; omitted keeps. */
export type SettingsUpdate = Partial<Omit<Settings, 'ntfy_token_set'>> & {
  ntfy_token?: string;
};

export interface TestNotificationResult {
  ok: boolean;
  error: string | null;
}

/** A supported store (GET /retailers). */
export interface Retailer {
  key: string;
  name: string;
  domain: string;
  /** Every host this store's product URLs can live on (domain first). */
  hosts?: string[];
  color: string;
  /** Supports in-store pickup near a ZIP. */
  pickup: boolean;
  /** Can tell official (first-party) sellers from marketplace sellers. */
  seller_filter: boolean;
  note: string | null;
}

export type Fulfillment = 'delivery' | 'pickup' | 'any';

export interface RetailerConfig {
  fulfillment: Fulfillment;
  zip: string | null;
  radius_miles: number;
  store_id?: string | null;
  official_only: boolean;
  condition?: 'new' | 'any';
}

/** One store tracking the same product (GET /items/{id}/stores). */
export interface StoreRow {
  id: number;
  name: string;
  url: string;
  retailer: Retailer | null;
  status: ItemStatus;
  status_text: string;
  price: string | null;
  max_price: number | null;
  enabled: boolean;
  notify_enabled: boolean;
  last_in_stock_at: string | null;
  last_checked_at: string | null;
}

export interface Restock {
  id: number;
  checked_at: string;
  status_text: string;
}

export type GenericMode = 'auto' | 'selector' | 'text';

export interface GenericConfig {
  mode: GenericMode;
  selector: string | null;
  in_stock_text: string | null;
  out_of_stock_text: string | null;
  render_js: boolean;
}

export interface ApplePart {
  part_number: string;
  label: string;
}

export interface AppleConfig {
  parts: ApplePart[];
  zip: string;
  max_distance_miles: number;
  watch_pickup: boolean;
  watch_delivery: boolean;
  /** Only alert for same-day pickup (default true). Off = also alert for later pickup dates. */
  pickup_today_only: boolean;
}

export interface AppleStorePart {
  part_number: string;
  label: string;
  available: boolean;
  /** Available for pickup today (vs. a later date). */
  today?: boolean;
  quote: string | null;
}

export interface AppleStore {
  store_number: string;
  name: string;
  city: string;
  distance_miles: number | null;
  parts: AppleStorePart[];
}

export interface AppleDelivery {
  part_number: string;
  label: string;
  two_hour: boolean;
  quote: string | null;
}

export interface AppleResult {
  stores?: AppleStore[];
  delivery?: AppleDelivery[];
  display?: string;
  /** Stores found beyond max_distance_miles that were hidden. */
  stores_out_of_range?: number;
  /** Explanation shown when no stores are returned. */
  pickup_message?: string | null;
  zip?: string;
}

export interface GenericResult {
  signals?: string[];
  matched?: string | null;
}

export type LastResult = (AppleResult & GenericResult & Record<string, unknown>) | null;

export interface Item {
  id: number;
  name: string;
  url: string;
  kind: ItemKind;
  enabled: boolean;
  notify_enabled: boolean;
  interval_minutes: number;
  image_url: string | null;
  status: ItemStatus;
  status_text: string | null;
  price: string | null;
  last_checked_at: string | null;
  last_change_at: string | null;
  last_error: string | null;
  generic_config: GenericConfig | null;
  apple_config: AppleConfig | null;
  last_result: LastResult;
  /** Set when the item was marked purchased: it's then on the Purchased page and no longer checked. */
  purchased_at?: string | null;
  purchased_price?: string | null;
  retailer_config?: RetailerConfig | null;
  /** Price limit: no alert while the price is above it. */
  max_price?: number | null;
  last_in_stock_at?: string | null;
  /** Items sharing this are the same product at different stores. */
  product_group?: string | null;
  retailer?: Retailer | null;
  seller?: string | null;
  third_party?: boolean | null;
  cart_url?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ItemCreate {
  name?: string;
  url: string;
  kind?: ItemKind;
  interval_minutes?: number;
  notify_enabled?: boolean;
  image_url?: string;
  generic_config?: GenericConfig;
  apple_config?: AppleConfig;
  retailer_config?: RetailerConfig;
  max_price?: number | null;
  product_group?: string | null;
}

export type ItemUpdate = Partial<{
  name: string;
  url: string;
  kind: ItemKind;
  enabled: boolean;
  notify_enabled: boolean;
  interval_minutes: number;
  generic_config: GenericConfig;
  apple_config: AppleConfig;
  retailer_config: RetailerConfig | null;
  max_price: number | null;
  product_group: string | null;
}>;

export interface CheckEvent {
  id: number;
  item_id: number;
  checked_at: string;
  status: ItemStatus;
  status_text: string | null;
  error: string | null;
  duration_ms: number | null;
  changed: boolean;
}

export interface Preview {
  name: string | null;
  image_url: string | null;
  price: string | null;
  status: ItemStatus | null;
  is_apple: boolean;
  /** The store this URL belongs to, when supported. */
  retailer?: Retailer | null;
  /** Set when the page couldn't be fetched/parsed (response is still 200). */
  error?: string | null;
}

export interface AppleVariant {
  part_number: string;
  label: string;
  price: string | null;
}

export interface AppleResolve {
  product_name: string | null;
  image_url: string | null;
  variants: AppleVariant[];
  /** The model the pasted URL points to; pre-selected in the form. */
  selected_part_number?: string | null;
  error?: string | null;
}

export interface Notification {
  id: number;
  item_id: number | null;
  item_name: string | null;
  title: string;
  message: string;
  url: string | null;
  image_url: string | null;
  created_at: string;
  read: boolean;
  delivered: boolean;
  delivery_error: string | null;
}

export interface NotificationList {
  items: Notification[];
  unread_count: number;
  total: number;
}

export interface Stats {
  total: number;
  in_stock: number;
  out_of_stock: number;
  unknown: number;
  error: number;
  paused: number;
  unread_notifications: number;
  checks_24h: number;
  alerts_24h: number;
  purchased?: number;
}

export interface UpdateStatus {
  current_version: string;
  latest_version: string | null;
  latest_created: string | null;
  update_available: boolean;
  can_update: boolean;
  error: string | null;
}
