// Главная's shared duration helpers (#340 unit 3b review-fix): the collector's durations as Russian text, one rule for every
// Главная panel that imports them. The agents panel (home-agents.js) keeps its own copy until its owner switches to this file.
// Plain ES module, no DOM at import.

// "86 s", "1.9 min", "2 h 08 min" -> seconds (the collector's ages), for ordering and for the Russian form.
export function ageS(v) {
  let n = 0;
  for (const m of String(v ?? "").matchAll(/(\d+(?:\.\d+)?)\s*(s|min|h|d)\b/g)) {
    n += Number(m[1]) * { s: 1, min: 60, h: 3600, d: 86400 }[m[2]];
  }
  return n;
}

// Seconds -> seconds under 90, then minutes, then hours and minutes past 90 min (a whole hour reads «2 ч»).
export function dur(s) {
  const v = Math.max(0, Math.round(s));
  if (v < 90) return `${v} с`;
  const m = Math.round(v / 60);
  return m < 90 ? `${m} мин` : m % 60 === 0 ? `${m / 60} ч` : `${Math.floor(m / 60)} ч ${m % 60} мин`;
}

// A collector time ("10 min", "2 h 08 min") in the Russian form; a string with no time in it stays as it is.
export function ru(v) {
  const t = String(v ?? "");
  return /\d\s*(s|min|h|d)\b/.test(t) ? dur(ageS(t)) : t;
}
