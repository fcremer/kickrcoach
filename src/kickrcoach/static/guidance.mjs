// Display guidance only. This module never issues trainer commands.
export function goalFeedback(value, minimum, maximum, active, live) {
  if(!Number.isFinite(minimum)||!Number.isFinite(maximum)||minimum>maximum) return 'unconfigured';
  if(!live||!Number.isFinite(value)) return 'unavailable';
  if(!active) return 'inactive';
  return value<minimum?'below':value>maximum?'above':'within';
}
