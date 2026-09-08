/** Shared mood colouring: one hue scale used by the orb, the sparkline and the log. */

export type RGB = [number, number, number];

const CALM: RGB = [96, 200, 255];      // low energy, pleasant
const BRIGHT: RGB = [255, 196, 92];    // high energy, pleasant
const LOW: RGB = [110, 132, 190];      // low energy, unpleasant
const HOT: RGB = [255, 110, 132];      // high energy, unpleasant

function mix(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

/** Bilinear blend across the valence/arousal plane. */
export function moodRGB(valence: number, arousal: number): RGB {
  const v = Math.max(-1, Math.min(1, valence));
  const a = Math.max(0, Math.min(1, arousal));
  const pleasant = mix(CALM, BRIGHT, a);
  const unpleasant = mix(LOW, HOT, a);
  return mix(unpleasant, pleasant, (v + 1) / 2).map(Math.round) as RGB;
}

export function moodColor(valence: number, arousal: number, alpha = 1): string {
  const [r, g, b] = moodRGB(valence, arousal);
  return alpha >= 1 ? `rgb(${r},${g},${b})` : `rgba(${r},${g},${b},${alpha})`;
}
