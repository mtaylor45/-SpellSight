"""Regenerate docs/images/spellbook.png — the wand-movement reference chart.

Spell names, effects and suggested automations come from wandportal.spells and
config.yaml, so the chart cannot drift from the code. Glyph paths are drawn in a
0..100 box and an SVG marker supplies the arrowhead, so direction of travel is
always visible — which matters, because the recognizer deliberately has rotation
normalization switched off and an up-stroke is a different spell to a down-stroke.

    python tools/make_spellbook.py

Needs playwright and a chromium build, and fetches two webfonts. Deliberately
not in requirements.txt or requirements-dev.txt: nothing on the device needs it,
and it should not be dragged in just to run the regression suites. Set
CHROME_PATH if your chromium lives somewhere unusual.
"""
import os
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from wandportal.spells import BY_ID  # noqa: E402

OUT = os.path.join(ROOT, "docs", "images", "spellbook.png")
ENABLED = set(yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))["spells"])

# id -> (path in a 0..100 box, proposed?)
# "proposed" means no canonical wand movement exists; these are designed as the
# direction-reversed partner of their opposite, which separates well precisely
# because rotation normalization is off.
GLYPHS = {
 "lumos":              ("M32,67 A13,13 0 1,1 32,41 A13,13 0 0,1 32,67 L82,38", False),
 "nox":                ("M82,34 L36,58 A13,13 0 1,0 36,32 A13,13 0 0,0 36,58", True),
 "alohomora":          ("M50,26 A15,15 0 1,1 49.6,26 L50,86", False),
 "colloportus":        ("M50,86 L50,41 A15,15 0 1,0 49.6,41", True),
 "incendio":           ("M22,26 L78,26 L24,72 L80,72", False),
 "accio":              ("M16,74 C20,24 80,24 84,72", False),
 "silencio":           ("M24,30 C26,74 56,82 66,54 L78,26", False),
 "revelio":            ("M28,82 L28,34 C28,20 64,20 64,42 C64,58 38,58 31,55 L74,82", False),
 "wingardium_leviosa": ("M16,58 C30,36 50,36 60,56 C64,64 70,62 72,52 L80,24", False),
 "expelliarmus":       ("M20,30 L72,30 L72,78", False),
 "protego":            ("M50,86 L50,20", False),
 "stupefy":            ("M50,18 L50,84", False),
 "petrificus_totalus": ("M16,50 L84,50", False),
 "engorgio":           ("M22,24 L50,76 L78,24", False),
 "reducio":            ("M22,76 L50,24 L78,76", False),
 "reducto":            ("M28,24 L76,50 L28,76", False),
 "reparo":             ("M30,30 L70,30 L70,70 L30,70 L30,34", False),
 "scourgify":          ("M72,32 C72,18 32,18 32,40 C32,58 70,54 70,72 C70,88 30,86 28,70", False),
 "expecto_patronum":   ("M56,52 C56,44 44,44 44,54 C44,66 62,68 66,52 C72,32 46,22 33,40 C19,60 38,86 64,80", False),
 "diffindo":           ("M24,78 L24,28 L64,76 L64,24", False),
 "finite_incantatem":  ("M34,22 L34,62 C34,78 60,80 68,64", False),
 "aguamenti":          ("M14,56 C26,20 40,88 54,48 C63,22 75,70 87,38", False),
 "locomotor":          ("M64,16 L24,60 L80,60", False, "M64,16 L64,86"),
 "tarantallegra":      ("M20,32 C20,66 40,68 42,36 C44,66 64,68 66,36 L78,20", False),
}

ORDER = [s for s in GLYPHS if s in ENABLED] + [s for s in GLYPHS if s not in ENABLED]

def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

cells = []
for sid in ORDER:
    spell = BY_ID[sid]
    entry = GLYPHS[sid]
    path, proposed = entry[0], entry[1]
    extra = entry[2] if len(entry) > 2 else None
    enabled = sid in ENABLED
    mark = ('<span class="mark on" title="enabled in config.yaml">&#9733;</span>' if enabled else "")
    prop = ('<span class="mark prop" title="no canonical movement; proposed">&#9671;</span>' if proposed else "")
    cells.append(f"""
    <div class="cell{' lit' if enabled else ''}">
      <div class="medal">
        <svg viewBox="-6 -6 112 112" aria-label="wand movement for {esc(spell.name)}">
          <path d="{path}" class="glyph" marker-end="url(#tip)" marker-start="url(#dot)"/>{f'<path d="{extra}" class="glyph"/>' if extra else ""}
        </svg>
      </div>
      <div class="txt">
        <h2>{esc(spell.name)}{mark}{prop}</h2>
        <p class="eff">{esc(spell.effect)}.</p>
        <p class="auto">{esc(spell.suggested)}</p>
      </div>
    </div>""")

