/**
 * Computes a minimal PUT /config patch: only changed branches are sent, so a
 * save never rewrites unrelated settings (TZ §11 "применяются без перезапуска").
 */
import type { Config, ConfigPatch } from './types';

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function deepEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (typeof left !== typeof right) return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)) return false;
    if (left.length !== right.length) return false;
    return left.every((item, index) => deepEqual(item, right[index]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) return false;
    return leftKeys.every(
      (key) => Object.prototype.hasOwnProperty.call(right, key) && deepEqual(left[key], right[key]),
    );
  }
  return false;
}

function diffObject(
  original: Record<string, unknown>,
  draft: Record<string, unknown>,
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  for (const key of Object.keys(draft)) {
    const before = original[key];
    const after = draft[key];
    if (deepEqual(before, after)) continue;
    if (isPlainObject(before) && isPlainObject(after)) {
      const nested = diffObject(before, after);
      if (Object.keys(nested).length > 0) patch[key] = nested;
    } else {
      patch[key] = after;
    }
  }
  return patch;
}

export function diffConfig(original: Config, draft: Config): ConfigPatch {
  const patch = diffObject(
    original as unknown as Record<string, unknown>,
    draft as unknown as Record<string, unknown>,
  );
  return patch as ConfigPatch;
}

export function hasChanges(patch: ConfigPatch): boolean {
  return Object.keys(patch).length > 0;
}
