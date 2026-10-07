function toHex(value) {
  return (value >>> 0).toString(16).padStart(8, "0");
}

function hashText(value) {
  let first = 2166136261;
  let second = 2654435769;

  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    first = Math.imul(first ^ code, 16777619);
    second = Math.imul(second ^ (code + index), 2246822519);
  }

  return `${toHex(first)}${toHex(second)}`;
}

export function buildStableContextKey(prefix, value) {
  return `${prefix}-${hashText(String(value ?? ""))}`;
}
