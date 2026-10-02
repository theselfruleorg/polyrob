/**
 * format.js — the ONE USD formatter the console uses (FE11, 2026-10-03 audit).
 *
 * Lifted out of money.js so the transcript's cost line and the token rows
 * share it: a sub-cent value is written out in full, never rounded to a
 * confident `$0.00` — the two-decimal format renders every tiny cost as the
 * one number that means "free".
 */

/** A USD figure a person reads. `null` is the caller's to dash. */
export function fmtUsd(n) {
  if (n === null || n === undefined || !Number.isFinite(n)) return null;
  const neg = n < 0;
  const a = Math.abs(n);
  let body;
  if (a !== 0 && a < 0.01) {
    body = a.toFixed(12).replace(/0+$/, "").replace(/\.$/, "");
  } else {
    body = a.toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }
  return (neg ? "-$" : "$") + body;
}

/** A SIGNED USD change ("+$12.34" / "-$7.00"), for a P&L a person reads. */
export function fmtSignedUsd(n) {
  const f = fmtUsd(n);
  if (f === null) return null;
  return n > 0 ? `+${f}` : f;
}
