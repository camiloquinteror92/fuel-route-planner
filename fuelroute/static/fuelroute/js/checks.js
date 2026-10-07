// Arithmetic on a /api/route answer, recomputed in the browser. Pure functions (no DOM).
// Money is added in integer cents, so a sum is exact to the cent.

export const cents = (value) => Math.round(Number(value) * 100);

// The sum of amounts in dollars, as integer cents.
export function sumCents(values) {
  return values.reduce((total, value) => total + cents(value), 0);
}

// The longest drive without buying fuel: from the start, between stops, to the finish.
export function longestStretch(body) {
  const miles = [0, ...(body.fuel_stops || []).map((s) => s.mile_marker), body.route.distance_miles];
  let longest = 0;
  for (let i = 1; i < miles.length; i++) longest = Math.max(longest, miles[i] - miles[i - 1]);
  return longest;
}
