import { accentColors, skinColors, validAvatar, avatarFromSeed, type AvatarConfig } from '../lib/avatarRecipe'

/** Shared vector parts: no bitmap, font, external request or per-face file. */
export default function AvatarFace({ value, size = 48 }: { value: AvatarConfig; size?: number }) {
  const v = validAvatar(value) ? value : avatarFromSeed(0), ink = '#28313d', skin = skinColors[v.skin], accent = accentColors[v.accent], faceInk = v.skin === 2 || v.skin === 3 ? '#f6e9d8' : ink, paper = v.skin === 2 || v.skin === 3 ? ink : 'white'
  const heads = [
    <rect x="15" y="19" width="50" height="51" rx="19"/>, <ellipse cx="40" cy="45" rx="26" ry="26"/>,
    <path d="M22 21h36l9 17-5 25-22 10-22-10-5-25z"/>, <rect x="16" y="21" width="48" height="48" rx="9"/>,
    <path d="M18 25Q40 9 62 25v28Q60 73 40 73T18 53z"/>, <path d="M15 35Q15 17 40 17t25 18l-5 27-20 10-20-10z"/>,
    <rect x="13" y="24" width="54" height="44" rx="20"/>, <path d="M18 20h44v36Q60 72 40 72T18 56z"/>,
    <path d="M21 22Q40 13 59 22l9 28-12 20H24L12 50z"/>, <ellipse cx="40" cy="45" rx="22" ry="28"/>,
  ]
  const hairs = [
    <path d="M16 32Q13 12 37 14l8-7 4 10q18-1 17 20l-9-11-14 4-15-5z"/>,
    <path d="M15 34V22l12-8 10 7 12-9 15 12v13l-13-9-12 5-13-6z"/>,
    <path d="M15 32q-2-19 25-19t25 19H52l-6-9-6 9-6-9-6 9z"/>,
    <path d="M15 39Q8 14 40 14q27 0 26 23L54 22q-9 20-30 17l7-15z"/>,
    <path d="M17 29V19q23-15 46 0v10zM22 20l-4-8m40 8 4-8"/>,
    <g><rect x="15" y="16" width="50" height="15" rx="7"/><path d="M22 17V9h36v8"/></g>,
    <g><path d="M14 30q1-17 26-17t26 17z"/><path d="M40 13V5"/><circle cx="40" cy="5" r="4"/></g>,
    <g><path d="M18 28Q40 10 62 28v6H18z"/><path d="m25 20-7-11 15 5m22 6 7-11-15 5"/></g>,
    <path d="M14 30q5-14 19-14h14q15 1 19 14l-12-4-9 9-9-10-13 10z"/>,
    <g><rect x="18" y="17" width="44" height="14" rx="5"/><path d="M25 17v-5m15 5V9m15 8v-5"/></g>,
    <g><path d="M16 33Q12 15 40 15t24 18l-13-9-10 4-12-5z"/><circle cx="12" cy="23" r="7"/><circle cx="68" cy="23" r="7"/></g>,
    <g><path d="M18 27q22-15 44 0v7H18z"/><path d="m23 22 7-12 9 7 10-9 9 15"/></g>,
  ]
  const eyes = [
    <g><ellipse cx="29" cy="43" rx="3" ry="4"/><ellipse cx="51" cy="43" rx="3" ry="4"/></g>,
    <g><path d="M25 43q4-7 8 0m14 0q4-7 8 0" fill="none"/></g>,
    <g><rect x="24" y="39" width="9" height="7" rx="2"/><rect x="47" y="39" width="9" height="7" rx="2"/></g>,
    <g><circle cx="29" cy="43" r="5" fill={paper}/><circle cx="51" cy="43" r="5" fill={paper}/><circle cx="30" cy="43" r="2"/><circle cx="50" cy="43" r="2"/></g>,
    <g><path d="m24 42 9 1m14 0 9-1"/><circle cx="29" cy="44" r="2"/><circle cx="51" cy="44" r="2"/></g>,
    <g><path d="M24 43h9m14 0h9" fill="none"/></g>,
    <g><path d="M25 43q4-7 8 0" fill="none"/><circle cx="51" cy="43" r="4"/></g>,
    <g><rect x="21" y="37" width="16" height="12" rx="5" fill={paper}/><rect x="43" y="37" width="16" height="12" rx="5" fill={paper}/><path d="M37 41h6"/><circle cx="29" cy="43" r="2"/><circle cx="51" cy="43" r="2"/></g>,
    <g><path d="m24 39 9 1m14 0 9-1"/><ellipse cx="29" cy="44" rx="3" ry="2"/><ellipse cx="51" cy="44" rx="3" ry="2"/></g>,
    <g><rect x="24" y="39" width="10" height="10" rx="5"/><rect x="46" y="39" width="10" height="10" rx="5"/><path d="M27 41h1m21 0h1" stroke="white"/></g>,
    <g><path d="m25 40 7 3-7 3m30-6-7 3 7 3" fill="none"/></g>,
    <g><path d="M23 38h13v9H23zm21 0h13v9H44z" fill={paper}/><path d="M36 41h8"/><path d="M28 41v3m23-3v3"/></g>,
  ]
  const noses = [null, <path d="m40 48-3 6h5" fill="none"/>, <path d="m40 48 3 6h-6z"/>, <circle cx="40" cy="53" r="2"/>, <path d="M38 51q2 5 4 0" fill="none"/>, <path d="M40 49v5"/>, <ellipse cx="40" cy="53" rx="3" ry="2" fill={accent}/>, <path d="m38 50 4 4m-4 0 4-4"/>]
  const mouths = [
    <path d="M33 60q7 6 14 0" fill="none"/>, <path d="M34 62h12"/>, <ellipse cx="40" cy="62" rx="3" ry="4"/>,
    <path d="M33 59h14q-1 9-7 9t-7-9" fill={paper}/>, <path d="m33 63 6-2 8 1" fill="none"/>,
    <path d="M34 62q3 4 6 0 3 4 6 0" fill="none"/>, <path d="M34 63q6-4 12 0" fill="none"/>,
    <g><rect x="32" y="59" width="16" height="6" rx="3" fill={paper}/><path d="M37 60v4m6-4v4"/></g>,
    <path d="m34 60 5 4 7-5" fill="none"/>, <path d="M34 62h12m-12 0 2-3m10 3-2-3" fill="none"/>,
    <path d="M34 60q6 3 12 0" fill="none"/>, <g><circle cx="37" cy="62" r="1"/><circle cx="43" cy="62" r="1"/></g>,
  ]
  return <svg viewBox="0 0 80 80" width={size} height={size} aria-hidden="true" focusable="false" style={{display:'block',maxWidth:'100%',height:'auto'}}>
    <g stroke={ink} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round">
      <rect x="9" y="38" width="9" height="16" rx="4" fill={skin}/><rect x="62" y="38" width="9" height="16" rx="4" fill={skin}/>
      <g fill={skin}>{heads[v.head]}</g><g fill={accent}>{hairs[v.hair]}</g><g fill={faceInk} stroke={faceInk} strokeWidth="2.1">{eyes[v.eyes]}{noses[v.nose]}{mouths[v.mouth]}</g>
    </g>
  </svg>
}
