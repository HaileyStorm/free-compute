"use strict";

const app = document.querySelector("#app");
const STATUS_LABELS = {
  ready: "Ready",
  empty: "Empty",
  confirmed_free: "Confirmed free",
  unconfirmed_card_free: "Verify card-free",
  unverified_balance: "Verify balance",
  grant_application: "Application",
  blocked_auth: "Sign-in blocked",
  blocked_payment: "Payment blocked",
  open: "Open",
  action_required_by_provider: "Provider action",
  user_verification: "User verification",
  eligibility_input: "Eligibility input",
  credit_consuming: "Consumes credit",
  conditional_free: "Conditional free",
  payment_required_blocked: "Payment blocked",
  terms_unverified: "Verify terms",
  live: "Live meter",
  catalog: "Catalog fallback",
  disabled: "Monitoring off",
  error: "Meter error",
  never_polled: "Not polled"
};
const ORIGINS = new Set(["previously_had", "found_this_project", "unknown"]);
const RECURRENCES = new Set(["one_time", "daily", "weekly", "monthly", "annual", "dynamic", "unknown"]);
const CADENCE_FILTERS = ["one_time", "recurring", "daily", "weekly", "monthly", "annual", "dynamic", "unknown"];
const INTERRUPTIBILITY = new Set(["non_interruptible", "interruptible", "unknown"]);
const POOL_SUITABILITY = new Set(["good", "conditional", "poor", "none", "unknown"]);
const POOL_MECHANISMS = new Set(["provider_project", "organization_workspace", "shared_api_wrapper", "transferable_credit", "team_allocation", "isolated_personal", "unknown"]);
const COMPUTE_FAMILIES = ["cuda", "blackwell_cuda", "tpu", "rocm", "other_unknown"];
const COMPUTE_BACKENDS = new Set(["any", "cuda", "tpu", "rocm", "oneapi", "cpu"]);
const STORAGE_SAFETY = new Set(["confirmed_free", "conditional_free", "credit_consuming", "payment_required_blocked", "terms_unverified", "unknown"]);
const LIVE_POLL_MS = 30_000;
const API_BASE = (() => {
  if (!/^https?:$/.test(window.location.protocol) || !["127.0.0.1", "localhost", "::1", "[::1]"].includes(window.location.hostname)) return null;
  return window.location.origin;
})();
const state = {
  catalog: null,
  catalogSource: null,
  warnings: [],
  query: "",
  status: "all",
  origin: "all",
  recurrence: "all",
  interruptibility: "all",
  poolability: "all",
  computeClass: "all",
  computeFamily: "all",
  pickerSort: "usable",
  storageQuery: "",
  storageSafety: "all",
  storagePersistence: "all",
  storageLocality: "all",
  storagePoolability: "all",
  live: {
    supported: null,
    data: null,
    loading: false,
    lastSuccessAt: null,
    error: null,
    observing: false,
    observeFeedback: ""
  },
  onboarding: {
    supported: null,
    data: null,
    loading: false,
    submitting: false,
    error: null,
    feedback: "",
    credentialRefs: new Map()
  },
  arm: {
    supported: null,
    data: null,
    loading: false,
    submitting: false,
    error: null,
    feedback: ""
  },
  armDraft: {
    providers: new Set(),
    storageId: "",
    durationMinutes: "60",
    expiresAt: "",
    maxJobs: "1",
    maxH100e: "1",
    balanceFloor: "0",
    idleMinutes: "15",
    maxErrors: "3"
  },
  autoDraft: {
    kind: "python",
    computeBackend: "any",
    blackwellRequired: false,
    gpuCount: "1",
    minVram: "16",
    preferredVram: "24",
    interruptibility: "allowed",
    runtimeMinutes: "60",
    storageRequired: false,
    storageMinGib: "1",
    storagePersistence: "any",
    storageAccess: "",
    providerCount: "1"
  }
};

function enumValue(value, allowed) {
  return allowed.has(value) ? value : "unknown";
}

function label(value) {
  return String(value || "unknown")
    .replaceAll("_", " ")
    .replace(/\b\w/g, character => character.toUpperCase());
}

function originOf(item) {
  return enumValue(item.acquisition_origin, ORIGINS);
}

function recurrenceOf(item) {
  const aliases = {
    signup_once: "one_time",
    project_term: "one_time",
    program_term: "one_time",
    six_month_award: "one_time",
    rolling_30_days: "monthly",
    ongoing: "dynamic"
  };
  return enumValue(aliases[item.recurrence] || item.recurrence, RECURRENCES);
}

function interruptibilityOf(item) {
  return enumValue(item.interruptibility || item.usability?.interruptibility, INTERRUPTIBILITY);
}

function hardwareOf(item) {
  const hardware = item.hardware && typeof item.hardware === "object" ? item.hardware : {};
  return {
    gpuModels: Array.isArray(hardware.gpu_models) ? hardware.gpu_models.filter(Boolean) : [],
    vramMin: number(hardware.vram_gb_min ?? hardware.memory_per_unit_gb_min),
    vramMax: number(hardware.vram_gb_max ?? hardware.memory_per_unit_gb_max),
    bestGpu: hardware.best_gpu || null,
    computeClass: hardware.compute_class || "unknown"
  };
}

function computeProfileOf(item) {
  const hardware = item.hardware && typeof item.hardware === "object" ? item.hardware : {};
  const explicitCompute = item.compute && typeof item.compute === "object" ? item.compute : {};
  const hardwareCompute = hardware.compute && typeof hardware.compute === "object" ? hardware.compute : {};
  const values = [];
  const append = value => {
    if (Array.isArray(value)) value.forEach(append);
    else if (typeof value === "string" && value.trim()) values.push(value.trim().toLowerCase());
  };
  [
    item.compute_backend,
    item.compute_backends,
    item.compute_family,
    item.compute_families,
    explicitCompute.backend,
    explicitCompute.backends,
    explicitCompute.family,
    explicitCompute.families,
    hardware.compute_backend,
    hardware.compute_backends,
    hardware.accelerator_backend,
    hardware.accelerator_backends,
    hardware.compute_family,
    hardware.compute_families,
    hardware.accelerator_family,
    hardware.accelerator_families,
    hardwareCompute.backend,
    hardwareCompute.backends,
    hardwareCompute.family,
    hardwareCompute.families,
    hardware.stack,
    hardware.gpu_models,
    hardware.best_gpu,
    hardware.compute_class,
    item.kind
  ].forEach(append);
  const text = values.join(" ");
  const backends = new Set();
  if (/\btpu\b|tensor processing unit/.test(text)) backends.add("tpu");
  if (/\brocm\b|\bamd\b|\bmi(?:100|200|210|250|250x|300|300a|300x|325x)\b/.test(text)) backends.add("rocm");
  if (/\boneapi\b|\bsycl\b|intel (?:data center )?gpu|gpu max|ponte vecchio/.test(text)) backends.add("oneapi");
  if (/\bcpu\b|cpu_only|cpu-only/.test(text)) backends.add("cpu");
  const blackwell = /blackwell|blackwell_cuda|blackwell-cuda|\bb(?:100|200|300)\b|\bgb(?:200|300)\b|rtx pro 6000/.test(text)
    || item.blackwell === true
    || hardware.blackwell === true
    || explicitCompute.blackwell === true
    || hardwareCompute.blackwell === true;
  if (/\bcuda\b|\bnvidia\b|\b(?:h100|h200|a100|a40|l4|l40s|p100|t4|v100)\b/.test(text) || blackwell) backends.add("cuda");
  if (!backends.size) backends.add("unknown");
  return { backends, blackwell };
}

function computeFamilyKeysOf(item) {
  const profile = computeProfileOf(item);
  const families = [];
  if (profile.backends.has("cuda")) families.push("cuda");
  if (profile.blackwell) families.push("blackwell_cuda");
  if (profile.backends.has("tpu")) families.push("tpu");
  if (profile.backends.has("rocm")) families.push("rocm");
  if ([...profile.backends].some(value => ["oneapi", "cpu", "unknown"].includes(value))) families.push("other_unknown");
  return families.length ? families : ["other_unknown"];
}

function computeFamilyLabel(value) {
  const labels = {
    cuda: "CUDA",
    blackwell_cuda: "Blackwell-CUDA",
    tpu: "TPU",
    rocm: "ROCm",
    other_unknown: "Other / unknown"
  };
  return labels[value] || label(value);
}

function computeFamilyBadges(item) {
  return `<span class="family-badges">${computeFamilyKeysOf(item).map(value => `<span class="family-${esc(value)}">${esc(computeFamilyLabel(value))}</span>`).join("")}</span>`;
}

function computeFamilyText(item) {
  return computeFamilyKeysOf(item).map(computeFamilyLabel).join(" / ");
}

function isTpuOnly(item) {
  const backends = computeProfileOf(item).backends;
  return backends.has("tpu") && ![...backends].some(value => ["cuda", "rocm", "oneapi"].includes(value));
}

function usabilityOf(item) {
  const usability = item.usability && typeof item.usability === "object" ? item.usability : {};
  return {
    accessMode: usability.access_mode || "unknown",
    workloadTypes: Array.isArray(usability.workload_types) ? usability.workload_types.filter(Boolean) : [],
    usableNow: typeof usability.usable_now === "boolean" ? usability.usable_now : null,
    setupFriction: usability.setup_friction || "unknown"
  };
}

function poolabilityOf(item) {
  const poolability = item.poolability && typeof item.poolability === "object" ? item.poolability : {};
  return {
    suitability: enumValue(poolability.suitability, POOL_SUITABILITY),
    mechanism: enumValue(poolability.mechanism, POOL_MECHANISMS),
    oneTime: typeof poolability.one_time_contribution === "boolean" ? poolability.one_time_contribution : null,
    recurring: typeof poolability.recurring_contribution === "boolean" ? poolability.recurring_contribution : null,
    constraints: poolability.constraints || "Not recorded"
  };
}

function redactEmails(value) {
  return String(value ?? "").replace(
    /([A-Z0-9._%+-])[A-Z0-9._%+-]*(@[A-Z0-9.-]+\.[A-Z]{2,})/gi,
    "$1***$2"
  );
}

function esc(value) {
  return redactEmails(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? esc(url.href) : "#";
  } catch {
    return "#";
  }
}

