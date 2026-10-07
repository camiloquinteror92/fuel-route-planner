// Arithmetic on a /api/route answer, recomputed in the browser. Pure functions (no DOM).
// Money is added in integer cents, so a sum is exact to the cent.

export const cents = (value) => Math.round(Number(value) * 100);

// The sum of amounts in dollars, as integer cents.
export function sumCents(values) {
  return values.reduce((total, value) => total + cents(value), 0);
}

// Fuel in the tank along the route, from the answer itself:
// [{mile, fuel, kind: 'start' | 'arrive' | 'depart' | 'end', stop}], plus min,
// max and the longest stretch between purchases. Each arrival is the API's own
// fuel_on_arrival_gallons (and the end its end_fuel_gallons), so the rounding of the
// mile markers does not pile up over a long trip.
export function tankTrace({ distance, mpg, startFuel, stops, endFuel = null }) {
  const points = [{ mile: 0, fuel: startFuel, kind: 'start', stop: null }];
  let fuel = startFuel;
  let prev = 0;
  for (const s of stops) {
    fuel -= (s.mile_marker - prev) / mpg;
    if (Number.isFinite(s.fuel_on_arrival_gallons)) fuel = s.fuel_on_arrival_gallons;
    points.push({ mile: s.mile_marker, fuel, kind: 'arrive', stop: s.stop ?? null });
    fuel += s.gallons;
    points.push({ mile: s.mile_marker, fuel, kind: 'depart', stop: s.stop ?? null });
    prev = s.mile_marker;
  }
  fuel -= (distance - prev) / mpg;
  if (Number.isFinite(endFuel)) fuel = endFuel;
  points.push({ mile: distance, fuel, kind: 'end', stop: null });
  const levels = points.map((p) => p.fuel);
  return { points, min: Math.min(...levels), max: Math.max(...levels), end: fuel };
}

// Fuel at a given mile (linear between the points of a trace).
export function fuelAt(trace, mile) {
  const pts = trace.points;
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    if (a.mile === b.mile) continue;
    if (mile >= a.mile && mile <= b.mile) return a.fuel + ((b.fuel - a.fuel) * (mile - a.mile)) / (b.mile - a.mile);
  }
  return pts.length ? pts[pts.length - 1].fuel : null;
}

// The plan's tank, with the API's own levels.
export function planTrace(body) {
  return tankTrace({
    distance: body.route.distance_miles,
    mpg: body.vehicle.miles_per_gallon,
    startFuel: body.summary.start_fuel_gallons,
    endFuel: body.summary.end_fuel_gallons,
    stops: body.fuel_stops || [],
  });
}

// The longest drive without buying fuel: from the start, between stops, to the finish.
export function longestStretch(body) {
  const miles = [0, ...(body.fuel_stops || []).map((s) => s.mile_marker), body.route.distance_miles];
  let longest = 0;
  for (let i = 1; i < miles.length; i++) longest = Math.max(longest, miles[i] - miles[i - 1]);
  return longest;
}
