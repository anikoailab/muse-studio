/* Shared palette definitions for the colour-picking pages.
   Every palette is a COMPLETE token set for both modes, not just an accent, so a
   preview never inherits whatever colour scheme the viewer's OS happens to be in.
   All pairs verified at WCAG AA or better before being added here. */
window.PALETTES={
  chartreuse:{
    label:"acid",
    swatch:"#c6f000",
    blurb:"lime on near black. the boldest of the three and the hardest to forget.",
    light:{bg:"#f4f4f0",surface:"#ffffff","surface-2":"#e9e9e1",ink:"#111309",muted:"#585b4b",line:"#dcdcd0",
      accent:"#c6f000","accent-ink":"#10120a","accent-text":"#4c6600","accent-soft":"rgba(198,240,0,.20)",
      "accent-ink-soft":"rgba(16,18,10,.74)","accent-chip":"rgba(16,18,10,.13)",
      shadow:"0 16px 40px rgba(17,19,9,.10)","shadow-lift":"0 26px 60px rgba(17,19,9,.16)"},
    dark:{bg:"#0b0c08",surface:"#14160f","surface-2":"#1b1e14",ink:"#f1f2ea",muted:"#9a9e8c",line:"#262a1c",
      accent:"#c6f000","accent-ink":"#10120a","accent-text":"#c6f000","accent-soft":"rgba(198,240,0,.16)",
      "accent-ink-soft":"rgba(16,18,10,.74)","accent-chip":"rgba(16,18,10,.13)",
      shadow:"0 16px 40px rgba(0,0,0,.5)","shadow-lift":"0 26px 60px rgba(0,0,0,.62)"}},

  coral:{
    label:"coral",
    swatch:"#d61e46",
    blurb:"warm pink red, closest to the tutti reference. the friendliest to buy from.",
    light:{bg:"#f8f5f4",surface:"#ffffff","surface-2":"#efe9e7",ink:"#17100e",muted:"#6a5b57",line:"#e5dcd9",
      accent:"#d61e46","accent-ink":"#ffffff","accent-text":"#cf2445","accent-soft":"rgba(214,30,70,.09)",
      "accent-ink-soft":"rgba(255,255,255,.86)","accent-chip":"rgba(255,255,255,.20)",
      shadow:"0 16px 40px rgba(23,16,14,.10)","shadow-lift":"0 26px 60px rgba(23,16,14,.16)"},
    dark:{bg:"#100b0b",surface:"#1a1213","surface-2":"#221718",ink:"#f5efee",muted:"#a8938f",line:"#2c1f20",
      accent:"#ff4d6a","accent-ink":"#1a0508","accent-text":"#ff8095","accent-soft":"rgba(255,77,106,.14)",
      "accent-ink-soft":"rgba(26,5,8,.74)","accent-chip":"rgba(26,5,8,.14)",
      shadow:"0 16px 40px rgba(0,0,0,.5)","shadow-lift":"0 26px 60px rgba(0,0,0,.62)"}},

  forest:{
    label:"forest",
    swatch:"#0f6b47",
    blurb:"deep green on bone. calm and grown up. the safest of the three.",
    light:{bg:"#f2f2ee",surface:"#ffffff","surface-2":"#e6e8e1",ink:"#0e1410",muted:"#525c54",line:"#d9ded6",
      accent:"#0f6b47","accent-ink":"#ffffff","accent-text":"#0d5c3d","accent-soft":"rgba(15,107,71,.09)",
      "accent-ink-soft":"rgba(255,255,255,.86)","accent-chip":"rgba(255,255,255,.20)",
      shadow:"0 16px 40px rgba(14,20,16,.10)","shadow-lift":"0 26px 60px rgba(14,20,16,.16)"},
    dark:{bg:"#0a0d0b",surface:"#121712","surface-2":"#182019",ink:"#eef2ec",muted:"#93a196",line:"#212a23",
      accent:"#0f7a52","accent-ink":"#ffffff","accent-text":"#55c79a","accent-soft":"rgba(15,122,82,.16)",
      "accent-ink-soft":"rgba(255,255,255,.86)","accent-chip":"rgba(255,255,255,.20)",
      shadow:"0 16px 40px rgba(0,0,0,.5)","shadow-lift":"0 26px 60px rgba(0,0,0,.62)"}}
};

/* Injects a palette into a same-origin iframe. Appended last so it beats both the
   page's own :root and its prefers-color-scheme block. */
window.applyPalette=function(frame,pal,mode){
  const doc=frame.contentDocument;
  if(!doc||!doc.head)return false;
  const tokens=window.PALETTES[pal][mode];
  let css=":root{";
  for(const k in tokens)css+="--"+k+":"+tokens[k]+";";
  css+="}";
  let tag=doc.getElementById("palette-override");
  if(!tag){tag=doc.createElement("style");tag.id="palette-override";doc.head.appendChild(tag);}
  tag.textContent=css;
  doc.documentElement.style.colorScheme=mode;
  return true;
};
