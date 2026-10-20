// User-defined boundaries only. A boundary is the inclusive start of Z2..Z5.
export function validBoundaries(values) {
  return Array.isArray(values) && values.length === 4 && values.every((v,i)=>
    Number.isInteger(v) && v > 0 && v <= 65535 && (i === 0 || v > values[i-1]));
}
export function zoneFor(bpm, freshness, boundaries) {
  if (freshness !== 'LIVE' || !Number.isFinite(bpm) || bpm <= 0 || !validBoundaries(boundaries)) return null;
  return 1 + boundaries.filter(boundary=>bpm >= boundary).length;
}
export function zoneRange(zone, boundaries) {
  if (!validBoundaries(boundaries) || !Number.isInteger(zone) || zone < 1 || zone > 5) return '';
  if (zone === 1) return `Unter ${boundaries[0]} bpm`;
  if (zone === 5) return `Ab ${boundaries[3]} bpm`;
  return `${boundaries[zone-2]}–${boundaries[zone-1]-1} bpm`;
}