function safeHref(value) {
  const raw = String(value || "").trim();
  if (!raw) return "#";
  if (/^https?:\/\//i.test(raw)) return safeUrl(raw);
  const normalized = raw.replaceAll("\\", "/").replace(/^\.\//, "");
  if (normalized.includes("..") || normalized.startsWith("//") || /^[a-z]+:/i.test(normalized)) return "#";
  return esc(encodeURI(`./${normalized.replace(/^\/+/, "")}`));
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function usd(value) {
  const parsed = number(value);
  return parsed === null
    ? "-"
    : parsed.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

function decimal(value, digits = 2) {
  const parsed = number(value);
  return parsed === null ? "-" : parsed.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function compactDateTime(value) {
  if (!value) return "Not reported";
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return String(value);
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function listOf(value) {
  if (Array.isArray(value)) return value.filter(entry => typeof entry === "string" && entry.trim());
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

function storageSafetyOf(item) {
  const raw = item.storage_safety || item.safety || item.status;
  const aliases = {
    ready: "confirmed_free",
    safe: "confirmed_free",
    zero_liability: "confirmed_free",
    conditional_zero_liability: "conditional_free",
    blocked_payment: "payment_required_blocked",
    payment_required: "payment_required_blocked",
    unverified: "terms_unverified"
  };
  return enumValue(aliases[raw] || raw, STORAGE_SAFETY);
}

function storagePersistenceOf(item) {
  const raw = typeof item.persistence === "object"
    ? item.persistence.mode || item.persistence.kind || item.persistence.class
    : item.persistence;
  return typeof raw === "string" && raw ? raw : "unknown";
}

function storageCapacityOf(item) {
  const capacity = item.capacity && typeof item.capacity === "object" ? item.capacity : {};
  const amount = number(capacity.amount ?? item.allowance_gib ?? item.capacity_gib);
  const bytes = number(capacity.normalized_bytes ?? capacity.bytes);
  const unit = String(capacity.unit || (item.allowance_gib != null || item.capacity_gib != null ? "GiB" : "")).trim();
  let gib = bytes === null ? null : bytes / (1024 ** 3);
  if (gib === null && amount !== null) {
    const normalized = unit.toLowerCase().replaceAll(" ", "");
    const factors = { b: 1 / (1024 ** 3), kb: 1000 / (1024 ** 3), mb: 1_000_000 / (1024 ** 3), gb: 1_000_000_000 / (1024 ** 3), tb: 1_000_000_000_000 / (1024 ** 3), kib: 1 / (1024 ** 2), mib: 1 / 1024, gib: 1, tib: 1024 };
    gib = Object.prototype.hasOwnProperty.call(factors, normalized) ? amount * factors[normalized] : null;
  }
  const display = amount !== null && unit
    ? `${decimal(amount)} ${unit}`
    : gib !== null ? `${decimal(gib)} GiB` : "Capacity unknown";
  return {
    amount,
    unit: unit || "unknown",
    gib,
    display,
    scope: capacity.scope || item.allowance_basis || "unknown"
  };
}

function storagePoolabilityOf(item) {
  const pool = item.poolability;
  if (typeof pool === "string") return pool;
  if (pool && typeof pool === "object") return pool.mechanism || pool.suitability || "unknown";
  return "unknown";
}

function storagePoolabilitySummary(item) {
  const pool = item.poolability;
  if (!pool || typeof pool !== "object") return label(storagePoolabilityOf(item));
  return `${label(pool.suitability || "unknown")} / ${label(pool.mechanism || "unknown")}`;
}

function storagePoolabilityValues(item) {
  const pool = item.poolability;
  if (typeof pool === "string") return [pool];
  if (pool && typeof pool === "object") return [pool.suitability, pool.mechanism].filter(value => typeof value === "string" && value);
  return ["unknown"];
}

function storageEgressOf(item) {
  const value = item.egress ?? item.egress_and_operations;
  if (typeof value === "string") return value;
  if (value && typeof value === "object") return value.policy || value.status || "unknown";
  return "unknown";
}

function storageMatches(item) {
  const localities = listOf(item.compute_locality);
  const poolability = storagePoolabilityValues(item);
  return (!state.storageQuery || haystack(item).includes(state.storageQuery))
    && (state.storageSafety === "all" || storageSafetyOf(item) === state.storageSafety)
    && (state.storagePersistence === "all" || storagePersistenceOf(item) === state.storagePersistence)
    && (state.storageLocality === "all" || localities.includes(state.storageLocality))
    && (state.storagePoolability === "all" || poolability.includes(state.storagePoolability));
}

function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || "unknown").replaceAll("_", " ");
}

function pill(status) {
  return `<span class="pill status-${esc(status || "unknown")}">${esc(statusLabel(status))}</span>`;
}

function sourceLinks(sources = []) {
  if (!sources.length) return '<span class="muted">No source recorded</span>';
  return sources.map((source, index) => {
    const label = source.label || `Source ${index + 1}`;
    const date = source.verified_on ? ` (${source.verified_on})` : "";
    return `<a href="${safeUrl(source.url)}" target="_blank" rel="noopener noreferrer">${esc(label)}${esc(date)}</a>`;
  }).join(" &middot; ");
}

function normalizedLinks(item) {
  const collected = [];
  const append = (value, local = false) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(entry => append(entry, local));
      return;
    }
    if (typeof value === "string") {
      collected.push({ href: value, label: local ? value.split(/[\\/]/).pop() : "Open", local });
      return;
    }
    if (typeof value === "object") {
      const href = value.url || value.href || value.path;
      if (href) {
        collected.push({ href, label: value.label || value.title || (local ? String(href).split(/[\\/]/).pop() : "Open"), local: local || Boolean(value.path) });
      } else {
        Object.entries(value).forEach(([linkLabel, linkValue]) => {
          if (typeof linkValue === "string") collected.push({ href: linkValue, label: linkLabel, local });
        });
      }
    }
  };
  append(item.links);
  append(item.local_docs, true);
  append(item.docs, true);
  return collected;
}

function itemLinks(item) {
  const links = normalizedLinks(item);
  if (!links.length) return '<span class="muted">No direct links</span>';
  return links.map(link => `<a href="${safeHref(link.href)}" ${link.local ? "" : 'target="_blank" rel="noopener noreferrer"'}>${esc(link.label)}</a>`).join(" &middot; ");
}

function accountUsd(account) {
  for (const candidate of [account.acquired_usd_value, account.acquired_usd, account.balance_usd, account.blocked_usd]) {
    const explicit = number(candidate);
    if (explicit !== null) return explicit;
  }
  return /usd/i.test(account.balance_unit || "") ? number(account.balance) : null;
}

function accountH100e(account, referenceRate) {
  if (isTpuOnly(account)) return null;
  const directKeys = ["acquired_h100e_hours", "h100e_hours", "normalized_h100e_hours"];
  const nested = [account.normalized_acquired, account.normalized].filter(value => value && typeof value === "object");
  let explicitPresent = false;
  const candidates = [];
  directKeys.forEach(key => {
    if (Object.prototype.hasOwnProperty.call(account, key)) {
      explicitPresent = true;
      candidates.push(account[key]);
    }
  });
  nested.forEach(value => {
    if (Object.prototype.hasOwnProperty.call(value, "h100e_hours")) {
      explicitPresent = true;
      candidates.push(value.h100e_hours);
    }
  });
  for (const candidate of candidates) {
    const parsed = number(candidate);
    if (parsed !== null) return parsed;
  }
  if (explicitPresent) return null;
  const value = accountUsd(account);
  return value !== null && referenceRate ? value / referenceRate : null;
}

function totals(catalog) {
  const referenceRate = number(catalog.normalization?.reference_usd_per_h100e_hour);
  return catalog.accounts.reduce((result, account) => {
    const value = accountUsd(account);
    if (value !== null) {
      result.knownUsd += value;
      result.knownBalanceAccounts += 1;
    }
    if (account.acquired_safe === true) {
      if (computeProfileOf(account).backends.has("tpu")) result.safeTpuAccounts += 1;
      if (value !== null) result.safeUsd += value;
      const h100e = accountH100e(account, referenceRate);
      // TPU availability stays native instead of becoming an "unconverted H100e" amount.
      if (h100e !== null) {
        result.safeH100e += h100e;
      } else if (!isTpuOnly(account)) result.safeUnconverted += 1;
      const usability = usabilityOf(account);
      if (usability.usableNow === true) {
        result.usableNow += 1;
        if (value !== null) result.usableUsd += value;
        if (h100e !== null) result.usableH100e += h100e;
      }
      else if (usability.usableNow === null) result.usabilityUnknown += 1;
      result.safeAccounts += 1;
    }
    if (account.status === "blocked_payment" && value !== null) {
      result.blockedUsd += value;
    }
    return result;
  }, {
    safeUsd: 0,
    safeH100e: 0,
    usableUsd: 0,
    usableH100e: 0,
    knownUsd: 0,
    blockedUsd: 0,
    safeAccounts: 0,
    knownBalanceAccounts: 0,
    safeUnconverted: 0,
    safeTpuAccounts: 0,
    usableNow: 0,
    usabilityUnknown: 0
  });
}

function haystack(item) {
  return JSON.stringify(item).toLowerCase();
}

function matches(item, { ignoreComputeFamily = false } = {}) {
  const statusMatch = state.status === "all" || item.status === state.status;
  const originMatch = state.origin === "all" || originOf(item) === state.origin;
  const recurrenceMatch = state.recurrence === "all"
    || state.recurrence === "recurring" && cadenceGroup(item) === "recurring"
    || recurrenceOf(item) === state.recurrence;
  const interruptibilityMatch = state.interruptibility === "all" || interruptibilityOf(item) === state.interruptibility;
  const poolabilityMatch = state.poolability === "all" || poolabilityOf(item).suitability === state.poolability;
  const computeClassMatch = state.computeClass === "all" || hardwareOf(item).computeClass === state.computeClass;
  const computeFamilyMatch = ignoreComputeFamily || state.computeFamily === "all" || computeFamilyKeysOf(item).includes(state.computeFamily);
  const queryMatch = !state.query || haystack(item).includes(state.query);
  return statusMatch && originMatch && recurrenceMatch && interruptibilityMatch && poolabilityMatch && computeClassMatch && computeFamilyMatch && queryMatch;
}

function isGpu(offer) {
  const hardware = hardwareOf(offer);
  return /(^|_)(gpu|tpu)($|_)/i.test(offer.kind || "")
    || /gpu|tpu/i.test(hardware.computeClass)
    || hardware.gpuModels.length > 0
    || Boolean(hardware.bestGpu);
}

function isDeferred(offer) {
  return offer.status === "grant_application" || offer.status === "blocked_payment";
}

function normalized(offer) {
  if (isTpuOnly(offer)) return '<span class="safe-unconverted">TPU native quota / not H100e</span>';
  const potential = offer.normalized_potential;
  if (!potential) {
    const safe = offer.status === "confirmed_free" && offer.payment_method === "not_required" && offer.hard_stop === true;
    return safe ? '<span class="safe-unconverted">Safe / unconverted</span>' : '<span class="muted">Unconverted</span>';
  }
  const parts = [];
  if (number(potential.usd_value) !== null) parts.push(`${usd(potential.usd_value)} value`);
  if (number(potential.h100e_hours) !== null) parts.push(`${decimal(potential.h100e_hours)} H100e-hours`);
  if (potential.period) parts.push(esc(potential.period.replaceAll("_", " ")));
  return parts.join(" / ") || '<span class="muted">Unconverted</span>';
}

function vramRange(hardware) {
  if (hardware.vramMin === null && hardware.vramMax === null) return "Unknown";
  if (hardware.vramMin !== null && hardware.vramMax !== null && hardware.vramMin !== hardware.vramMax) {
    return `${decimal(hardware.vramMin, 0)}-${decimal(hardware.vramMax, 0)} GB`;
  }
  const value = hardware.vramMin ?? hardware.vramMax;
  return `${decimal(value, 0)} GB`;
}

function hardwareSummary(item) {
  const hardware = hardwareOf(item);
  const models = hardware.gpuModels.length ? hardware.gpuModels.join(", ") : hardware.bestGpu || "Model unknown";
  return `${models}; ${vramRange(hardware)} VRAM; ${label(hardware.computeClass)}`;
}

function usabilitySummary(item) {
  const usability = usabilityOf(item);
  const usable = usability.usableNow === true ? "usable now" : usability.usableNow === false ? "not usable now" : "availability unknown";
  const workloads = usability.workloadTypes.length ? usability.workloadTypes.join(", ") : "workloads unknown";
  return `${usable}; ${label(usability.accessMode)}; ${workloads}; ${label(usability.setupFriction)} setup`;
}

function cadenceGroup(item) {
  const recurrence = recurrenceOf(item);
  if (recurrence === "one_time") return "single-use";
  if (["daily", "weekly", "monthly", "annual"].includes(recurrence)) return "recurring";
  return recurrence;
}

function poolBadge(item) {
  const pool = poolabilityOf(item);
  return `<span class="pool-badge pool-${esc(pool.suitability)}" title="${esc(`${label(pool.mechanism)}: ${pool.constraints}`)}">Pool fit: ${esc(label(pool.suitability))}</span>`;
}

function poolabilitySummary(item) {
  const pool = poolabilityOf(item);
  const flag = value => value === true ? "yes" : value === false ? "no" : "unknown";
  return `${label(pool.mechanism)}; one-time ${flag(pool.oneTime)}; recurring ${flag(pool.recurring)}; ${pool.constraints}`;
}

function offerCard(offer) {
  const hardStop = offer.hard_stop === true ? "hard stop" : offer.hard_stop === false ? "no hard stop" : "hard stop unverified";
  return `
    <article class="card offer-card">
      <div class="card-heading">
        <div><p class="eyebrow">${esc(offer.provider)}</p><h3>${esc(offer.title)}</h3></div>
        ${pill(offer.status)}
      </div>
      <div class="tag-row">
        <span>${esc(label(originOf(offer)))}</span>
        <span>${esc(label(cadenceGroup(offer)))}</span>
        <span>${esc(label(interruptibilityOf(offer)))}</span>
        ${poolBadge(offer)}
        ${computeFamilyBadges(offer)}
      </div>
      <p>${esc(offer.allowance)}</p>
      <dl class="facts">
        <div><dt>Catalog ceiling / conditional</dt><dd>${normalized(offer)}</dd></div>
        <div><dt>Safety</dt><dd>${esc(offer.payment_method || "unknown")} payment / ${hardStop}</dd></div>
        <div><dt>Cadence</dt><dd>${esc(label(recurrenceOf(offer)))}</dd></div>
        <div><dt>Hardware</dt><dd>${esc(hardwareSummary(offer))}</dd></div>
        <div><dt>Usability</dt><dd>${esc(usabilitySummary(offer))}</dd></div>
        <div><dt>Pooling</dt><dd>${esc(poolabilitySummary(offer))}</dd></div>
        <div><dt>Eligibility</dt><dd>${esc(offer.eligibility || "Not recorded")}</dd></div>
      </dl>
      <p class="next"><strong>Next:</strong> ${esc(offer.next_action || "No next action recorded.")}</p>
      <p class="links"><strong>Links:</strong> ${itemLinks(offer)}</p>
      <p class="sources">${sourceLinks(offer.sources)}</p>
    </article>`;
}

function emptyState() {
  return '<p class="empty">No matching entries.</p>';
}

function offerSection(id, title, note, offers) {
  const visible = offers.filter(matches);
  return `
    <section id="${id}" class="lane">
      <div class="section-heading"><div><p class="eyebrow">GPU / ML compute</p><h2>${esc(title)}</h2><p>${esc(note)}</p></div><span class="count">${visible.length}</span></div>
      <div class="cards">${visible.length ? visible.map(offerCard).join("") : emptyState()}</div>
    </section>`;
}

function accountTable(id, title, note, accounts, normalization) {
  const referenceRate = number(normalization?.reference_usd_per_h100e_hour);
  const visible = accounts.filter(matches);
  const rows = visible.map(account => {
    const value = accountUsd(account);
    const h100e = account.acquired_safe === true ? accountH100e(account, referenceRate) : null;
    const balanceIsUsd = /usd/i.test(account.balance_unit || "");
    const balanceValue = number(account.balance);
    const balanceText = balanceValue === null
      ? esc(account.balance ?? "-")
      : balanceIsUsd ? usd(balanceValue) : decimal(balanceValue);
    const normalizedValue = value !== null && !balanceIsUsd
      ? `<br><span class="muted">${usd(value)} H100e-normalized value</span>`
      : "";
    const safeH100e = account.acquired_safe !== true
      ? '<span class="muted">Not counted</span>'
      : isTpuOnly(account)
        ? '<span class="safe-unconverted">TPU native quota / not H100e</span>'
      : h100e === null
        ? '<span class="safe-unconverted">Acquired / unconverted</span>'
        : `${decimal(h100e)} H100e${computeProfileOf(account).backends.has("tpu") ? " (TPU excluded)" : ""}`;
    const usability = usabilityOf(account);
    return `
      <tr>
        <td><strong>${esc(account.provider)}</strong><br><span class="muted">${esc(account.id || "Catalog account")}</span><br>${pill(account.status)} ${poolBadge(account)}</td>
        <td>${esc(label(originOf(account)))}<br><span class="muted">${esc(label(recurrenceOf(account)))}</span><br><span class="muted">${esc(poolabilitySummary(account))}</span></td>
        <td>${balanceText}${account.balance_unit ? `<br><span class="muted">${esc(account.balance_unit)}</span>` : ""}${normalizedValue}</td>
        <td>${safeH100e}</td>
        <td>${esc(hardwareSummary(account))}<br>${computeFamilyBadges(account)}</td>
        <td><strong>${usability.usableNow === true ? "Usable now" : usability.usableNow === false ? "Not usable now" : "Unknown"}</strong><br><span class="muted">${esc(usabilitySummary(account))}</span></td>
        <td>${esc(account.payment_state || "unknown")}<br><span class="links">${itemLinks(account)}</span></td>
        <td>${esc(account.next_action || "-")}</td>
      </tr>`;
  }).join("");
  return `
    <section id="${esc(id)}" class="account-origin">
      <div class="section-heading"><div><p class="eyebrow">Account ledger</p><h2>${esc(title)}</h2><p>${esc(note)}</p></div><span class="count">${visible.length}</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Provider / account</th><th>Origin / cadence</th><th>Balance</th><th>Safe H100e</th><th>Hardware</th><th>Usability</th><th>Payment / links</th><th>Next action</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8">No matching accounts.</td></tr>'}</tbody>
      </table></div>
    </section>`;
}

function bestSingleHardware(items) {
  const ranked = items.map(item => ({ item, hardware: hardwareOf(item) })).sort((left, right) => {
    const rightVram = right.hardware.vramMax ?? right.hardware.vramMin ?? -1;
    const leftVram = left.hardware.vramMax ?? left.hardware.vramMin ?? -1;
    if (rightVram !== leftVram) return rightVram - leftVram;
    return Number(Boolean(right.hardware.bestGpu)) - Number(Boolean(left.hardware.bestGpu));
  });
  return ranked[0] || { item: null, hardware: hardwareOf({}) };
}

function pickerSort(rows, sortKey) {
  return [...rows].sort((left, right) => {
    if (sortKey === "compute") {
      const classOrder = label(left.computeClass).localeCompare(label(right.computeClass));
      if (classOrder) return classOrder;
    } else if (sortKey === "family") {
      const familyOrder = computeFamilyText(left.item).localeCompare(computeFamilyText(right.item));
      if (familyOrder) return familyOrder;
    } else {
      const key = sortKey === "vram" ? "maxVram" : sortKey === "total" ? "totalH100e" : "usableH100e";
      const difference = (right[key] ?? -1) - (left[key] ?? -1);
      if (difference) return difference;
    }
    const totalDifference = (right.totalH100e ?? -1) - (left.totalH100e ?? -1);
    if (totalDifference) return totalDifference;
    const priorityDifference = (right.priority ?? 0) - (left.priority ?? 0);
    if (priorityDifference) return priorityDifference;
    return left.name.localeCompare(right.name);
  });
}

function pickerHardware(hardware) {
  const gpu = hardware.bestGpu || hardware.gpuModels[0] || "Unspecified";
  const maxVram = hardware.vramMax ?? hardware.vramMin;
  return `<strong>${esc(gpu)}</strong><br><span class="muted">${maxVram === null ? "VRAM unknown" : `${decimal(maxVram, 0)} GB max per unit`} / ${esc(label(hardware.computeClass))}</span>`;
}

function accountPickerRows(catalog) {
  const referenceRate = number(catalog.normalization?.reference_usd_per_h100e_hour);
  return catalog.accounts.filter(account => account.acquired_safe === true && usabilityOf(account).usableNow === true && matches(account)).map(account => {
    const linkedOffers = catalog.offers.filter(offer => offer.account_id === account.id);
    const hardwareChoice = bestSingleHardware([account, ...linkedOffers]);
    const total = account.acquired_safe === true ? accountH100e(account, referenceRate) : null;
    const usable = account.acquired_safe === true && usabilityOf(account).usableNow === true ? total : null;
    const tpuOnly = isTpuOnly(account);
    const mixedTpu = computeProfileOf(account).backends.has("tpu") && !tpuOnly;
    const usableDisplay = account.acquired_safe === true && usabilityOf(account).usableNow === true
      ? tpuOnly ? "TPU native quota / not H100e" : total === null ? "Usable / unconverted" : `${decimal(total)} H100e${mixedTpu ? " (TPU excluded)" : ""}`
      : "0 H100e";
    const accountStability = interruptibilityOf(account);
    return {
      item: account,
      name: `${account.provider || "Unknown"} ${account.id || ""}`,
      totalH100e: total ?? 0,
      totalDisplay: account.acquired_safe === true ? tpuOnly ? "TPU native quota / not H100e" : total === null ? "Safe / unconverted" : `${decimal(total)} H100e${mixedTpu ? " (TPU excluded)" : ""}` : "0 H100e (not safe-acquired)",
      usableH100e: usable ?? 0,
      usableDisplay,
      hardware: hardwareChoice.hardware,
      maxVram: hardwareChoice.hardware.vramMax ?? hardwareChoice.hardware.vramMin ?? 0,
      computeClass: hardwareChoice.hardware.computeClass,
      priority: account.acquired_safe === true ? 3 : ["empty", "expired"].includes(account.status) ? 0 : 1,
      stability: accountStability === "unknown" && hardwareChoice.item ? interruptibilityOf(hardwareChoice.item) : accountStability
    };
  });
}

function offerPickerRows(catalog) {
  return catalog.offers.filter(matches).map(offer => {
    const hardware = hardwareOf(offer);
    const tpuOnly = isTpuOnly(offer);
    const mixedTpu = computeProfileOf(offer).backends.has("tpu") && !tpuOnly;
    const potential = tpuOnly ? null : number(offer.normalized_potential?.h100e_hours);
    const usablePotential = usabilityOf(offer).usableNow === true ? potential : null;
    return {
      item: offer,
      name: `${offer.provider || "Unknown"} ${offer.title || ""}`,
      totalH100e: potential ?? 0,
      totalDisplay: tpuOnly ? "TPU native quota / not H100e" : potential === null ? "Unconverted catalog ceiling" : `${decimal(potential)} H100e catalog ceiling${mixedTpu ? " (TPU excluded)" : ""}`,
      usableH100e: usablePotential ?? 0,
      usableDisplay: tpuOnly
        ? usabilityOf(offer).usableNow === true ? "TPU usable / native quota" : "TPU conditional / native quota"
        : usablePotential === null ? "Conditional; not found usable" : `${decimal(usablePotential)} H100e conditional ceiling${mixedTpu ? " (TPU excluded)" : ""}`,
      hardware,
      maxVram: hardware.vramMax ?? hardware.vramMin ?? 0,
      computeClass: hardware.computeClass,
      priority: potential !== null ? 2 : offer.status === "confirmed_free" ? 1 : 0,
      stability: interruptibilityOf(offer)
    };
  });
}

function pickerSection(catalog) {
  const accounts = pickerSort(accountPickerRows(catalog), state.pickerSort);
  const accountRows = accounts.map((row, index) => {
    const account = row.item;
    return `<tr>
      <td class="rank">${index + 1}</td>
      <td><strong>${esc(account.provider)}</strong><br><span class="muted">${esc(account.id || "Catalog account")}</span><br>${pill(account.status)}</td>
      <td><strong>${row.usableDisplay}</strong><br><span class="muted">${row.totalDisplay} total</span></td>
      <td>${pickerHardware(row.hardware)}</td>
      <td>${computeFamilyBadges(account)}</td>
      <td>${esc(label(row.stability))}<br><span class="muted">${esc(label(recurrenceOf(account)))}</span></td>
      <td>${poolBadge(account)}</td>
      <td>${esc(account.next_action || "No action recorded.")}<br><span class="links">${itemLinks(account)}</span></td>
    </tr>`;
  }).join("");
  return `<section id="provider-picker" class="picker-section">
    <div class="picker-heading"><div><p class="eyebrow">Choose compute you already have</p><h2>Safe, usable provider picker</h2><p>Only safely acquired accounts marked usable now appear here. Each row is one account; linked configurations identify its best hardware option without stacking overlapping quota.</p></div>
      <label>Rank by <select id="picker-sort"><option value="usable" ${state.pickerSort === "usable" ? "selected" : ""}>Immediately usable H100e</option><option value="total" ${state.pickerSort === "total" ? "selected" : ""}>Total H100e / catalog ceiling</option><option value="vram" ${state.pickerSort === "vram" ? "selected" : ""}>Max per-unit VRAM</option><option value="family" ${state.pickerSort === "family" ? "selected" : ""}>Compute family</option><option value="compute" ${state.pickerSort === "compute" ? "selected" : ""}>Compute class</option></select></label>
    </div>
    <div class="picker-view acquired-picker"><div class="section-heading"><div><p class="eyebrow">Usable now</p><h3>Best existing accounts</h3><p>Safe H100e is account-scoped only. TPU capacity remains native and separate.</p></div><span class="count">${accounts.length}</span></div>
      <div class="picker-table"><table><thead><tr><th>#</th><th>Single account</th><th>Usable / total</th><th>Best single hardware</th><th>Compute family</th><th>Stability / cadence</th><th>Pool fit</th><th>Action</th></tr></thead><tbody>${accountRows || '<tr><td colspan="8">No safe, immediately usable account matches these inventory filters.</td></tr>'}</tbody></table></div>
    </div>
  </section>`;
}

function acquisitionSection(catalog) {
  const offers = pickerSort(offerPickerRows(catalog), state.pickerSort);
  const rows = offers.map((row, index) => {
    const offer = row.item;
    const safety = `${offer.payment_method || "unknown"} payment / ${offer.hard_stop === true ? "hard stop" : offer.hard_stop === false ? "no hard stop" : "hard stop unknown"}`;
    return `<tr><td class="rank">${index + 1}</td><td><strong>${esc(offer.provider)}</strong><br><span class="muted">${esc(offer.title || "Single offer")}</span><br>${pill(offer.status)}</td><td><strong>${row.totalDisplay}</strong><br><span class="muted">${row.usableDisplay}</span></td><td>${pickerHardware(row.hardware)}</td><td>${computeFamilyBadges(offer)}</td><td>${esc(label(row.stability))}<br><span class="muted">${esc(label(recurrenceOf(offer)))}</span></td><td>${poolBadge(offer)}<br><span class="muted">${esc(safety)}</span></td><td>${esc(offer.eligibility || "Eligibility not recorded")}<br><strong>Next:</strong> ${esc(offer.next_action || "No action recorded.")}<br><span class="links">${itemLinks(offer)} &middot; ${sourceLinks(offer.sources)}</span></td></tr>`;
  }).join("");
  return `<section id="next-acquisition" class="picker-section acquisition-section"><div class="section-heading"><div><p class="eyebrow">Next acquisition</p><h2>Conditional opportunities</h2><p>Independent, non-additive catalog ceilings—not acquired balances or proof of immediate usability. Complete eligibility and zero-liability checks before acting.</p></div><span class="count">${offers.length}</span></div><div class="picker-table"><table><thead><tr><th>#</th><th>Single offer</th><th>Conditional ceiling</th><th>Hardware</th><th>Compute family</th><th>Stability / cadence</th><th>Status / pool</th><th>Eligibility / action</th></tr></thead><tbody>${rows || '<tr><td colspan="8">No conditional offer matches these inventory filters.</td></tr>'}</tbody></table></div></section>`;
}

function storageIsZeroLiability(item) {
  const payment = String(item.payment_method || item.payment_method_required || "unknown").toLowerCase();
  return storageSafetyOf(item) === "confirmed_free"
    && item.paid_fallback_allowed !== true
    && item.hard_stop !== false
    && !["required", "yes", "card_required", "payment_required"].includes(payment);
}

function storageSection(catalog) {
  const storage = Array.isArray(catalog.storage) ? catalog.storage.filter(item => item && typeof item === "object") : [];
  if (!storage.length) {
    return `<section id="storage" class="storage-section"><div class="section-heading"><div><p class="eyebrow">Persistent storage</p><h2>Storage catalog</h2><p>No storage entries are published yet. Compute totals remain unchanged.</p></div><span class="count">0</span></div></section>`;
  }
  const visible = storage.filter(storageMatches);
  const safe = storage.filter(storageIsZeroLiability);
  const safeCapacities = safe.filter(item => storageCapacityOf(item).gib !== null);
  const usableSafe = safe.filter(item => item.usable_now === true);
  const conditional = storage.filter(item => !storageIsZeroLiability(item));
  const localities = [...new Set(storage.flatMap(item => listOf(item.compute_locality)))].sort();
  const poolabilities = [...new Set(storage.flatMap(storagePoolabilityValues))].sort();
  const safetyValues = [...new Set(storage.map(storageSafetyOf))].sort();
  const persistenceValues = [...new Set(storage.map(storagePersistenceOf))].sort();
  const option = (value, current) => `<option value="${esc(value)}" ${current === value ? "selected" : ""}>${esc(label(value))}</option>`;
  const rows = visible.map(item => {
    const capacity = storageCapacityOf(item);
    const access = listOf(item.access);
    const locality = listOf(item.compute_locality);
    const persistence = storagePersistenceOf(item);
    const payment = item.payment_method || item.payment_method_required || "unknown";
    const hardStop = item.hard_stop === true ? "hard stop" : item.hard_stop === false ? "no hard stop" : "hard stop unknown";
    const scope = capacity.scope === "unknown" ? "Scope not recorded" : label(capacity.scope);
    const retention = item.retention_notes || (typeof item.persistence === "object" ? item.persistence.notes : "") || "No retention note";
    const linkText = itemLinks(item);
    return `<tr>
      <td><strong>${esc(item.provider || "Unknown provider")}</strong><br><span class="muted">${esc(item.service || item.title || item.id)}</span><br>${pill(storageSafetyOf(item))}</td>
      <td><strong>${esc(capacity.display)}</strong><br><span class="muted">${esc(scope)}</span></td>
      <td><strong>${esc(label(persistence))}</strong><br><span class="muted">${esc(retention)}</span></td>
      <td>${access.length ? access.map(value => `<span class="mini-tag">${esc(label(value))}</span>`).join(" ") : '<span class="muted">Unknown</span>'}</td>
      <td>${locality.length ? locality.map(value => `<span class="mini-tag">${esc(label(value))}</span>`).join(" ") : '<span class="muted">External / unknown</span>'}</td>
      <td>${esc(storagePoolabilitySummary(item))}<br><span class="muted">${esc(label(item.storage_class || "unknown"))}</span></td>
      <td>${esc(label(payment))} / ${hardStop}<br><span class="muted">Egress: ${esc(label(storageEgressOf(item)))}</span></td>
      <td><span class="links">${linkText}</span><br><span class="sources">${sourceLinks(item.sources)}</span></td>
    </tr>`;
  }).join("");

  return `<section id="storage" class="storage-section">
    <div class="section-heading"><div><p class="eyebrow">Persistent storage / separate ledger</p><h2>Storage catalog</h2><p>Capacity, persistence, access, locality, safety, and poolability are tracked independently. Storage is never converted to or added to H100e.</p></div><span class="count">${visible.length}</span></div>
    <div class="storage-metrics" aria-label="Storage summary">
      <article><span>Confirmed-free entries</span><strong>${safe.length}</strong><small>${safeCapacities.length} have a convertible individual ceiling.</small></article>
      <article><span>Marked usable now</span><strong>${usableSafe.length}</strong><small>Confirmed-free entries only; live quota may differ.</small></article>
      <article><span>Conditional or blocked</span><strong>${conditional.length}</strong><small>Never selected by zero-liability Arm.</small></article>
    </div>
    <div class="storage-capacity-grid" aria-label="Individual confirmed-free storage ceilings">${safeCapacities.map(item => {
      const capacity = storageCapacityOf(item);
      return `<article><span>${esc(item.provider || item.id)}</span><strong>${esc(capacity.display)}</strong><small>${esc(capacity.scope)}</small></article>`;
    }).join("")}</div>
    <div class="storage-toolbar" role="search" aria-label="Filter storage catalog">
      <label>Search storage <input id="storage-search" type="search" value="${esc(state.storageQuery)}" placeholder="provider, S3, archive..." autocomplete="off"></label>
      <label>Safety <select id="storage-safety"><option value="all">All safety</option>${safetyValues.map(value => option(value, state.storageSafety)).join("")}</select></label>
      <label>Persistence <select id="storage-persistence"><option value="all">All persistence</option>${persistenceValues.map(value => option(value, state.storagePersistence)).join("")}</select></label>
      <label>Locality <select id="storage-locality"><option value="all">All locality</option>${localities.map(value => option(value, state.storageLocality)).join("")}</select></label>
      <label>Poolability <select id="storage-poolability"><option value="all">All poolability</option>${poolabilities.map(value => option(value, state.storagePoolability)).join("")}</select></label>
      <button id="clear-storage-filters" type="button">Clear storage filters</button>
    </div>
    <div class="table-wrap storage-table"><table>
      <thead><tr><th scope="col">Provider / service</th><th scope="col">Capacity</th><th scope="col">Persistence</th><th scope="col">Access</th><th scope="col">Compute locality</th><th scope="col">Poolability / class</th><th scope="col">Safety / egress</th><th scope="col">Links / evidence</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8">No matching storage entries.</td></tr>'}</tbody>
    </table></div>
    <p class="muted">Individual ceilings are intentionally not summed: scopes can overlap or represent per-owner, per-project, per-record, or alternative public/private limits.</p>
  </section>`;
}

function simpleOfferSection(id, eyebrow, title, offers) {
  const visible = offers.filter(matches);
  return `
    <section id="${id}">
      <div class="section-heading"><div><p class="eyebrow">${esc(eyebrow)}</p><h2>${esc(title)}</h2></div><span class="count">${visible.length}</span></div>
      <div class="cards">${visible.length ? visible.map(offerCard).join("") : emptyState()}</div>
    </section>`;
}

function blockerSection(blockers = []) {
  const visible = blockers.filter(matches);
  return `
    <section id="blockers" class="blocker-section">
      <div class="section-heading"><div><p class="eyebrow">Human or policy gates</p><h2>Blockers</h2></div><span class="count">${visible.length}</span></div>
      <div class="cards">${visible.length ? visible.map(blocker => `
        <article class="card blocker-card">
          <div class="card-heading"><h3>${esc(blocker.title || blocker.provider || blocker.id)}</h3>${pill(blocker.status || "blocked")}</div>
          <p>${esc(blocker.summary || blocker.reason || blocker.description || blocker.blocker || "No detail recorded.")}</p>
          <p><strong>Needed:</strong> ${esc(blocker.needed || blocker.user_action || "Review manually.")}</p>
          <p class="next"><strong>Next:</strong> ${esc(blocker.next_action || "Review manually.")}</p>
          ${blocker.sources ? `<p class="sources">${sourceLinks(blocker.sources)}</p>` : ""}
        </article>`).join("") : '<p class="empty">No blockers recorded.</p>'}</div>
    </section>`;
}

function normalizationSection(normalization) {
  return `
    <details class="method">
      <summary>How H100-hour-equivalent is calculated</summary>
      <p><strong>${esc(normalization.unit)}</strong> uses a ${usd(normalization.reference_usd_per_h100e_hour)} reference for ${esc(normalization.reference_gpu)} as of ${esc(normalization.reference_date)}.</p>
      <p>${esc(normalization.reference_basis)}</p>
      <p><strong>TPU exclusion:</strong> TPU time remains in its native quota and is never converted to or added to H100e.</p>
      <ul>${(normalization.rules || []).map(rule => `<li>${esc(rule)}</li>`).join("")}</ul>
      <p class="sources">${sourceLinks(normalization.sources)}</p>
    </details>`;
}

function countBy(items, getter) {
  return items.reduce((counts, item) => {
    const key = getter(item) || "unknown";
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {});
}

function countList(counts, preferred = []) {
  const keys = [...new Set([...preferred, ...Object.keys(counts).sort()])];
  return keys.filter(key => preferred.includes(key) || counts[key]).map(key => `<li><span>${esc(label(key))}</span><strong>${counts[key] || 0}</strong></li>`).join("") || "<li><span>None</span><strong>0</strong></li>";
}

function poolTotals(accounts, referenceRate) {
  const result = {
    poolFit: { accounts: 0, usd: 0, h100e: 0 },
    isolated: { accounts: 0, usd: 0, h100e: 0 },
    unknown: { accounts: 0, usd: 0, h100e: 0 }
  };
  accounts.forEach(account => {
    const pool = poolabilityOf(account);
    const bucket = pool.mechanism === "isolated_personal" || ["poor", "none"].includes(pool.suitability)
      ? "isolated"
      : ["good", "conditional"].includes(pool.suitability) ? "poolFit" : "unknown";
    result[bucket].accounts += 1;
    result[bucket].usd += accountUsd(account) || 0;
    result[bucket].h100e += accountH100e(account, referenceRate) || 0;
  });
  return result;
}

function familyAggregateCards(catalog) {
  const accounts = catalog.accounts.filter(item => matches(item, { ignoreComputeFamily: true }));
  const offers = catalog.offers.filter(item => matches(item, { ignoreComputeFamily: true }));
  const notes = {
    cuda: "Includes Blackwell-CUDA as a CUDA subtype.",
    blackwell_cuda: "Explicit Blackwell-capable CUDA subset.",
    tpu: "Native TPU quota only; never converted to H100e.",
    rocm: "AMD/ROCm paths remain backend-specific.",
    other_unknown: "oneAPI, CPU, other, or backend unknown."
  };
  return `<div class="family-grid" aria-label="Compute family availability">${COMPUTE_FAMILIES.map(family => {
    const familyAccounts = accounts.filter(item => computeFamilyKeysOf(item).includes(family));
    const familyOffers = offers.filter(item => computeFamilyKeysOf(item).includes(family));
    const safeAccounts = familyAccounts.filter(item => item.acquired_safe === true);
    const safeUsable = safeAccounts.filter(item => usabilityOf(item).usableNow === true).length;
    const familyH100e = ["cuda", "blackwell_cuda"].includes(family)
      ? safeAccounts.reduce((sum, item) => sum + (accountH100e(item, number(catalog.normalization?.reference_usd_per_h100e_hour)) || 0), 0)
      : null;
    const normalization = family === "tpu"
      ? "Native TPU quota; H100e not applicable."
      : family === "rocm" ? "ROCm stays unconverted unless an exact factor exists."
        : familyH100e !== null ? `${decimal(familyH100e)} acquired H100e; TPU components excluded.` : "No family H100e attribution.";
    const selected = state.computeFamily === family ? "is-selected" : "";
    return `<article class="aggregate-card family-card family-card-${esc(family)} ${selected}">
      <h3>${esc(computeFamilyLabel(family))}</h3>
      <strong>${familyOffers.length} offers / ${safeUsable} usable</strong>
      <p>${familyAccounts.length} filtered account records. ${esc(normalization)} ${esc(notes[family])}</p>
    </article>`;
  }).join("")}</div>`;
}

function aggregatePanels(catalog, offers, summary) {
  const acquired = catalog.accounts.filter(account => account.acquired_safe === true);
  const pools = poolTotals(acquired, number(catalog.normalization?.reference_usd_per_h100e_hour));
  const originCounts = countBy(acquired, originOf);
  const stabilityCounts = countBy(offers, interruptibilityOf);
  const recurrenceCounts = countBy(offers, recurrenceOf);
  const classCounts = countBy(offers, offer => hardwareOf(offer).computeClass);
  const vramMins = offers.map(offer => hardwareOf(offer).vramMin).filter(value => value !== null);
  const vramMaxes = offers.map(offer => hardwareOf(offer).vramMax).filter(value => value !== null);
  const bestGpus = [...new Set(offers.map(offer => hardwareOf(offer).bestGpu).filter(Boolean))];
  const minVram = vramMins.length ? Math.min(...vramMins) : null;
  const maxVram = vramMaxes.length ? Math.max(...vramMaxes) : null;
  const vramEnvelope = minVram !== null && maxVram !== null
    ? `${decimal(minVram, 0)}-${decimal(maxVram, 0)} GB`
    : minVram !== null ? `${decimal(minVram, 0)}+ GB` : maxVram !== null ? `Up to ${decimal(maxVram, 0)} GB` : "Unspecified";
  const recurringCount = offers.filter(offer => ["daily", "weekly", "monthly", "annual"].includes(recurrenceOf(offer))).length;
  const singleUseCount = offers.filter(offer => recurrenceOf(offer) === "one_time").length;

  return `
    <section class="aggregates" aria-label="Filtered portfolio aggregates">
      <div class="section-heading"><div><p class="eyebrow">Filtered portfolio</p><h2>Availability and shape</h2><p>Offer panels update with the filters; acquired totals always remain zero-liability only.</p></div><span class="count">${offers.length}</span></div>
      <div class="aggregate-grid">
        <article class="aggregate-card acquired"><h3>Safely acquired availability</h3><strong>${usd(summary.safeUsd)} / ${decimal(summary.safeH100e)} H100e</strong><p>${summary.safeAccounts} accounts; ${summary.safeUnconverted} unconverted and ${summary.safeTpuAccounts} TPU-capable tracked separately.</p><ul>${countList(originCounts, ["previously_had", "found_this_project", "unknown"])}</ul></article>
        <article class="aggregate-card"><h3>Zero-liability usable now</h3><strong>${usd(summary.usableUsd)} / ${decimal(summary.usableH100e)} H100e</strong><p>${summary.usableNow} of ${summary.safeAccounts} safely acquired accounts are marked usable now. ${summary.usabilityUnknown} are unknown.</p></article>
        <article class="aggregate-card stability"><h3>Stability</h3><ul>${countList(stabilityCounts, ["non_interruptible", "interruptible", "unknown"])}</ul></article>
        <article class="aggregate-card"><h3>Cadence</h3><p><strong>${singleUseCount}</strong> single-use / <strong>${recurringCount}</strong> recurring</p><ul>${countList(recurrenceCounts, ["one_time", "daily", "weekly", "monthly", "annual", "dynamic", "unknown"])}</ul></article>
        <article class="aggregate-card"><h3>Compute class</h3><ul>${countList(classCounts)}</ul></article>
        <article class="aggregate-card"><h3>VRAM envelope</h3><strong>${vramEnvelope}</strong><p>${bestGpus.length ? `Best listed: ${esc(bestGpus.slice(0, 5).join(", "))}` : "No best-GPU field in the filtered set."}</p></article>
        <article class="aggregate-card"><h3>Safe acquired pool fit</h3><ul><li><span>Good / conditional</span><strong>${usd(pools.poolFit.usd)} / ${decimal(pools.poolFit.h100e)} H100e</strong></li><li><span>Isolated</span><strong>${usd(pools.isolated.usd)} / ${decimal(pools.isolated.h100e)} H100e</strong></li><li><span>Unknown</span><strong>${pools.unknown.accounts} accounts</strong></li></ul><p>Based only on explicit provider-supported mechanisms; credential sharing is never inferred.</p></article>
      </div>
      <div class="family-heading"><p class="eyebrow">Compute families</p><h3>Backend-specific availability</h3><p>Counts can overlap when one route supports multiple backends. Blackwell-CUDA is both an explicit family and a CUDA subtype; TPU stays in native units.</p></div>
      ${familyAggregateCards(catalog)}
    </section>`;
}

function historyValue(snapshot, key) {
  const aliases = {
    used_h100e: ["used_h100e", "used_h100e_since_tracking"],
    accounts_acquired: ["accounts_acquired", "accounts_acquired_this_project", "safe_accounts"]
  };
  for (const candidate of aliases[key] || [key]) {
    if (snapshot[candidate] != null) return snapshot[candidate];
  }
  return null;
}

function historySeries(history, key, width, height, padding, maxValue) {
  const points = history.map((snapshot, index) => {
    const value = number(historyValue(snapshot, key));
    if (value === null) return null;
    const x = history.length === 1 ? width / 2 : padding + index * ((width - padding * 2) / (history.length - 1));
    const y = height - padding - (value / maxValue) * (height - padding * 2);
    return { x, y, value };
  }).filter(Boolean);
  return {
    line: points.length > 1 ? points.map(point => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ") : "",
    dots: points.map(point => `<circle cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="4"><title>${decimal(point.value)} H100e</title></circle>`).join("")
  };
}

function toolEffectiveness(history, tracking) {
  const names = ["browser", "mail", "web", "automation", "manual"];
  const totals = {};
  const seen = new Set();
  const absorb = (name, stats) => {
    if (!stats || typeof stats !== "object") return;
    seen.add(name);
    if (!names.includes(name)) names.push(name);
    totals[name] ||= { checks: 0, offers: 0, accounts: 0, h100e: 0 };
    totals[name].checks += number(stats.checks ?? stats.verified_accounts ?? stats.evidence_records ?? stats.runs ?? stats.user_signins) || 0;
    totals[name].offers += number(stats.offers_added ?? stats.offers_cataloged ?? stats.material_updates) || 0;
    totals[name].accounts += number(stats.accounts_acquired ?? stats.acquired_accounts) || 0;
    totals[name].h100e += number(stats.h100e_acquired) || 0;
  };
  history.forEach(snapshot => {
    const tools = snapshot.tools && typeof snapshot.tools === "object" ? snapshot.tools : {};
    Object.entries(tools).forEach(([name, stats]) => absorb(name, stats));
  });
  const trackedTools = tracking?.tool_effectiveness && typeof tracking.tool_effectiveness === "object" ? tracking.tool_effectiveness : {};
  Object.entries(trackedTools).forEach(([name, stats]) => absorb(name, stats));
  return names.filter(name => seen.has(name)).map(name => ({ name, ...totals[name] }));
}

function historySection(rawHistory, tracking) {
  if (!Array.isArray(rawHistory) || !rawHistory.length) return "";
  const history = rawHistory.filter(snapshot => snapshot && typeof snapshot === "object" && snapshot.observed_on).sort((a, b) => String(a.observed_on).localeCompare(String(b.observed_on)));
  if (!history.length) return "";
  const latest = history.at(-1);
  const tools = toolEffectiveness(history, tracking);
  const provenance = historyProvenance(history);
  if (history.length === 1) {
    return `
      <section id="history" class="history-section">
        <div class="section-heading"><div><p class="eyebrow">History / observability</p><h2>Baseline recorded</h2><p>One observation exists. A trend will appear after the next daily sweep.</p></div><span class="count">1</span></div>
        <div class="baseline-grid">
          <article><span>Observed</span><strong>${esc(latest.observed_on)}</strong></article>
          <article><span>Acquired / available</span><strong>${decimal(latest.acquired_h100e_available)} H100e</strong></article>
          <article><span>Conditional catalog ceiling</span><strong>${decimal(latest.discovered_h100e_potential)} H100e</strong></article>
          <article><span>Actual usage</span><strong>${decimal(historyValue(latest, "used_h100e"))} H100e</strong></article>
          <article><span>Acquired value</span><strong>${usd(latest.acquired_usd_value)}</strong></article>
        </div>
        ${provenance}
        ${toolStats(tools)}
      </section>`;
  }

  const width = 800;
  const height = 260;
  const padding = 32;
  const values = history.flatMap(snapshot => [historyValue(snapshot, "acquired_h100e_available"), historyValue(snapshot, "discovered_h100e_potential"), historyValue(snapshot, "used_h100e")]).map(number).filter(value => value !== null);
  const maxValue = Math.max(1, ...values);
  const acquired = historySeries(history, "acquired_h100e_available", width, height, padding, maxValue);
  const potential = historySeries(history, "discovered_h100e_potential", width, height, padding, maxValue);
  const used = historySeries(history, "used_h100e", width, height, padding, maxValue);
  const line = (series, className) => `${series.line ? `<polyline class="history-line ${className}" points="${series.line}"></polyline>` : ""}<g class="history-dots ${className}">${series.dots}</g>`;

  return `
    <section id="history" class="history-section">
      <div class="section-heading"><div><p class="eyebrow">History / observability</p><h2>Compute over time</h2><p>Acquired availability, conditional catalog ceiling, and actual usage. Values remain H100e-normalized only where conversion is supported; TPU stays separate.</p></div><span class="count">${history.length}</span></div>
      <p class="history-chart-note"><strong>Corrections are records, not usage.</strong> A same-day baseline-to-correction change updates provenance; only the Actual usage series represents consumption.</p>
      <div class="chart-wrap">
        <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="H100-equivalent compute history chart">
          <line class="axis" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
          <line class="axis" x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}"></line>
          <text x="4" y="${padding + 5}">${esc(decimal(maxValue))}</text><text x="15" y="${height - padding + 5}">0</text>
          ${line(acquired, "acquired-series")}${line(potential, "potential-series")}${line(used, "used-series")}
        </svg>
        <div class="chart-labels"><span>${esc(history[0].observed_on)}</span><span>${esc(latest.observed_on)}</span></div>
        <div class="chart-legend"><span class="acquired-series">Acquired / available</span><span class="potential-series">Conditional catalog ceiling</span><span class="used-series">Actual usage</span></div>
      </div>
      <p class="history-latest">Latest: ${usd(latest.acquired_usd_value)} acquired value; ${decimal(latest.acquired_h100e_available)} H100e available; ${decimal(historyValue(latest, "used_h100e"))} H100e used; ${esc(latest.cataloged_offers ?? "-")} offers; ${esc(historyValue(latest, "accounts_acquired") ?? "-")} acquired accounts.</p>
      ${provenance}
      ${toolStats(tools)}
    </section>`;
}

function historyProvenance(history) {
  const latest = history.at(-1);
  const latestCeiling = number(historyValue(latest, "discovered_h100e_potential"));
  const ceilingNotice = latestCeiling === null ? "" : `
    <aside class="history-ceiling-note" role="note">
      <strong>${decimal(latestCeiling)} H100e is a conditional catalog ceiling, not available capacity.</strong>
      <span>It is non-stackable, eligibility-gated, and not acquired. Do not add it across offers or to account balances.</span>
    </aside>`;
  const records = history.map((snapshot, index) => {
    const correctedBy = history.slice(index + 1).find(candidate =>
      candidate.observed_on === snapshot.observed_on && /correction/i.test(candidate.event || "")
    );
    const isCorrection = /correction/i.test(snapshot.event || "");
    const stateClass = correctedBy ? "is-superseded" : isCorrection ? "is-correction" : "";
    const status = correctedBy ? "Superseded / corrected" : isCorrection ? "Corrected record" : "Recorded";
    const explanation = correctedBy
      ? `Superseded by the next same-day ${label(correctedBy.event)} row (${decimal(correctedBy.acquired_h100e_available)} H100e / ${usd(correctedBy.acquired_usd_value)}). This baseline is preserved for provenance; the difference is not usage or loss.`
      : isCorrection
        ? "This row corrects the same-day baseline and includes a research update; the difference is not measured usage."
        : "Append-only observation.";
    return `<li class="history-record ${stateClass}">
      <div class="history-record-heading"><div><span>${esc(snapshot.observed_on)}</span><strong>${esc(label(snapshot.event || "observation"))}</strong></div><span class="history-record-status">${esc(status)}</span></div>
      <p class="history-record-explanation">${esc(explanation)}</p>
      <dl class="history-record-values">
        <div><dt>Acquired / available</dt><dd>${decimal(snapshot.acquired_h100e_available)} H100e</dd></div>
        <div><dt>Acquired value</dt><dd>${usd(snapshot.acquired_usd_value)}</dd></div>
        <div><dt>Conditional ceiling</dt><dd>${decimal(historyValue(snapshot, "discovered_h100e_potential"))} H100e</dd></div>
        <div><dt>Actual usage</dt><dd>${decimal(historyValue(snapshot, "used_h100e"))} H100e</dd></div>
      </dl>
      <p class="history-record-notes"><strong>Record note:</strong> ${esc(snapshot.notes || "No note recorded.")}</p>
    </li>`;
  }).join("");
  return `<div class="history-provenance"><h3>Append-only history records</h3>${ceilingNotice}<ol class="history-records">${records}</ol></div>`;
}

function toolStats(tools) {
  if (!tools.length) return '<p class="muted">No per-tool effectiveness data recorded.</p>';
  return `<div class="tool-stats" aria-label="Tool effectiveness">${tools.map(tool => `
    <article><h3>${esc(label(tool.name))}</h3><p><strong>${tool.checks}</strong> checks / <strong>${tool.offers}</strong> offers / <strong>${tool.accounts}</strong> accounts / <strong>${decimal(tool.h100e)}</strong> H100e</p></article>`).join("")}</div>`;
}

function validateCatalog(catalog) {
  const warnings = [];
  if (!catalog || typeof catalog !== "object") throw new Error("catalog root is not an object");
  if (!Array.isArray(catalog.accounts) || !Array.isArray(catalog.offers)) throw new Error("catalog is missing accounts or offers arrays");
  if (catalog.policy?.maximum_financial_liability_usd !== 0) throw new Error("catalog does not enforce zero financial liability");
  const inspect = (item, type, index) => {
    const name = item.id || `${type} ${index + 1}`;
    if (!item.id) warnings.push(`${name}: missing id`);
    if (item.acquisition_origin != null && !ORIGINS.has(item.acquisition_origin)) warnings.push(`${name}: acquisition_origin is outside the v2 enum`);
    if (item.recurrence == null) warnings.push(`${name}: recurrence is missing`);
    else if (recurrenceOf(item) === "unknown" && item.recurrence !== "unknown") warnings.push(`${name}: recurrence is outside the v2 enum and known compatibility aliases`);
    if (item.hardware != null && typeof item.hardware !== "object") warnings.push(`${name}: hardware metadata is not an object`);
    if (item.usability != null && typeof item.usability !== "object") warnings.push(`${name}: usability metadata is not an object`);
    if (item.poolability != null && typeof item.poolability !== "object") warnings.push(`${name}: poolability metadata is not an object`);
    const hardware = hardwareOf(item);
    if (hardware.vramMin !== null && hardware.vramMax !== null && hardware.vramMin > hardware.vramMax) warnings.push(`${name}: VRAM minimum exceeds maximum`);
    if (item.usability?.usable_now != null && typeof item.usability.usable_now !== "boolean") warnings.push(`${name}: usable_now is not boolean`);
    if (item.poolability && !POOL_SUITABILITY.has(item.poolability.suitability)) warnings.push(`${name}: poolability suitability is outside the v2 enum`);
    if (item.poolability && !POOL_MECHANISMS.has(item.poolability.mechanism)) warnings.push(`${name}: poolability mechanism is outside the v2 enum`);
    if (item.poolability?.mechanism === "isolated_personal" && ["good", "conditional"].includes(item.poolability.suitability)) warnings.push(`${name}: poolability suitability conflicts with isolated_personal mechanism; aggregates fail closed to isolated`);
  };
  catalog.accounts.forEach((item, index) => inspect(item, "account", index));
  catalog.offers.forEach((item, index) => inspect(item, "offer", index));
  if (catalog.storage != null && !Array.isArray(catalog.storage)) warnings.push("storage: expected an optional array; storage UI is disabled");
  if (Array.isArray(catalog.storage)) catalog.storage.forEach((item, index) => {
    const name = item?.id || `storage ${index + 1}`;
    if (!item || typeof item !== "object") {
      warnings.push(`${name}: storage entry is not an object`);
      return;
    }
    if (!item.id) warnings.push(`${name}: missing id`);
    if (storageSafetyOf(item) === "unknown" && ![null, undefined, "unknown"].includes(item.status)) warnings.push(`${name}: storage safety/status is outside the UI compatibility enum`);
    if (item.capacity != null && typeof item.capacity !== "object") warnings.push(`${name}: capacity is not an object`);
  });
  if (catalog.history != null && !Array.isArray(catalog.history)) warnings.push("history: expected an array; history UI is disabled");
  return warnings;
}

function warningPanel(warnings) {
  if (!warnings.length) return "";
  return `<details class="validation-warning"><summary>${warnings.length} catalog compatibility warning${warnings.length === 1 ? "" : "s"}</summary><ul>${warnings.slice(0, 20).map(warning => `<li>${esc(warning)}</li>`).join("")}${warnings.length > 20 ? `<li>...and ${warnings.length - 20} more.</li>` : ""}</ul></details>`;
}

function usageBalance(account) {
  const value = number(account.balance);
  const unit = account.balance_unit || "";
  if (value === null) return account.balance == null ? "Not reported" : `${account.balance}${unit ? ` ${unit}` : ""}`;
  return /usd/i.test(unit) ? usd(value) : `${decimal(value)}${unit ? ` ${unit}` : ""}`;
}

function meterQuantity(value, unit) {
  const parsed = number(value);
  if (parsed === null) return "Not reported";
  return `${decimal(parsed)}${unit ? ` ${unit}` : ""}`;
}

function usageMeters(account) {
  const supplied = account?.meters;
  const meters = Array.isArray(supplied)
    ? supplied
    : supplied && typeof supplied === "object"
      ? Object.entries(supplied).map(([id, meter]) => meter && typeof meter === "object" ? { id, ...meter } : { id, value: meter })
      : [];
  const normalized = meters.filter(meter => meter && typeof meter === "object").map((meter, index) => ({
    id: String(meter.id || meter.kind || `meter-${index + 1}`),
    kind: String(meter.kind || "meter"),
    label: String(meter.label || meter.id || meter.kind || `Meter ${index + 1}`),
    value: number(meter.value),
    available: number(meter.available),
    used: number(meter.used),
    unit: String(meter.unit || "").trim(),
    resetAt: meter.reset_at || null,
    expiresAt: meter.expires_at || null
  }));
  if (normalized.length) return normalized;
  const legacyMeters = [
    { id: "h100e-hours", label: "Accelerator hours", available: number(account?.available_h100e), used: number(account?.used_h100e), unit: "H100e" },
    { id: "tpu-hours", label: "TPU hours", available: number(account?.available_tpu_hours), used: number(account?.used_tpu_hours), unit: "TPU hours" }
  ].filter(meter => meter.available !== null || meter.used !== null);
  return legacyMeters.map(meter => ({ ...meter, kind: "accelerator-hours", value: null, resetAt: null, expiresAt: null }));
}

function meterSummary(meter, { includeTiming = true } = {}) {
  const quantities = [];
  if (meter.value !== null) quantities.push(`Value ${meterQuantity(meter.value, meter.unit)}`);
  if (meter.available !== null) quantities.push(`Available ${meterQuantity(meter.available, meter.unit)}`);
  if (meter.used !== null) quantities.push(`Used ${meterQuantity(meter.used, meter.unit)}`);
  if (!quantities.length) quantities.push("No value reported");
  if (includeTiming && meter.resetAt) quantities.push(`Resets ${compactDateTime(meter.resetAt)}`);
  if (includeTiming && meter.expiresAt) quantities.push(`Expires ${compactDateTime(meter.expiresAt)}`);
  return quantities.join(" · ");
}

function meterList(meters, { compact = false } = {}) {
  if (!meters.length) return '<span class="muted">No meter values reported</span>';
  return `<ul class="meter-list ${compact ? "is-compact" : ""}">${meters.map(meter => `<li><strong>${esc(meter.label)}</strong><span>${esc(meterSummary(meter, { includeTiming: !compact }))}</span></li>`).join("")}</ul>`;
}

function activeCostRate(account) {
  const value = number(account.active_cost_per_hour);
  if (value === null) return "Cost rate unknown";
  const unit = String(account.active_cost_unit || "provider units").trim();
  return `${meterQuantity(value, unit)}/hour`;
}

function usageExternal(account) {
  if (account.external_activity_detected === true) {
    const deltas = account.deltas && typeof account.deltas === "object" ? account.deltas : {};
    const meters = usageMeters({ meters: deltas.meters });
    const balance = number(deltas.balance);
    const details = meters.length
      ? meterList(meters, { compact: true })
      : balance === null ? "" : `<br><span class="muted">${esc(`${balance >= 0 ? "+" : ""}${meterQuantity(balance, deltas.balance_unit || "") } balance`)}</span>`;
    return `<strong class="external-use">Detected</strong>${details}`;
  }
  if (account.external_activity_detected === false) return "Not detected";
  return '<span class="muted">Unknown</span>';
}

function onboardingMethodLabel(method) {
  return {
    none: "No credential needed",
    manual: "Manual read or local CLI",
    env_ref: "Environment reference",
    cli_session: "Existing local CLI session",
    transient: "Paste once for this browser session",
    reference: "Approved credential reference"
  }[method] || label(method);
}

function onboardingMethodNote(method) {
  return {
    none: "No authentication material is sent.",
    manual: "Use an already authenticated local tool, then record its redacted meter result below. This marks setup metadata only; it does not connect or make the account routable.",
    env_ref: "The local service reads an already configured environment reference; its name and value are never shown here.",
    cli_session: "Uses an already authenticated local CLI session; this app does not open a sign-in flow.",
    transient: "The value is sent once over loopback, then immediately cleared from this form. It stays only in process memory and is lost on restart.",
    reference: "Stores only an opaque reference and consent metadata. It never stores or displays a credential value."
  }[method] || "This capability is configured by the local service.";
}

function onboardingReadinessFor(profileId) {
  return (state.onboarding.data?.readiness || []).find(item => item && item.profile_id === profileId) || null;
}

function onboardingAllowedMethods(profileId) {
  const methods = onboardingReadinessFor(profileId)?.allowed_methods;
  return Array.isArray(methods) ? methods.filter(method => typeof method === "string") : [];
}

function onboardingReadinessStatus(item, key, fallback) {
  if (typeof item[key] === "boolean") return { value: item[key], label: item[key] ? "Ready" : "Not ready" };
  if (typeof item.armable === "boolean") return { value: item.armable, label: `${item.armable ? "Ready" : "Not ready"} (legacy combined readiness)` };
  return { value: null, label: fallback };
}

function onboardingConnectionStatus(item) {
  if (item.connected === true) return { value: true, label: "Connected" };
  if (item.missing_profile_definition === true) return { value: null, label: "Catalog metadata only" };
  if (item.session_only === true && Array.isArray(item.allowed_methods) && item.allowed_methods.includes("env_ref")) {
    return { value: false, label: "Environment unavailable" };
  }
  return { value: false, label: "Not connected" };
}

function onboardingSection() {
  if (state.onboarding.supported === false) return "";
  const data = state.onboarding.data || {};
  const readiness = Array.isArray(data.readiness) ? data.readiness.filter(item => item && typeof item === "object") : [];
  const checklist = Array.isArray(data.checklist) ? data.checklist.filter(item => item && typeof item === "object") : [];
  const profiles = readiness.map(item => `<option value="${esc(item.profile_id)}">${esc(item.account_id || item.profile_id)}</option>`).join("");
  const rows = readiness.map(item => {
    const connection = onboardingConnectionStatus(item);
    const policy = onboardingReadinessStatus(item, "policy_eligible", "Not reported");
    const routable = onboardingReadinessStatus(item, "routable_now", "Not reported");
    const status = result => `<span class="pill ${result.value === true ? "status-ready" : result.value === false ? "status-warn" : "status-unknown"}">${esc(result.label)}</span>`;
    const setupNote = item.missing_profile_definition === true && item.next_action
      ? `<br><span class="muted">${esc(item.next_action)}</span>`
      : "";
    return `<tr><td><strong>${esc(item.account_id || item.profile_id)}</strong><br><span class="muted">${esc(item.profile_id)}</span>${setupNote}</td><td>${status(connection)}</td><td>${pill(item.balance_verified === true ? "ready" : "unknown")}</td><td>${pill(item.zero_liability_verified === true ? "ready" : "unknown")}</td><td>${status(policy)}</td><td>${status(routable)}</td></tr>`;
  }).join("");
  const checklistMarkup = checklist.length ? `<ol class="onboarding-checklist">${checklist.map(item => `<li><span class="pill status-${esc(item.status || "unknown")}">${esc(label(item.status || "unknown"))}</span><span>${esc(item.prompt || item.id)}</span></li>`).join("")}</ol>` : "<p class=\"muted\">The catalog works now. Connect only the account capability you want to monitor.</p>";
  return `<section id="onboarding" class="onboarding-section" aria-live="polite">
    <div class="section-heading"><div><p class="eyebrow">First use / account meter setup</p><h2>Connect only the meter capability you need</h2><p>Browse the catalog immediately. Connection, balance verification, zero-liability verification, policy eligibility, and current routability are separate facts; a missing credential disables only its affected account capability.</p></div><span class="pill">Optional</span></div>
    ${checklistMarkup}
    <details class="onboarding-readiness"><summary>Review readiness for ${readiness.length} account${readiness.length === 1 ? "" : "s"}</summary><div class="table-wrap onboarding-table"><table><thead><tr><th>Account</th><th>Connected</th><th>Balance verified</th><th>Zero liability</th><th>Policy eligible</th><th>Routable now</th></tr></thead><tbody>${rows || "<tr><td colspan=\"6\">No account connection is configured. Catalog and manual meter evidence remain available.</td></tr>"}</tbody></table></div></details>
    ${readiness.length ? `<details class="credential-connect"><summary>Set up an account meter or session</summary><p>Sign-in, eligibility, and no-payment checks remain yours to confirm. This screen never creates or retrieves credentials. An agent-acquired reference requires your explicit consent and must already be available through an authorized secure route.</p><form id="onboarding-connect-form"><div class="onboarding-grid"><label>Account capability<select id="onboarding-profile" required>${profiles}</select></label><label>Connection method<select id="onboarding-method" required></select></label><label>Provenance<select id="onboarding-provenance" required><option value="user_supplied">User supplied</option><option value="existing_session">Existing session</option><option value="agent_acquired">Agent acquired (consented reference)</option></select></label><label id="onboarding-value-wrap" hidden>Session-only value<input id="onboarding-value" type="password" autocomplete="off" spellcheck="false" maxlength="4096"></label><label id="onboarding-reference-wrap" hidden>Opaque reference<input id="onboarding-reference" autocomplete="off" spellcheck="false" maxlength="160" pattern="[A-Za-z0-9._-]+"></label></div><label id="onboarding-assisted-wrap" class="consent-check" hidden><input id="onboarding-assisted" type="checkbox"> Set up a session-only OpenAI-compatible dispatch endpoint for this catalog account</label><div id="onboarding-assisted-fields" class="onboarding-grid" hidden><label>Base URL<input id="onboarding-base-url" type="url" autocomplete="off" spellcheck="false" placeholder="https://service.example/" maxlength="2048"></label><label>Relative endpoint on that base URL<input id="onboarding-endpoint" autocomplete="off" spellcheck="false" value="v1/chat/completions" maxlength="1024"></label><label>Session authentication<select id="onboarding-session-method"><option value="transient">Paste once</option><option value="env_ref">Environment reference</option></select></label><label id="onboarding-env-ref-wrap" hidden>Environment reference<input id="onboarding-env-ref" autocomplete="off" spellcheck="false" maxlength="160" pattern="[A-Za-z_][A-Za-z0-9_]*"></label></div><p id="onboarding-method-note" class="muted"></p><label class="consent-check"><input id="onboarding-consent" type="checkbox" required> I consent to this local, capability-specific setup.</label><div class="onboarding-actions"><button class="primary-action" type="submit" ${state.onboarding.submitting ? "disabled" : ""}>${state.onboarding.submitting ? "Saving…" : "Save meter setup"}</button><button id="onboarding-clear" type="button" ${state.onboarding.submitting ? "disabled" : ""}>Clear this session setup</button></div><p class="${state.onboarding.error ? "inline-error" : "muted"}" aria-live="polite">${esc(state.onboarding.error || state.onboarding.feedback || "No credential values are retained in browser storage, rendered after submission, or written to the catalog.")}</p></form></details>` : ""}
  </section>`;
}

function liveUsageSection() {
  if (state.live.supported === false) {
    return `<section id="live-usage" class="live-section static-mode" aria-live="polite">
      <div class="section-heading"><div><p class="eyebrow">Live usage overlay</p><h2>Catalog-only mode</h2><p>The loopback usage API is not available. The static Free Compute app remains usable and no live balance is implied.</p></div><span class="pill">Static</span></div>
      <button id="retry-live-api" type="button">Enable API</button>
    </section>`;
  }
  const data = state.live.data;
  const accounts = Array.isArray(data?.accounts) ? data.accounts.filter(item => item && typeof item === "object") : [];
  const staleByAge = state.live.lastSuccessAt && Date.now() - state.live.lastSuccessAt > LIVE_POLL_MS * 2.5;
  const stale = Boolean(state.live.error || staleByAge || accounts.some(account => account.status === "error"));
  const monitoring = data?.monitoring && typeof data.monitoring === "object" ? data.monitoring : {};
  const refreshLabel = state.live.lastSuccessAt ? compactDateTime(state.live.lastSuccessAt) : "Awaiting first meter read";
  const activeAccounts = accounts.filter(account => (number(account.active_jobs) ?? 0) > 0 || (number(account.active_cost_per_hour) ?? 0) > 0);
  const accountOptions = (state.catalog?.accounts || []).map(account => `<option value="${esc(account.id)}">${esc(account.provider || account.id)} — ${esc(account.id)}</option>`).join("");
  const activeAlerts = activeAccounts.map(account => {
    const meters = usageMeters(account);
    return `<article class="spend-alert"><div><p class="eyebrow">Active external work</p><h3>${esc(account.provider || account.account_id || "Provider")}</h3><p>${number(account.active_jobs) === null ? "Job count unknown" : `${decimal(account.active_jobs, 0)} active job${number(account.active_jobs) === 1 ? "" : "s"}`} · <strong>${esc(activeCostRate(account))}</strong></p></div><dl><div><dt>Balance</dt><dd>${esc(usageBalance(account))}</dd></div><div><dt>Meters</dt><dd>${meterList(meters, { compact: true })}</dd></div><div><dt>Observed</dt><dd>${esc(compactDateTime(account.observed_at))}</dd></div><div><dt>Expiry</dt><dd>${esc(compactDateTime(account.expires_at))}</dd></div></dl><p class="monitor-only"><strong>Monitor only.</strong> This app does not stop, restart, or launch this external workload.</p></article>`;
  }).join("");
  const rows = accounts.map(account => `
    <tr>
      <td><strong>${esc(account.provider || account.account_id || "Unknown account")}</strong><br><span class="muted">${esc(account.account_id || "No account id")}</span></td>
      <td>${pill(account.status || "never_polled")}<br><span class="muted">Observed ${esc(compactDateTime(account.observed_at))}</span>${account.error ? `<br><span class="inline-error">${esc(account.error.message || account.error.code || account.error)}</span>` : ""}</td>
      <td><strong>${esc(usageBalance(account))}</strong><br><span class="muted">Next poll ${esc(compactDateTime(account.next_poll_at))}</span></td>
      <td>${meterList(usageMeters(account))}</td>
      <td>${usageExternal(account)}</td>
      <td>${number(account.active_jobs) === null ? "Active jobs unknown" : `${decimal(account.active_jobs, 0)} active`}<br><span class="muted">${esc(activeCostRate(account))} / expiry ${esc(compactDateTime(account.expires_at))}</span></td>
    </tr>`).join("");
  const configured = number(monitoring.configured);
  const enabled = number(monitoring.enabled);
  const modeLabel = monitoring.running === true
    ? `Polling (${enabled ?? 0} enabled)`
    : configured === 0 ? "Not configured" : enabled === 0 ? "Disabled" : "Paused";

  return `<section id="live-usage" class="live-section ${stale ? "is-stale" : ""} ${activeAccounts.length ? "has-active-spend" : ""}" aria-live="polite">
    <div class="section-heading"><div><p class="eyebrow">Usage and credit watch</p><h2>${activeAccounts.length ? `${activeAccounts.length} monitored account${activeAccounts.length === 1 ? "" : "s"} reporting active work` : "Balances and meter availability"}</h2><p>Meter observations include external use when detected. Observation never grants permission to dispatch work.</p></div><span class="pill ${stale ? "status-error" : activeAccounts.length ? "status-warn" : "status-live"}">${stale ? "Stale / attention" : activeAccounts.length ? "Active use" : esc(modeLabel)}</span></div>
    ${activeAlerts ? `<div class="spend-alerts">${activeAlerts}</div>` : ""}
    <div class="live-meta" role="status">
      <span><strong>Last refresh:</strong> ${esc(refreshLabel)}</span>
      <span><strong>Provider as-of:</strong> ${esc(compactDateTime(data?.as_of))}</span>
      <span><strong>Monitor:</strong> ${esc(modeLabel)}</span>
      ${state.live.error ? `<span class="inline-error"><strong>Error:</strong> ${esc(state.live.error)}</span>` : ""}
      <button id="refresh-usage" type="button" ${state.live.loading ? "disabled" : ""}>${state.live.loading ? "Refreshing..." : "Refresh meters"}</button>
    </div>
    <div class="table-wrap live-table"><table>
      <thead><tr><th scope="col">Provider / account</th><th scope="col">Meter status</th><th scope="col">Balance</th><th scope="col">Meters</th><th scope="col">External use</th><th scope="col">Active work</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6">${state.live.loading ? "Loading live meters..." : "No monitored accounts were returned."}</td></tr>`}</tbody>
    </table></div>
    <details class="meter-observation"><summary>Record a read-only meter observation</summary><p>Use this when a provider session or CLI shows fresh usage that an automatic monitor cannot read. No API key, login, or secret is accepted. The generic meter fields support any unit; accelerator-hour fields are only convenience shortcuts.</p>
      <form id="observe-usage-form"><div class="observation-grid">
        <label>Account<select id="observe-account" required><option value="">Choose catalog account</option>${accountOptions}</select></label>
        <label>Balance<input id="observe-balance" type="number" step="any" inputmode="decimal"></label><label>Balance unit<input id="observe-balance-unit" maxlength="24" placeholder="USD, credits..."></label>
        <label>Meter ID<input id="observe-meter-id" maxlength="80" placeholder="tpu-hours"></label><label>Meter kind<input id="observe-meter-kind" maxlength="80" placeholder="accelerator-hours"></label><label>Meter unit<input id="observe-meter-unit" maxlength="32" placeholder="TPU hours, credits..."></label>
        <label>Meter value<input id="observe-meter-value" type="number" step="any" inputmode="decimal"></label><label>Meter available<input id="observe-meter-available" type="number" step="any" inputmode="decimal"></label><label>Meter used<input id="observe-meter-used" type="number" step="any" inputmode="decimal"></label><label>Meter reset (optional)<input id="observe-meter-reset" type="datetime-local"></label><label>Meter expiry (optional)<input id="observe-meter-expiry" type="datetime-local"></label>
        <label>Available H100e<input id="observe-available-h100e" type="number" min="0" step="any" inputmode="decimal"></label><label>Used H100e<input id="observe-used-h100e" type="number" min="0" step="any" inputmode="decimal"></label><label>Available TPU hours<input id="observe-available-tpu" type="number" min="0" step="any" inputmode="decimal"></label><label>Used TPU hours<input id="observe-used-tpu" type="number" min="0" step="any" inputmode="decimal"></label>
        <label>Active jobs<input id="observe-active-jobs" type="number" min="0" step="1" inputmode="numeric"></label><label>Hourly cost<input id="observe-cost-hour" type="number" min="0" step="any" inputmode="decimal"></label><label>Cost unit<input id="observe-cost-unit" maxlength="32" placeholder="credits, USD..."></label><label>Credit expiry<input id="observe-expiry" type="datetime-local"></label>
      </div><button class="primary-action" type="submit" ${state.live.observing ? "disabled" : ""}>${state.live.observing ? "Recording…" : "Record observation"}</button><p class="${state.live.observeFeedback?.startsWith("Error:") ? "inline-error" : "muted"}" aria-live="polite">${esc(state.live.observeFeedback || "At least one meter value is required. This cannot arm or alter provider resources.")}</p></form>
    </details>
  </section>`;
}

function armProviderRows(catalog) {
  const referenceRate = number(catalog.normalization?.reference_usd_per_h100e_hour);
  return catalog.accounts.filter(account => {
    return account.acquired_safe === true
      && account.hard_stop === true
      && account.paid_fallback_allowed !== true
      && usabilityOf(account).usableNow === true;
  }).map(account => {
    const h100e = accountH100e(account, referenceRate);
    return {
      account,
      h100e,
      stability: interruptibilityOf(account),
      hardware: hardwareOf(account),
      compute: computeProfileOf(account)
    };
  }).sort((left, right) => {
    const stabilityOrder = { non_interruptible: 0, interruptible: 1, unknown: 2 };
    const stability = (stabilityOrder[left.stability] ?? 3) - (stabilityOrder[right.stability] ?? 3);
    if (stability) return stability;
    const capacity = (right.h100e ?? -1) - (left.h100e ?? -1);
    if (capacity) return capacity;
    return String(left.account.provider || left.account.id).localeCompare(String(right.account.provider || right.account.id));
  });
}

function armProviderGroup(title, note, rows, firstRank, className = "") {
  if (!rows.length) return "";
  return `<fieldset class="provider-group ${className}"><legend>${esc(title)}</legend><p class="field-note">${esc(note)}</p><div class="choice-grid">${rows.map((row, index) => {
    const account = row.account;
    const hardware = row.hardware.bestGpu || row.hardware.gpuModels[0] || label(row.hardware.computeClass);
    const checked = state.armDraft.providers.has(account.id) ? "checked" : "";
    return `<label class="provider-choice"><input type="checkbox" name="arm-provider" value="${esc(account.id)}" ${checked} ${state.arm.submitting ? "disabled" : ""}><span><strong>#${firstRank + index} ${esc(account.provider || account.id)}</strong><small>${row.h100e === null ? isTpuOnly(account) ? "TPU native quota / not H100e" : "Safe / unconverted" : `${decimal(row.h100e)} H100e${row.compute.backends.has("tpu") ? "; TPU excluded" : ""}`} / ${esc(hardware)}</small>${computeFamilyBadges(account)}<small>${esc(account.id)} / ${esc(label(row.stability))}</small></span></label>`;
  }).join("")}</div></fieldset>`;
}

function armPoolCompatibility(catalog) {
  const selected = catalog.accounts.filter(account => state.armDraft.providers.has(account.id));
  if (!selected.length) return { warning: false, text: "Select providers to preview routing-pool backend compatibility." };
  const profiles = selected.map(account => ({ account, compute: computeProfileOf(account) }));
  const knownBackends = new Set(profiles.flatMap(row => [...row.compute.backends].filter(value => value !== "unknown")));
  const unknown = profiles.some(row => row.compute.backends.has("unknown"));
  const hasBlackwell = profiles.some(row => row.compute.blackwell);
  const hasOlderCuda = profiles.some(row => row.compute.backends.has("cuda") && !row.compute.blackwell);
  const messages = [];
  if (knownBackends.size > 1) messages.push(`Cross-backend pool: ${[...knownBackends].map(value => value.toUpperCase()).join(" + ")}. Each job must request one compatible backend.`);
  if (hasBlackwell && hasOlderCuda) messages.push("Blackwell and older CUDA are both selected; do not assume architecture, kernel, or image parity.");
  if (unknown) messages.push("At least one selected account has an unknown or other compute family; dispatch must fail closed on unmet requirements.");
  if (!messages.length) messages.push(`Pool family: ${profiles.map(row => computeFamilyText(row.account)).filter((value, index, all) => all.indexOf(value) === index).join(", ")}.`);
  return { warning: knownBackends.size > 1 || hasBlackwell && hasOlderCuda || unknown, text: messages.join(" ") };
}

function armPoolWarningMarkup(catalog) {
  const compatibility = armPoolCompatibility(catalog);
  return `<p id="arm-family-warning" class="pool-compatibility ${compatibility.warning ? "is-warning" : ""}" role="status"><strong>Pool compatibility:</strong> ${esc(compatibility.text)}</p>`;
}

function armStateSummary() {
  const current = state.arm.data?.arm && typeof state.arm.data.arm === "object" ? state.arm.data.arm : state.arm.data;
  if (!current || typeof current !== "object") {
    const message = state.arm.supported === false ? "Arm API unavailable in static mode." : "Checking Arm state...";
    return `<div class="arm-state"><strong>State unknown</strong><span>${esc(message)}</span>${state.arm.supported === false ? '<button id="retry-arm-api" type="button">Enable API</button>' : ""}</div>`;
  }
  const providers = Array.isArray(current.providers) ? current.providers : [];
  const storage = Array.isArray(current.storage_ids) ? current.storage_ids : [];
  const shutdown = current.shutdown && typeof current.shutdown === "object" ? current.shutdown : {};
  const providerNames = providers.map(value => {
    const id = typeof value === "object" ? value.account_id || value.id : value;
    const account = state.catalog?.accounts?.find(item => item.id === id);
    if (account) return `${account.provider || id} (${computeFamilyText(account)})`;
    return typeof value === "object" ? value.provider || id : id;
  }).filter(Boolean);
  const shutdownParts = [
    shutdown.duration_minutes != null ? `${shutdown.duration_minutes} min` : null,
    shutdown.max_jobs != null ? `${shutdown.max_jobs} jobs` : null,
    shutdown.max_h100e != null ? `${shutdown.max_h100e} H100e` : null,
    shutdown.balance_floor != null ? `floor ${shutdown.balance_floor}` : null,
    shutdown.idle_minutes != null ? `${shutdown.idle_minutes} min idle` : null,
    shutdown.max_errors != null ? `${shutdown.max_errors} errors` : null
  ].filter(Boolean);
  return `<div class="arm-state ${current.armed === true ? "is-armed" : ""}" role="status">
    <div><span class="pill ${current.armed === true ? "status-ready" : ""}">${esc(current.armed === true ? "Armed" : current.status || "Disarmed")}</span><strong>${current.armed === true ? " Routing pool active" : " No automatic routing"}</strong></div>
    <dl class="arm-facts">
      <div><dt>Providers</dt><dd>${esc(providerNames.join(", ") || "None")}</dd></div>
      <div><dt>Storage</dt><dd>${esc(storage.join(", ") || "None")}</dd></div>
      <div><dt>Expires</dt><dd>${esc(compactDateTime(current.expires_at))}</dd></div>
      <div><dt>Progress</dt><dd>${esc(current.jobs_started ?? 0)} jobs / ${decimal(current.h100e_used)} H100e</dd></div>
      <div><dt>Last activity</dt><dd>${esc(compactDateTime(current.last_activity_at))}</dd></div>
      <div><dt>Shutoffs</dt><dd>${esc(shutdownParts.join(" / ") || "Not reported")}</dd></div>
    </dl>
    ${current.reason ? `<p class="muted"><strong>Reason:</strong> ${esc(current.reason)}</p>` : ""}
    ${Array.isArray(current.warnings) && current.warnings.length ? `<ul class="arm-warnings">${current.warnings.map(warning => `<li>${esc(warning)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

function armControlSection(catalog) {
  const rows = armProviderRows(catalog);
  const dependable = rows.filter(row => row.stability === "non_interruptible");
  const interruptible = rows.filter(row => row.stability === "interruptible");
  const unknown = rows.filter(row => !["non_interruptible", "interruptible"].includes(row.stability));
  const safeStorage = (Array.isArray(catalog.storage) ? catalog.storage : [])
    .filter(item => item && typeof item === "object" && storageIsZeroLiability(item) && item.usable_now !== false)
    .sort((left, right) => {
      const usable = Number(right.usable_now === true) - Number(left.usable_now === true);
      if (usable) return usable;
      return (storageCapacityOf(right).gib ?? -1) - (storageCapacityOf(left).gib ?? -1);
    });
  const disabled = state.arm.supported === false || state.arm.submitting;
  const rankAfterDependable = dependable.length + 1;
  const rankAfterInterruptible = rankAfterDependable + interruptible.length;
  const storageOptions = safeStorage.map(item => {
    const capacity = storageCapacityOf(item);
    const selected = state.armDraft.storageId === item.id ? "selected" : "";
    return `<option value="${esc(item.id)}" ${selected}>${esc(item.provider || item.id)} - ${esc(capacity.display)} - ${esc(label(storagePersistenceOf(item)))}</option>`;
  }).join("");
  const feedbackClass = state.arm.error ? "inline-error" : "muted";

  return `<section id="arm-control" class="arm-section">
    <div class="section-heading"><div><p class="eyebrow">Friendly Arm Compute / V1</p><h2>Bounded single-provider routing</h2><p>Select several safe accounts as a routing pool. <strong>Each V1 job runs on exactly one selected provider.</strong> Distributed multi-provider jobs are future work.</p></div><span class="pill ${state.arm.data?.armed === true ? "status-ready" : ""}">${state.arm.data?.armed === true ? "Armed" : "Fail closed"}</span></div>
    ${armStateSummary()}
    <form id="arm-form" class="arm-form">
      <div class="arm-columns">
        <div>
          <h3>1. Choose a safe routing pool</h3>
          <p class="field-note">Only safely acquired accounts marked usable now are offered. Ranking favors dependable capacity. Selection stays in page memory only.</p>
          ${armProviderGroup("Dependable providers", "Non-interruptible suggestions are listed first.", dependable, 1, "dependable-group")}
          ${armProviderGroup("Interruptible providers", "Separate lane: use only for checkpointable jobs.", interruptible, rankAfterDependable, "interruptible-group")}
          ${armProviderGroup("Stability unverified", "Verify interruption behavior before relying on these accounts.", unknown, rankAfterInterruptible, "unknown-group")}
          ${rows.length ? "" : '<p class="empty">No safely acquired, currently usable account is eligible for Arm.</p>'}
          ${armPoolWarningMarkup(catalog)}
          <label class="full-field">Optional persistent storage
            <select id="arm-storage" ${state.arm.submitting ? "disabled" : ""}><option value="">No storage choice</option>${storageOptions}</select>
            <small>Only confirmed-free storage is listed. Credit-consuming storage is never enabled here.</small>
          </label>
        </div>
        <div>
          <h3>2. Set automatic shutoffs</h3>
          <p class="field-note">Duration and absolute expiry use the earlier limit. The service also stops at any other bound below.</p>
          <div class="limits-grid">
            <label>Duration (minutes)<input id="arm-duration" type="number" min="1" step="1" value="${esc(state.armDraft.durationMinutes)}" inputmode="numeric"></label>
            <label>Absolute expiry<input id="arm-expiry" type="datetime-local" value="${esc(state.armDraft.expiresAt)}"></label>
            <label>Maximum jobs<input id="arm-max-jobs" type="number" min="1" step="1" value="${esc(state.armDraft.maxJobs)}" inputmode="numeric"></label>
            <label>Maximum usage (H100e)<input id="arm-max-h100e" type="number" min="0.01" step="0.01" value="${esc(state.armDraft.maxH100e)}" inputmode="decimal"></label>
            <label>Balance floor (provider units)<input id="arm-balance-floor" type="number" min="0" step="0.01" value="${esc(state.armDraft.balanceFloor)}" inputmode="decimal"></label>
            <label>Idle shutoff (minutes)<input id="arm-idle" type="number" min="1" step="1" value="${esc(state.armDraft.idleMinutes)}" inputmode="numeric"></label>
            <label>Maximum consecutive errors<input id="arm-max-errors" type="number" min="1" step="1" value="${esc(state.armDraft.maxErrors)}" inputmode="numeric"></label>
          </div>
          <div class="arm-actions">
            <button id="arm-submit" class="primary-action" type="submit" ${disabled || !rows.length ? "disabled" : ""}>${state.arm.submitting ? "Applying..." : "Arm selected pool"}</button>
            <button id="disarm-submit" type="button" ${disabled || state.arm.data?.armed !== true ? "disabled" : ""}>Disarm now</button>
          </div>
          <p id="arm-feedback" class="${feedbackClass}" aria-live="polite">${esc(state.arm.error || state.arm.feedback || (state.arm.supported === false ? "Start the loopback API to arm routing." : "No secret or credential values are requested, stored, or sent by this form."))}</p>
        </div>
      </div>
    </form>
    ${autoArmPanel(disabled, safeStorage)}
    <p class="v15-note"><strong>V1.5:</strong> Prompt-to-plan through Codex, Claude Code, or an OpenAI-compatible tool, plus any session-only key helper. V1 core Arm remains usable without entering authentication here.</p>
  </section>`;
}

function autoArmPanel(disabled, safeStorage) {
  const draft = state.autoDraft;
  const hasSafeStorage = safeStorage.length > 0;
  const accessIds = [...new Set(safeStorage.flatMap(item => listOf(item.access)))].sort();
  const selected = (value, current) => value === current ? "selected" : "";
  return `<details class="auto-arm" open>
    <summary>Auto-arm from a small deterministic job shape</summary>
    <p>Describe resources, not prompts or credentials. The service ranks safe candidates deterministically, arms a bounded pool, and returns the chosen compute/storage plan.</p>
    <form id="auto-arm-form">
      <div class="auto-grid">
        <label>Job kind<select id="auto-kind"><option value="python" ${selected("python", draft.kind)}>Python</option><option value="notebook" ${selected("notebook", draft.kind)}>Notebook</option><option value="openai_inference" ${selected("openai_inference", draft.kind)}>OpenAI-compatible inference</option><option value="data" ${selected("data", draft.kind)}>Data job</option><option value="command" ${selected("command", draft.kind)}>Command</option></select></label>
        <label>Compute backend<select id="auto-compute-backend"><option value="any" ${selected("any", draft.computeBackend)}>Any safe backend</option><option value="cuda" ${selected("cuda", draft.computeBackend)}>CUDA</option><option value="tpu" ${selected("tpu", draft.computeBackend)}>TPU</option><option value="rocm" ${selected("rocm", draft.computeBackend)}>ROCm</option><option value="oneapi" ${selected("oneapi", draft.computeBackend)}>oneAPI</option><option value="cpu" ${selected("cpu", draft.computeBackend)}>CPU</option></select></label>
        <label>GPU count<input id="auto-gpu-count" type="number" min="0" max="1" step="1" value="${esc(draft.gpuCount)}" inputmode="numeric"><small>V1 supports at most one GPU per job; multi-GPU is Phase 2.</small></label>
        <label>Minimum VRAM (GB)<input id="auto-min-vram" type="number" min="0" max="4096" step="1" value="${esc(draft.minVram)}" inputmode="numeric"></label>
        <label>Preferred VRAM (GB)<input id="auto-preferred-vram" type="number" min="0" max="4096" step="1" value="${esc(draft.preferredVram)}" inputmode="numeric"></label>
        <label>Interruptibility<select id="auto-interruptibility"><option value="forbidden" ${selected("forbidden", draft.interruptibility)}>Forbidden / dependable only</option><option value="allowed" ${selected("allowed", draft.interruptibility)}>Allowed</option><option value="required" ${selected("required", draft.interruptibility)}>Required / interruptible only</option></select></label>
        <label>Runtime (minutes)<input id="auto-runtime" type="number" min="1" max="10080" step="1" value="${esc(draft.runtimeMinutes)}" inputmode="numeric"></label>
        <label>Routing pool size<input id="auto-provider-count" type="number" min="1" max="4" step="1" value="${esc(draft.providerCount)}" inputmode="numeric"><small>Each job still uses one provider.</small></label>
      </div>
      <label class="check-line"><input id="auto-blackwell-required" type="checkbox" ${draft.blackwellRequired ? "checked" : ""}> Require Blackwell-CUDA <span class="muted">(explicit CUDA subtype)</span></label>
      <label class="check-line"><input id="auto-storage-required" type="checkbox" ${draft.storageRequired ? "checked" : ""} ${hasSafeStorage ? "" : "disabled"}> Require persistent storage ${hasSafeStorage ? "" : "(no safe storage cataloged)"}</label>
      <div id="auto-storage-options" class="auto-grid storage-requirements" ${draft.storageRequired ? "" : "hidden"}>
        <label>Minimum storage (GiB)<input id="auto-storage-min" type="number" min="0.01" step="0.01" value="${esc(draft.storageMinGib)}" inputmode="decimal"></label>
        <label>Persistence<select id="auto-storage-persistence"><option value="any" ${selected("any", draft.storagePersistence)}>Any</option><option value="run" ${selected("run", draft.storagePersistence)}>Run</option><option value="medium_term" ${selected("medium_term", draft.storagePersistence)}>Medium term</option><option value="long_term" ${selected("long_term", draft.storagePersistence)}>Long term</option><option value="archive" ${selected("archive", draft.storagePersistence)}>Archive</option></select></label>
        <label>Required access<select id="auto-storage-access"><option value="" ${selected("", draft.storageAccess)}>Any supported access</option>${accessIds.map(value => `<option value="${esc(value)}" ${selected(value, draft.storageAccess)}>${esc(label(value))}</option>`).join("")}</select></label>
      </div>
      <p class="field-note">Auto-arm may select external storage independently of compute. Review egress and locality in the returned plan.</p>
      <button id="auto-arm-submit" class="primary-action" type="submit" ${disabled ? "disabled" : ""}>Auto-rank and arm</button>
    </form>
  </details>`;
}

function render() {
  const catalog = state.catalog;
  const summary = totals(catalog);
  const activeGpu = catalog.offers.filter(offer => isGpu(offer) && !isDeferred(offer));
  const dependable = activeGpu.filter(offer => interruptibilityOf(offer) === "non_interruptible");
  const interruptible = activeGpu.filter(offer => interruptibilityOf(offer) === "interruptible");
  const unknown = activeGpu.filter(offer => interruptibilityOf(offer) === "unknown");
  const secondary = catalog.offers.filter(offer => !isGpu(offer) && !isDeferred(offer));
  const deferred = catalog.offers.filter(isDeferred);
  const previouslyHeld = catalog.accounts.filter(account => originOf(account) === "previously_had");
  const foundHere = catalog.accounts.filter(account => originOf(account) === "found_this_project");
  const originUnknown = catalog.accounts.filter(account => originOf(account) === "unknown");
  const filteredOffers = catalog.offers.filter(matches);
  const statuses = [...new Set([
    ...catalog.accounts.map(item => item.status),
    ...catalog.offers.map(item => item.status),
    ...(catalog.blockers || []).map(item => item.status)
  ].filter(Boolean))].sort();
  const computeClasses = [...new Set([...catalog.accounts, ...catalog.offers].map(item => hardwareOf(item).computeClass).filter(Boolean))].sort();
  const option = (value, current) => `<option value="${esc(value)}" ${current === value ? "selected" : ""}>${esc(label(value))}</option>`;

  app.innerHTML = `
    <header class="hero">
      <div><p class="eyebrow">Free Compute app / zero-liability inventory / as of ${esc(catalog.as_of)}</p><h1>Free Compute</h1><p>GPU-first compute and persistent storage. Conditional catalog ceilings are never counted as acquired or found usable.</p><p class="catalog-source">Loaded: <code>${esc(state.catalogSource)}</code></p></div>
      <div class="policy">Max liability <strong>${usd(catalog.policy.maximum_financial_liability_usd)}</strong></div>
    </header>

    ${onboardingSection()}
    ${liveUsageSection()}

    <section class="metrics primary-metrics" aria-label="Usable compute totals">
      <article><span>Zero-liability usable now</span><strong>${decimal(summary.usableH100e)} H100e</strong><small>${usd(summary.usableUsd)} across ${summary.usableNow} safe accounts; conditional offers excluded</small></article>
      <article><span>Safe acquired availability</span><strong>${decimal(summary.safeH100e)} H100e</strong><small>${usd(summary.safeUsd)}; ${summary.safeUnconverted} unconverted; TPU-native quota separate</small></article>
    </section>

    ${warningPanel(state.warnings)}
    <div class="decision-grid">
      ${pickerSection(catalog)}
      ${armControlSection(catalog)}
    </div>

    <details class="inventory-controls"><summary>Inventory filters and advanced views</summary><p>These controls affect provider, account, and offer inventory below; they do not filter live meters, Arm state, storage, or history.</p><div class="toolbar" role="search" aria-label="Filter compute inventory">
      <label class="search-field">Search inventory <input id="search" type="search" value="${esc(state.query)}" placeholder="provider, GPU, allowance..." autocomplete="off"></label>
      <label>Status <select id="status-filter"><option value="all">All statuses</option>${statuses.map(status => `<option value="${esc(status)}" ${state.status === status ? "selected" : ""}>${esc(statusLabel(status))}</option>`).join("")}</select></label>
      <label>Origin <select id="origin-filter"><option value="all">All origins</option>${[...ORIGINS].map(value => option(value, state.origin)).join("")}</select></label>
      <label>Cadence <select id="recurrence-filter"><option value="all">All cadences</option>${CADENCE_FILTERS.map(value => `<option value="${esc(value)}" ${state.recurrence === value ? "selected" : ""}>${esc(value === "one_time" ? "Single-use" : value === "recurring" ? "Recurring (any)" : label(value))}</option>`).join("")}</select></label>
      <label>Stability <select id="interruptibility-filter"><option value="all">All stability</option>${[...INTERRUPTIBILITY].map(value => option(value, state.interruptibility)).join("")}</select></label>
      <label>Pool fit <select id="poolability-filter"><option value="all">All pool fit</option>${[...POOL_SUITABILITY].map(value => option(value, state.poolability)).join("")}</select></label>
      <label>Compute family <select id="compute-family-filter"><option value="all">All families</option>${COMPUTE_FAMILIES.map(value => `<option value="${esc(value)}" ${state.computeFamily === value ? "selected" : ""}>${esc(computeFamilyLabel(value))}</option>`).join("")}</select></label>
      <label>Compute <select id="compute-filter"><option value="all">All classes</option>${computeClasses.map(value => option(value, state.computeClass)).join("")}</select></label>
      <button id="clear-filters" type="button">Clear inventory filters</button>
    </div></details>

    ${acquisitionSection(catalog)}
    <details class="advanced-inventory"><summary>Advanced compute inventory and ledgers</summary>
      ${aggregatePanels(catalog, filteredOffers, summary)}
      ${accountTable("accounts-previous", "Previously held", "Compute and credits that existed before this project.", previouslyHeld, catalog.normalization)}
      ${accountTable("accounts-found", "Found / acquired in this project", "New zero-liability availability discovered or acquired by this project.", foundHere, catalog.normalization)}
      ${originUnknown.length ? accountTable("accounts-unknown", "Origin not classified", "Compatibility fallback; these entries are never assumed newly acquired.", originUnknown, catalog.normalization) : ""}
      ${offerSection("gpu-dependable", "Dependable / non-interruptible", "Stable execution paths only; grants and payment-backed offers are listed elsewhere.", dependable)}
      ${offerSection("gpu-interruptible", "Interruptible / preemptible", "Separate checkpointable lane; these sessions can be reclaimed or disconnected.", interruptible)}
      ${offerSection("gpu-unknown", "Stability unknown", "Verify preemption and session behavior before treating these as dependable.", unknown)}
      ${simpleOfferSection("secondary", "TPU-native, CPU, serverless, CI, and inference", "Secondary compute / TPU kept native", secondary)}
      ${simpleOfferSection("deferred", "Applications and liability gates", "Grants / payment-blocked", deferred)}
    </details>
    ${storageSection(catalog)}
    ${historySection(catalog.history, catalog.tracking)}
    ${blockerSection(catalog.blockers)}
    ${normalizationSection(catalog.normalization)}
    <footer>Loaded source: <code>${esc(state.catalogSource)}</code>. Personal identifiers are omitted. Unknown metadata is never inferred as safe or usable. This UI has no secret-entry or secret-persistence path.</footer>`;

  document.querySelector("#search").addEventListener("input", event => {
    state.query = event.target.value.trim().toLowerCase();
    render();
    const input = document.querySelector("#search");
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
  const filterBindings = {
    "status-filter": "status",
    "origin-filter": "origin",
    "recurrence-filter": "recurrence",
    "interruptibility-filter": "interruptibility",
    "poolability-filter": "poolability",
    "compute-family-filter": "computeFamily",
    "compute-filter": "computeClass"
  };
  Object.entries(filterBindings).forEach(([id, key]) => document.querySelector(`#${id}`).addEventListener("change", event => {
    state[key] = event.target.value;
    render();
  }));
  document.querySelector("#picker-sort").addEventListener("change", event => {
    state.pickerSort = event.target.value;
    render();
  });
  const storageSearch = document.querySelector("#storage-search");
  storageSearch?.addEventListener("input", event => {
    state.storageQuery = event.target.value.trim().toLowerCase();
    render();
    const input = document.querySelector("#storage-search");
    input?.focus();
    input?.setSelectionRange(input.value.length, input.value.length);
  });
  const storageBindings = {
    "storage-safety": "storageSafety",
    "storage-persistence": "storagePersistence",
    "storage-locality": "storageLocality",
    "storage-poolability": "storagePoolability"
  };
  Object.entries(storageBindings).forEach(([id, key]) => document.querySelector(`#${id}`)?.addEventListener("change", event => {
    state[key] = event.target.value;
    render();
  }));
  document.querySelector("#clear-storage-filters")?.addEventListener("click", () => {
    state.storageQuery = "";
    state.storageSafety = "all";
    state.storagePersistence = "all";
    state.storageLocality = "all";
    state.storagePoolability = "all";
    render();
  });
  document.querySelector("#clear-filters").addEventListener("click", () => {
    state.query = "";
    state.status = "all";
    state.origin = "all";
    state.recurrence = "all";
    state.interruptibility = "all";
    state.poolability = "all";
    state.computeFamily = "all";
    state.computeClass = "all";
    render();
  });
  bindLiveAndArmControls();
}

class ApiRequestError extends Error {
  constructor(message, status = null, payload = null) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.payload = payload;
  }
}

async function apiRequest(path, { method = "GET", body } = {}) {
  if (!API_BASE) throw new ApiRequestError("Live controls require the loopback Free Compute app.");
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 7_000);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const planReasons = Array.isArray(payload?.plan?.reasons) ? payload.plan.reasons.join("; ") : null;
      const message = payload?.error?.message || payload?.reason || planReasons || `API returned HTTP ${response.status}`;
      throw new ApiRequestError(message, response.status, payload);
    }
    if (!payload || typeof payload !== "object") throw new ApiRequestError("API returned a non-JSON response.", response.status);
    return payload;
  } catch (error) {
    if (error instanceof ApiRequestError) throw error;
    if (error.name === "AbortError") throw new ApiRequestError("Live API request timed out.");
    throw new ApiRequestError("Loopback API could not be reached.");
  } finally {
    window.clearTimeout(timeout);
  }
}

function endpointUnavailable(error) {
  return error instanceof ApiRequestError && (error.status === null || [404, 405, 501].includes(error.status));
}

function replaceUsagePanel() {
  const panel = document.querySelector("#live-usage");
  if (!panel) return;
  panel.outerHTML = liveUsageSection();
  bindLiveControls();
}

function replaceOnboardingPanel() {
  const panel = document.querySelector("#onboarding");
  if (!panel) return;
  panel.outerHTML = onboardingSection();
  bindOnboardingControls();
}

function replaceArmPanel() {
  const panel = document.querySelector("#arm-control");
  if (!panel || !state.catalog) return;
  panel.outerHTML = armControlSection(state.catalog);
  bindArmControls();
}

async function refreshUsage({ force = false, manual = false } = {}) {
  if (state.live.loading || state.live.supported === false && !force) return;
  state.live.loading = true;
  replaceUsagePanel();
  try {
    const payload = await apiRequest(manual ? "/v1/usage/refresh" : "/v1/usage", manual ? { method: "POST", body: {} } : {});
    state.live.supported = true;
    state.live.data = payload;
    state.live.lastSuccessAt = Date.now();
    state.live.error = null;
  } catch (error) {
    if (state.live.supported !== true && endpointUnavailable(error)) {
      state.live.supported = false;
      state.live.error = null;
    } else {
      state.live.supported = error.status === 404 ? false : state.live.supported;
      state.live.error = error.message;
    }
  } finally {
    state.live.loading = false;
    replaceUsagePanel();
  }
}

async function refreshOnboarding({ force = false } = {}) {
  if (state.onboarding.loading || state.onboarding.submitting || state.onboarding.supported === false && !force) return;
  state.onboarding.loading = true;
  try {
    const payload = await apiRequest("/v1/onboarding");
    state.onboarding.supported = true;
    state.onboarding.data = payload;
    state.onboarding.error = null;
  } catch (error) {
    if (state.onboarding.supported !== true && endpointUnavailable(error)) {
      state.onboarding.supported = false;
      state.onboarding.error = null;
    } else {
      state.onboarding.error = error.message;
    }
  } finally {
    state.onboarding.loading = false;
    replaceOnboardingPanel();
  }
}

async function refreshArm({ force = false } = {}) {
  if (state.arm.loading || state.arm.submitting || state.arm.supported === false && !force) return;
  state.arm.loading = true;
  try {
    const payload = await apiRequest("/v1/arm");
    state.arm.supported = true;
    state.arm.data = payload;
    state.arm.error = null;
  } catch (error) {
    if (state.arm.supported !== true && endpointUnavailable(error)) {
      state.arm.supported = false;
      state.arm.error = null;
    } else {
      state.arm.supported = error.status === 404 ? false : state.arm.supported;
      state.arm.error = error.message;
    }
  } finally {
    state.arm.loading = false;
    replaceArmPanel();
  }
}

function retryLiveApi() {
  state.live.supported = null;
  state.live.error = null;
  state.onboarding.supported = null;
  state.onboarding.error = null;
  state.arm.supported = null;
  state.arm.error = null;
  void Promise.all([refreshUsage({ force: true }), refreshOnboarding({ force: true }), refreshArm({ force: true })]);
}

function startApiPolling() {
  if (state.apiPollTimer) window.clearInterval(state.apiPollTimer);
  void Promise.all([refreshUsage({ force: true }), refreshOnboarding({ force: true }), refreshArm({ force: true })]);
  state.apiPollTimer = window.setInterval(() => {
    if (document.hidden) return;
    void refreshUsage();
    void refreshOnboarding();
    void refreshArm();
  }, LIVE_POLL_MS);
}

function updateOnboardingMethodFields() {
  const profileId = document.querySelector("#onboarding-profile")?.value || "";
  const readiness = onboardingReadinessFor(profileId);
  const assisted = document.querySelector("#onboarding-assisted")?.checked === true;
  const sessionMethod = document.querySelector("#onboarding-session-method")?.value || "transient";
  const method = assisted ? sessionMethod : document.querySelector("#onboarding-method")?.value || "";
  const methodSelect = document.querySelector("#onboarding-method");
  const allowedMethods = assisted ? [sessionMethod] : onboardingAllowedMethods(profileId);
  const assistedWrap = document.querySelector("#onboarding-assisted-wrap");
  const assistedFields = document.querySelector("#onboarding-assisted-fields");
  const provenance = document.querySelector("#onboarding-provenance");
  const baseUrl = document.querySelector("#onboarding-base-url");
  const endpoint = document.querySelector("#onboarding-endpoint");
  const envRef = document.querySelector("#onboarding-env-ref");
  const envRefWrap = document.querySelector("#onboarding-env-ref-wrap");
  const canAssist = readiness?.missing_profile_definition === true;
  if (assistedWrap) assistedWrap.hidden = !canAssist;
  if (!canAssist && document.querySelector("#onboarding-assisted")) document.querySelector("#onboarding-assisted").checked = false;
  if (assistedFields) assistedFields.hidden = !assisted || !canAssist;
  if (baseUrl) baseUrl.required = assisted && canAssist;
  if (endpoint) endpoint.required = assisted && canAssist;
  if (envRefWrap) envRefWrap.hidden = !assisted || sessionMethod !== "env_ref";
  if (envRef) { envRef.required = assisted && sessionMethod === "env_ref"; if (!assisted || sessionMethod !== "env_ref") envRef.value = ""; }
  if (provenance) {
    provenance.disabled = assisted;
    if (assisted) provenance.value = "user_supplied";
  }
  if (methodSelect) {
    const retained = allowedMethods.includes(method) ? method : allowedMethods[0] || "";
    methodSelect.innerHTML = allowedMethods.length
      ? allowedMethods.map(item => `<option value="${esc(item)}" ${item === retained ? "selected" : ""}>${esc(onboardingMethodLabel(item))}</option>`).join("")
      : '<option value="">No supported connection method reported</option>';
    methodSelect.disabled = !allowedMethods.length;
  }
  const selectedMethod = methodSelect?.value || "";
  const valueWrap = document.querySelector("#onboarding-value-wrap");
  const referenceWrap = document.querySelector("#onboarding-reference-wrap");
  const value = document.querySelector("#onboarding-value");
  const reference = document.querySelector("#onboarding-reference");
  if (valueWrap) valueWrap.hidden = selectedMethod !== "transient";
  if (referenceWrap) referenceWrap.hidden = selectedMethod !== "reference";
  if (value) { value.required = selectedMethod === "transient"; if (selectedMethod !== "transient") value.value = ""; }
  if (reference) { reference.required = selectedMethod === "reference"; if (selectedMethod !== "reference") reference.value = ""; }
  const note = document.querySelector("#onboarding-method-note");
  if (note) note.textContent = assisted
    ? "Optional dispatch-only session. It remains in local process memory and does not create a meter, change policy eligibility, make the account routable without normal gates, or start work."
    : selectedMethod ? onboardingMethodNote(selectedMethod) : "This account capability has not reported a supported connection method.";
}

function bindOnboardingControls() {
  const form = document.querySelector("#onboarding-connect-form");
  if (!form) return;
  document.querySelector("#onboarding-profile")?.addEventListener("change", updateOnboardingMethodFields);
  document.querySelector("#onboarding-method")?.addEventListener("change", updateOnboardingMethodFields);
  document.querySelector("#onboarding-assisted")?.addEventListener("change", updateOnboardingMethodFields);
  document.querySelector("#onboarding-session-method")?.addEventListener("change", updateOnboardingMethodFields);
  document.querySelector("#onboarding-clear")?.addEventListener("click", () => void clearOnboardingConnection());
  form.addEventListener("submit", event => { event.preventDefault(); void submitOnboardingConnection(); });
  updateOnboardingMethodFields();
}

async function submitOnboardingConnection() {
  const value = id => document.querySelector(`#${id}`)?.value.trim() || "";
  const profileId = value("onboarding-profile");
  const assisted = document.querySelector("#onboarding-assisted")?.checked === true;
  const method = assisted ? value("onboarding-session-method") : value("onboarding-method");
  const provenance = value("onboarding-provenance");
  const secretInput = document.querySelector("#onboarding-value");
  const sessionValue = secretInput?.value || "";
  const reference = value("onboarding-reference");
  const baseUrl = value("onboarding-base-url");
  const endpoint = value("onboarding-endpoint") || "v1/chat/completions";
  const envRefInput = document.querySelector("#onboarding-env-ref");
  const envRef = envRefInput?.value || "";
  const consent = document.querySelector("#onboarding-consent")?.checked === true;
  const readiness = onboardingReadinessFor(profileId);
  const payload = { method, provenance, consent };
  if (assisted) {
    payload.account_id = readiness?.account_id;
    payload.adapter = "openai_compatible";
    payload.base_url = baseUrl;
    payload.endpoint = endpoint;
    if (method === "env_ref") payload.env_ref = envRef;
  } else if (readiness?.missing_profile_definition === true) payload.account_id = readiness.account_id;
  else payload.profile_id = profileId;
  if (method === "transient") payload.value = sessionValue;
  if (method === "reference") payload.reference = reference;
  if (!profileId || !method || !provenance || !consent || method === "transient" && !sessionValue || method === "reference" && !reference || assisted && (!readiness?.missing_profile_definition || !baseUrl || !endpoint || method === "env_ref" && !envRef)) {
    state.onboarding.error = "Choose an account, method, provenance, and consent; provide material only for the selected method.";
    replaceOnboardingPanel();
    return;
  }
  if (provenance === "agent_acquired" && method !== "reference") {
    state.onboarding.error = "Agent-acquired material may be recorded only as an explicitly consented opaque reference.";
    replaceOnboardingPanel();
    return;
  }
  try {
    state.onboarding.submitting = true;
    state.onboarding.error = null;
    state.onboarding.feedback = assisted ? "Creating a local session-only dispatch capability…" : "Saving only this account meter setup…";
    if (secretInput) secretInput.value = "";
    if (baseUrl) document.querySelector("#onboarding-base-url").value = "";
    if (envRefInput) envRefInput.value = "";
    const endpointInput = document.querySelector("#onboarding-endpoint");
    if (endpointInput) endpointInput.value = "";
    replaceOnboardingPanel();
    const result = await apiRequest("/v1/onboarding/connect", { method: "POST", body: payload });
    const sessionId = typeof result.profile_id === "string" ? result.profile_id : profileId;
    if (typeof result.credential_ref === "string") state.onboarding.credentialRefs.set(sessionId, result.credential_ref);
    if (method === "manual") {
      state.onboarding.feedback = "Manual setup recorded. Record a redacted meter observation next; this does not connect or make the account routable.";
      const accountSelect = document.querySelector("#observe-account");
      if (accountSelect && readiness?.account_id) accountSelect.value = readiness.account_id;
      document.querySelector("#live-usage")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (assisted) {
      state.onboarding.feedback = "Session-only dispatch capability created. It is not a usage monitor and remains blocked until normal fresh-meter, zero-liability, and explicit Arm gates pass.";
    } else {
      state.onboarding.feedback = "Setup recorded for this local session. Verify balance and zero liability separately before policy eligibility or current routability is possible.";
    }
    await refreshOnboarding({ force: true });
  } catch (error) {
    state.onboarding.error = error.message;
  } finally {
    state.onboarding.submitting = false;
    replaceOnboardingPanel();
  }
}

async function clearOnboardingConnection() {
  const profileId = document.querySelector("#onboarding-profile")?.value || "";
  const readiness = onboardingReadinessFor(profileId);
  const credentialRef = state.onboarding.credentialRefs.get(profileId);
  try {
    state.onboarding.submitting = true;
    state.onboarding.error = null;
    state.onboarding.feedback = "Clearing this session connection…";
    replaceOnboardingPanel();
    const payload = credentialRef ? { credential_ref: credentialRef } : readiness?.missing_profile_definition === true ? { account_id: readiness.account_id } : { profile_id: profileId };
    await apiRequest("/v1/onboarding/clear", { method: "POST", body: payload });
    state.onboarding.credentialRefs.delete(profileId);
    state.onboarding.feedback = "Session connection cleared. The catalog and other account capabilities remain available.";
    await refreshOnboarding({ force: true });
  } catch (error) {
    state.onboarding.error = error.message;
  } finally {
    state.onboarding.submitting = false;
    replaceOnboardingPanel();
  }
}

function bindLiveControls() {
  document.querySelector("#retry-live-api")?.addEventListener("click", retryLiveApi);
  document.querySelector("#refresh-usage")?.addEventListener("click", () => void refreshUsage({ force: true, manual: true }));
  document.querySelector("#observe-usage-form")?.addEventListener("submit", event => {
    event.preventDefault();
    void submitUsageObservation();
  });
}

async function submitUsageObservation() {
  const value = id => document.querySelector(`#${id}`)?.value.trim() || "";
  const payload = { account_id: value("observe-account"), source: "manual" };
  const numeric = {
    balance: "observe-balance",
    available_h100e: "observe-available-h100e",
    used_h100e: "observe-used-h100e",
    available_tpu_hours: "observe-available-tpu",
    used_tpu_hours: "observe-used-tpu",
    active_jobs: "observe-active-jobs",
    active_cost_per_hour: "observe-cost-hour"
  };
  Object.entries(numeric).forEach(([key, id]) => {
    const raw = value(id);
    if (raw !== "") payload[key] = Number(raw);
  });
  const balanceUnit = value("observe-balance-unit");
  if (balanceUnit) payload.balance_unit = balanceUnit;
  const meterId = value("observe-meter-id");
  const meterKind = value("observe-meter-kind");
  const meterValue = value("observe-meter-value");
  const meterAvailable = value("observe-meter-available");
  const meterUsed = value("observe-meter-used");
  const meterUnit = value("observe-meter-unit");
  const meterReset = value("observe-meter-reset");
  const meterExpires = value("observe-meter-expiry");
  if (meterId || meterKind || meterValue || meterAvailable || meterUsed || meterUnit || meterReset || meterExpires) {
    if (!meterId || !meterKind || !meterUnit || [meterValue, meterAvailable, meterUsed].every(entry => entry === "")) {
      state.live.observeFeedback = "Error: a generic meter needs an ID, kind, unit, and at least one of value, available, or used.";
      replaceUsagePanel();
      return;
    }
    const meter = { id: meterId, kind: meterKind, unit: meterUnit };
    if (meterValue !== "") meter.value = Number(meterValue);
    if (meterAvailable !== "") meter.available = Number(meterAvailable);
    if (meterUsed !== "") meter.used = Number(meterUsed);
    if (meterReset) meter.reset_at = new Date(meterReset).toISOString();
    if (meterExpires) meter.expires_at = new Date(meterExpires).toISOString();
    payload.meters = [meter];
  }
  const costUnit = value("observe-cost-unit");
  if (costUnit) payload.active_cost_unit = costUnit;
  const expiry = value("observe-expiry");
  if (expiry) payload.expires_at = new Date(expiry).toISOString();
  if (!payload.account_id) {
    state.live.observeFeedback = "Error: choose a catalog account.";
    replaceUsagePanel();
    return;
  }
  if (!Object.keys(payload).some(key => !["account_id", "source"].includes(key))) {
    state.live.observeFeedback = "Error: enter at least one meter value.";
    replaceUsagePanel();
    return;
  }
  try {
    state.live.observing = true;
    state.live.observeFeedback = "Recording observation…";
    replaceUsagePanel();
    const response = await apiRequest("/v1/usage/observe", { method: "POST", body: payload });
    state.live.supported = true;
    state.live.data = response;
    state.live.lastSuccessAt = Date.now();
    state.live.error = null;
    state.live.observeFeedback = "Observation recorded. It is monitoring evidence only and cannot authorize dispatch.";
  } catch (error) {
    state.live.observeFeedback = `Error: ${error.message}`;
  } finally {
    state.live.observing = false;
    replaceUsagePanel();
  }
}

function updateArmPoolCompatibility() {
  const element = document.querySelector("#arm-family-warning");
  if (!element || !state.catalog) return;
  const compatibility = armPoolCompatibility(state.catalog);
  element.classList.toggle("is-warning", compatibility.warning);
  element.innerHTML = `<strong>Pool compatibility:</strong> ${esc(compatibility.text)}`;
}

function bindArmControls() {
  document.querySelector("#retry-arm-api")?.addEventListener("click", retryLiveApi);
  document.querySelectorAll('input[name="arm-provider"]').forEach(input => input.addEventListener("change", event => {
    if (event.target.checked) state.armDraft.providers.add(event.target.value);
    else state.armDraft.providers.delete(event.target.value);
    updateArmPoolCompatibility();
  }));
  const armBindings = {
    "arm-storage": "storageId",
    "arm-duration": "durationMinutes",
    "arm-expiry": "expiresAt",
    "arm-max-jobs": "maxJobs",
    "arm-max-h100e": "maxH100e",
    "arm-balance-floor": "balanceFloor",
    "arm-idle": "idleMinutes",
    "arm-max-errors": "maxErrors"
  };
  Object.entries(armBindings).forEach(([id, key]) => {
    const input = document.querySelector(`#${id}`);
    input?.addEventListener("input", event => { state.armDraft[key] = event.target.value; });
    input?.addEventListener("change", event => { state.armDraft[key] = event.target.value; });
  });

  const autoBindings = {
    "auto-kind": "kind",
    "auto-compute-backend": "computeBackend",
    "auto-gpu-count": "gpuCount",
    "auto-min-vram": "minVram",
    "auto-preferred-vram": "preferredVram",
    "auto-interruptibility": "interruptibility",
    "auto-runtime": "runtimeMinutes",
    "auto-provider-count": "providerCount",
    "auto-storage-min": "storageMinGib",
    "auto-storage-persistence": "storagePersistence",
    "auto-storage-access": "storageAccess"
  };
  Object.entries(autoBindings).forEach(([id, key]) => {
    const input = document.querySelector(`#${id}`);
    input?.addEventListener("input", event => { state.autoDraft[key] = event.target.value; });
    input?.addEventListener("change", event => { state.autoDraft[key] = event.target.value; });
  });
  document.querySelector("#auto-storage-required")?.addEventListener("change", event => {
    state.autoDraft.storageRequired = event.target.checked;
    const options = document.querySelector("#auto-storage-options");
    if (options) options.hidden = !event.target.checked;
  });
  document.querySelector("#auto-blackwell-required")?.addEventListener("change", event => {
    state.autoDraft.blackwellRequired = event.target.checked;
  });
  document.querySelector("#arm-form")?.addEventListener("submit", event => {
    event.preventDefault();
    void submitArm();
  });
  document.querySelector("#disarm-submit")?.addEventListener("click", () => void submitDisarm());
  document.querySelector("#auto-arm-form")?.addEventListener("submit", event => {
    event.preventDefault();
    void submitAutoArm();
  });
}

function bindLiveAndArmControls() {
  bindOnboardingControls();
  bindLiveControls();
  bindArmControls();
}

function boundedNumber(raw, name, { min = 0, max = Number.MAX_SAFE_INTEGER, integer = false, optional = false } = {}) {
  if (String(raw).trim() === "") {
    if (optional) return null;
    throw new Error(`${name} is required.`);
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value < min || value > max || integer && !Number.isInteger(value)) {
    const shape = integer ? "whole number" : "number";
    throw new Error(`${name} must be a ${shape} from ${min} to ${max}.`);
  }
  return value;
}

function shutdownPayload() {
  const duration = boundedNumber(state.armDraft.durationMinutes, "Duration", { min: 1, max: 10_080, integer: true, optional: true });
  let expiresAt = null;
  if (state.armDraft.expiresAt) {
    const parsed = new Date(state.armDraft.expiresAt);
    if (Number.isNaN(parsed.getTime()) || parsed.getTime() <= Date.now()) throw new Error("Absolute expiry must be a future date and time.");
    expiresAt = parsed.toISOString();
  }
  if (duration === null && expiresAt === null) throw new Error("Set a duration or an absolute expiry.");
  const shutdown = {
    max_jobs: boundedNumber(state.armDraft.maxJobs, "Maximum jobs", { min: 1, max: 10_000, integer: true }),
    max_h100e: boundedNumber(state.armDraft.maxH100e, "Maximum H100e", { min: 0.01, max: 1_000_000 }),
    balance_floor: boundedNumber(state.armDraft.balanceFloor, "Balance floor", { min: 0, max: 1_000_000_000 }),
    idle_minutes: boundedNumber(state.armDraft.idleMinutes, "Idle shutoff", { min: 1, max: 10_080, integer: true }),
    max_errors: boundedNumber(state.armDraft.maxErrors, "Maximum errors", { min: 1, max: 1_000, integer: true })
  };
  if (duration !== null) shutdown.duration_minutes = duration;
  if (expiresAt !== null) shutdown.expires_at = expiresAt;
  return shutdown;
}

function armErrorMessage(error) {
  return error instanceof Error ? error.message : "Arm request failed.";
}

async function submitArm() {
  try {
    const providers = [...state.armDraft.providers];
    if (!providers.length) throw new Error("Select at least one safe provider.");
    const payload = {
      providers,
      storage_ids: state.armDraft.storageId ? [state.armDraft.storageId] : [],
      allow_credit_storage: false,
      shutdown: shutdownPayload()
    };
    state.arm.submitting = true;
    state.arm.error = null;
    state.arm.feedback = "Applying bounded routing policy...";
    replaceArmPanel();
    const response = await apiRequest("/v1/arm", { method: "POST", body: payload });
    state.arm.supported = true;
    state.arm.data = response.arm && typeof response.arm === "object" ? response.arm : response;
    state.arm.feedback = `Armed ${providers.length} provider${providers.length === 1 ? "" : "s"}; each job will use one.`;
    void refreshUsage({ force: true });
  } catch (error) {
    state.arm.error = armErrorMessage(error);
  } finally {
    state.arm.submitting = false;
    replaceArmPanel();
  }
}

async function submitDisarm() {
  try {
    state.arm.submitting = true;
    state.arm.error = null;
    state.arm.feedback = "Disarming...";
    replaceArmPanel();
    const response = await apiRequest("/v1/disarm", { method: "POST", body: { reason: "user_request" } });
    state.arm.supported = true;
    state.arm.data = response.arm && typeof response.arm === "object" ? response.arm : response;
    state.arm.feedback = "Routing is disarmed. No new jobs will start.";
    void refreshUsage({ force: true });
  } catch (error) {
    state.arm.error = armErrorMessage(error);
  } finally {
    state.arm.submitting = false;
    replaceArmPanel();
  }
}

function autoJobPayload() {
  const draft = state.autoDraft;
  if (!COMPUTE_BACKENDS.has(draft.computeBackend)) throw new Error("Compute backend is invalid.");
  if (draft.blackwellRequired && !["any", "cuda"].includes(draft.computeBackend)) throw new Error("Blackwell requires the CUDA backend or Any safe backend.");
  const gpuCount = boundedNumber(draft.gpuCount, "GPU count", { min: 0, max: 1, integer: true });
  const minVram = boundedNumber(draft.minVram, "Minimum VRAM", { min: 0, max: 4096 });
  const preferredVram = boundedNumber(draft.preferredVram, "Preferred VRAM", { min: 0, max: 4096 });
  const runtime = boundedNumber(draft.runtimeMinutes, "Runtime", { min: 1, max: 10_080, integer: true });
  if (preferredVram < minVram) throw new Error("Preferred VRAM cannot be lower than minimum VRAM.");
  const workloadTypes = {
    python: ["python"],
    notebook: ["python", "notebook"],
    openai_inference: ["inference"],
    data: ["data"],
    command: []
  }[draft.kind] || [];
  const idParts = ["ui-auto", draft.kind, draft.computeBackend, draft.blackwellRequired ? "blackwell" : "anyarch", `${gpuCount}g`, `${minVram}v`, `${preferredVram}p`, `${runtime}m`, draft.interruptibility];
  const job = {
    schema_version: 1,
    job_id: idParts.join("-").replace(/[^a-z0-9._-]/gi, "-").slice(0, 120),
    kind: draft.kind,
    mode: "plan",
    argv: [],
    inputs: [],
    outputs: [],
    workload_types: workloadTypes,
    resources: {
      gpu_count_min: gpuCount,
      vram_gb_min: minVram,
      vram_gb_preferred: preferredVram,
      max_runtime_minutes: runtime,
      interruptibility: draft.interruptibility,
      compute_backend: draft.computeBackend,
      blackwell_required: draft.blackwellRequired
    }
  };
  if (draft.storageRequired) {
    job.storage = {
      required: true,
      min_gib: boundedNumber(draft.storageMinGib, "Minimum storage", { min: 0.01, max: 10_000_000 }),
      persistence: draft.storagePersistence,
      access: draft.storageAccess ? [draft.storageAccess] : [],
      same_provider: false
    };
  }
  return job;
}

async function submitAutoArm() {
  try {
    const job = autoJobPayload();
    const request = {
      job,
      provider_count: boundedNumber(state.autoDraft.providerCount, "Routing pool size", { min: 1, max: 4, integer: true }),
      shutdown: shutdownPayload(),
      allow_credit_storage: false
    };
    state.arm.submitting = true;
    state.arm.error = null;
    state.arm.feedback = "Ranking safe providers and storage...";
    replaceArmPanel();
    const response = await apiRequest("/v1/arm/auto", { method: "POST", body: request });
    state.arm.supported = true;
    state.arm.data = response.arm && typeof response.arm === "object" ? response.arm : response;
    const selected = response.plan?.selected?.account_id || response.plan?.selected?.provider || "the top safe candidate";
    const storage = response.plan?.selected?.storage?.id || response.plan?.selected?.storage_id;
    const selectedCompute = response.plan?.selected?.compute;
    const backendText = Array.isArray(selectedCompute?.backends) ? selectedCompute.backends.join("/") : null;
    const computeText = backendText ? ` on ${backendText}${selectedCompute.blackwell === true ? " Blackwell" : ""}` : "";
    state.arm.feedback = `Auto-arm selected ${selected}${computeText}${storage ? ` with ${storage}` : ""}. Each job remains single-provider.`;
    void refreshUsage({ force: true });
  } catch (error) {
    state.arm.error = armErrorMessage(error);
  } finally {
    state.arm.submitting = false;
    replaceArmPanel();
  }
}

async function loadCatalog() {
  try {
    let catalog = null;
    let loadedSource = null;
    if (API_BASE) {
      const ledger = await apiRequest("/v1/ledger");
      catalog = ledger.catalog;
      loadedSource = "/v1/ledger";
      if (!catalog || typeof catalog !== "object") throw new Error("ledger did not include a catalog");
    } else {
      const response = await fetch("data/catalog.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`data/catalog.json: HTTP ${response.status}`);
      catalog = await response.json();
      loadedSource = "data/catalog.json";
    }
    catalog.blockers = Array.isArray(catalog.blockers) ? catalog.blockers : [];
    catalog.normalization = catalog.normalization && typeof catalog.normalization === "object" ? catalog.normalization : {};
    state.warnings = validateCatalog(catalog);
    state.catalog = catalog;
    state.catalogSource = loadedSource;
    render();
    startApiPolling();
  } catch (error) {
    app.innerHTML = `
      <section class="load-error" role="alert">
        <p class="eyebrow">Free Compute unavailable</p>
        <h1>Could not load the compute catalog</h1>
        <p>${esc(error.message)}</p>
        <p>Run <code>.\\start_app.ps1</code> from the project folder instead of opening index.html directly.</p>
        <button type="button" id="retry">Retry</button>
      </section>`;
    document.querySelector("#retry").addEventListener("click", loadCatalog);
  }
}

loadCatalog();