html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>SpellSight Grimoire</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;600;700&family=EB+Garamond:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html {{ background:#07070c; }}
  body {{ width:1700px; background:#07070c; color:#ece5d2;
         font-family:'EB Garamond',Georgia,'Liberation Serif',serif; position:relative; }}
  .frame {{ position:absolute; top:26px; left:26px; right:26px; bottom:26px; border:2px solid #8a6d2f;
            box-shadow:inset 0 0 0 1px rgba(212,175,55,.25), inset 0 0 140px rgba(0,0,0,.9); }}
  .frame::after {{ content:""; position:absolute; inset:9px; border:1px solid rgba(212,175,55,.4); }}
  .bg {{ position:absolute; inset:0;
         background:
           radial-gradient(circle at 50% 12%, rgba(76,64,120,.30), transparent 55%),
           radial-gradient(circle at 50% 100%, rgba(150,110,40,.16), transparent 60%),
           linear-gradient(#0a0a12 0%, #10101a 45%, #0b0a10 100%); }}
  .wrap {{ position:relative; padding:92px 92px 84px; }}
  header {{ text-align:center; }}
  .kicker {{ font-family:'Cinzel',serif; font-size:20px; letter-spacing:.62em;
             color:#b9903f; text-transform:uppercase; margin-bottom:26px; }}
  h1 {{ font-family:'Cinzel',serif; font-size:104px; font-weight:700; letter-spacing:.05em;
        background:linear-gradient(#fbf0c4,#e2bf62 42%,#a9793p 70%,#e9cf86);
        background:linear-gradient(#fbf0c4,#e6c76f 45%,#a87c2c 72%,#eed695);
        -webkit-background-clip:text; background-clip:text; color:transparent;
        text-shadow:0 0 46px rgba(214,170,74,.22); line-height:1; }}
  .sub {{ font-family:'Cinzel',serif; font-size:29px; letter-spacing:.42em; color:#cbbb8d;
          margin-top:20px; text-transform:uppercase; }}
  .rule {{ display:flex; align-items:center; justify-content:center; gap:20px; margin:38px 0 30px; }}
  .rule i {{ height:1px; width:290px; background:linear-gradient(90deg,transparent,#9b7a34,transparent); }}
  .rule b {{ color:#9b7a34; font-size:19px; }}
  .scroll {{ max-width:1130px; margin:0 auto 62px; text-align:center; font-size:25.5px;
             line-height:1.62; color:#c9c0a6; font-style:italic; }}
  .scroll em {{ color:#e0c579; font-style:italic; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:26px 46px; }}
  .cell {{ display:flex; align-items:center; gap:26px; padding:17px 22px;
           border:1px solid rgba(138,109,47,.22); border-radius:5px;
           background:linear-gradient(100deg, rgba(28,25,40,.62), rgba(16,15,24,.30)); }}
  .cell.lit {{ border-color:rgba(212,175,55,.46);
               background:linear-gradient(100deg, rgba(48,39,26,.66), rgba(20,17,26,.34)); }}
  .medal {{ flex:0 0 118px; height:118px; border-radius:50%;
            border:1px solid rgba(212,175,55,.42);
            background:radial-gradient(circle at 38% 30%, #1d1a2a, #0a0910 74%);
            box-shadow:0 0 26px rgba(0,0,0,.6), inset 0 0 20px rgba(0,0,0,.75);
            display:flex; align-items:center; justify-content:center; }}
  .cell.lit .medal {{ border-color:rgba(240,214,140,.75);
                      box-shadow:0 0 30px rgba(212,170,70,.20), inset 0 0 20px rgba(0,0,0,.7); }}
  .medal svg {{ width:96px; height:96px; }}
  .glyph {{ fill:none; stroke:#f2e2b0; stroke-width:5.4; stroke-linecap:round; stroke-linejoin:round; }}
  .cell.lit .glyph {{ stroke:#ffdf9a; }}
  .txt h2 {{ font-family:'Cinzel',serif; font-size:31px; font-weight:600; letter-spacing:.055em;
             color:#eccd7d; line-height:1.16; }}
  .cell.lit .txt h2 {{ color:#ffdf9f; }}
  .mark {{ font-size:19px; margin-left:11px; vertical-align:middle; }}
  .mark.on {{ color:#ffd257; }}
  .mark.prop {{ color:#8f86a3; }}
  .eff {{ font-size:24px; color:#bdb6a2; margin-top:5px; line-height:1.35; }}
  .auto {{ font-size:21.5px; color:#96854f; font-style:italic; margin-top:5px; letter-spacing:.015em; }}
  footer {{ margin-top:54px; text-align:center; }}
  .legend {{ font-size:22px; color:#a49b84; letter-spacing:.02em; }}
  .legend span {{ margin:0 20px; }}
  .note {{ max-width:1180px; margin:26px auto 0; font-size:23px; line-height:1.6;
           color:#8f8874; font-style:italic; }}
  .note b {{ color:#c2a75f; font-style:normal; font-weight:500; }}
  .sig {{ margin-top:30px; font-family:'Cinzel',serif; font-size:18px; letter-spacing:.4em;
          color:#7d6631; text-transform:uppercase; }}
</style></head>
<body>
<div class="bg"></div>
<div class="frame"></div>
<svg width="0" height="0" style="position:absolute">
  <defs>
    <marker id="tip" viewBox="0 0 10 10" refX="7.4" refY="5" markerWidth="4.4" markerHeight="4.4" orient="auto-start-reverse">
      <path d="M0,0.6 L10,5 L0,9.4 z" fill="#f2e2b0"/>
    </marker>
    <marker id="dot" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="2.3" markerHeight="2.3">
      <circle cx="5" cy="5" r="4.4" fill="#f2e2b0"/>
    </marker>
  </defs>
</svg>
<div class="wrap">
  <header>
    <div class="kicker">Wand Portal &middot; SpellSight</div>
    <h1>The Grimoire</h1>
    <div class="sub">Wand Movements &amp; Their Workings</div>
    <div class="rule"><i></i><b>&#10022;</b><i></i></div>
    <div class="scroll">
      The wand traces the shape; the house answers. Each movement below is a
      <em>starting vocabulary</em> — the portal learns whatever you actually wave,
      five or six clean passes to a spell.
    </div>
  </header>
  <div class="grid">{''.join(cells)}
  </div>
  <footer>
    <div class="rule"><i></i><b>&#10022;</b><i></i></div>
    <div class="legend">
      <span><span class="mark on">&#9733;</span> enabled in <code>config.yaml</code></span>
      <span><span class="mark prop">&#9671;</span> proposed movement</span>
      <span>&#9679; begin &nbsp;&#9656; end</span>
    </div>
    <div class="note">
      Direction is part of the spell. Rotation normalization is deliberately off, so an
      up-stroke and a down-stroke are two different castings — which is why
      <b>Nox</b> reverses <b>Lumos</b>, and <b>Colloportus</b> reverses <b>Alohomora</b>.
      Six to ten movements is the realistic ceiling; add one at a time and re-run the
      separation check. There is no microphone — <b>the movement is the whole incantation.</b>
    </div>
    <div class="sig">Infrared &middot; Retroreflective &middot; Entirely Local</div>
  </footer>
</div>
</body></html>"""

def render(html: str, out: str) -> None:
    """Rasterize through headless chromium, then shrink the file.

    The poster is mostly smooth dark gradient, which PNG stores badly — a 64
    colour k-means palette drops it from ~3.8MB to ~1.1MB with no banding
    visible at full size, which matters for something a README embeds.
    """
    import cv2
    import numpy as np
    from playwright.sync_api import sync_playwright

    chrome = os.environ.get("CHROME_PATH", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    with tempfile.TemporaryDirectory() as tmp:
        page_path = os.path.join(tmp, "spellbook.html")
        raw = os.path.join(tmp, "raw.png")
        with open(page_path, "w") as fh:
            fh.write(html)
        with sync_playwright() as p:
            launch = {"args": ["--no-sandbox", "--font-render-hinting=none"]}
            if os.path.exists(chrome):
                launch["executable_path"] = chrome
            browser = p.chromium.launch(**launch)
            pg = browser.new_page(viewport={"width": 1700, "height": 1200}, device_scale_factor=1.0)
            pg.goto(f"file://{page_path}")
            pg.wait_for_timeout(3500)          # let the webfonts land
            if not pg.evaluate("document.fonts.check('700 104px Cinzel')"):
                print("  warning: Cinzel did not load; falling back to a local serif")
            pg.screenshot(path=raw, full_page=True)
            browser.close()

        img = cv2.imread(raw, cv2.IMREAD_COLOR)
        flat = img.reshape(-1, 3).astype(np.float32)
        rng = np.random.default_rng(0)
        sample = flat[rng.choice(len(flat), min(60000, len(flat)), replace=False)]
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
        _, _, centres = cv2.kmeans(sample, 64, None, crit, 3, cv2.KMEANS_PP_CENTERS)
        idx = np.empty(len(flat), np.int32)
        for i in range(0, len(flat), 500_000):          # chunked to bound memory
            chunk = flat[i:i + 500_000]
            idx[i:i + 500_000] = ((chunk[:, None, :] - centres[None]) ** 2).sum(2).argmin(1)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        cv2.imwrite(out, centres[idx].reshape(img.shape).astype(np.uint8),
                    [cv2.IMWRITE_PNG_COMPRESSION, 9])
        print(f"  {img.shape[1]}x{img.shape[0]}, {os.path.getsize(out) // 1024}KB")


print(f"spellbook: {len(ORDER)} movements ({len([s for s in ORDER if s in ENABLED])} enabled)")
render(html, OUT)
print(f"wrote {os.path.relpath(OUT, ROOT)}")
