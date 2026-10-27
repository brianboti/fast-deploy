"""The look of every page the controller serves: one design system, written as CSS strings.

What the best product interfaces have in common, and what this follows: hierarchy from weight and spacing rather than
decoration; colour held back for meaning (the accent for the one primary action, links, focus and the live state; green,
amber and red for one meaning each); structure from faint borders, with shadows only where something floats or lifts; a
small type scale with tabular numbers; spacing from one scale; every interactive element with a hover, pressed, focused and
disabled state; and a dark theme designed as such (surfaces step up in lightness, accents desaturated, never pure black)
rather than inverted. Motion is brief and purposeful, and off when a person asks for reduced motion.

Under the pages' Content-Security-Policy (default-src 'none'; style-src 'unsafe-inline') nothing here may load a file: no
web font, no image, no url(), no @import. Every page module builds on STYLE; the Monitor views add MONITOR_STYLE; the
deployment page adds its own on top of the same tokens; the charts of labdeploy/charts.py colour themselves with the --c-*
tokens so they follow the theme. Nothing here reads data.
"""

# ---- tokens: the only place a colour, radius, shadow or duration is spelled out, in light and in dark

TOKENS = """
:root{color-scheme:light dark;
--font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Helvetica Neue",system-ui,sans-serif;
--mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
--label:#111318;--label-2:#5F6672;--label-3:#848B96;--label-4:#B2B7C0;
--canvas:#F6F7F9;--surface:#FFFFFF;--surface-2:#FAFBFC;--surface-3:#F2F3F5;
--fill:rgba(17,19,24,.045);--fill-2:rgba(17,19,24,.08);--fill-3:rgba(17,19,24,.16);
--hairline:rgba(17,19,24,.08);--hairline-strong:rgba(17,19,24,.15);--line:rgba(17,19,24,.06);
--accent:#2E6BEA;--accent-hover:#2560D8;--accent-deep:#1E4FB8;--accent-text:#1E58CC;--accent-rgb:46,107,234;
--accent-soft:rgba(46,107,234,.10);--accent-soft-2:rgba(46,107,234,.16);
--accent-top:#4A84F2;--accent-bottom:#2457D4;
--green:#22A757;--green-text:#157A3F;--green-soft:rgba(34,167,87,.12);--green-rgb:34,167,87;
--amber:#E8930C;--amber-text:#8A5200;--amber-soft:rgba(232,147,12,.14);--amber-rgb:232,147,12;
--red:#E5484D;--red-text:#C4262C;--red-soft:rgba(229,72,77,.12);--red-rgb:229,72,77;
--on-accent:#FFFFFF;--halo:#FFFFFF;
--ring:0 0 0 3px rgba(46,107,234,.30);
--r-sm:8px;--r-md:10px;--r-lg:14px;--r-pill:980px;
--shadow-1:0 1px 2px rgba(17,19,24,.04);
--shadow-2:0 1px 2px rgba(17,19,24,.05),0 8px 24px -8px rgba(17,19,24,.14);
--shadow-float:0 2px 6px rgba(17,19,24,.06),0 16px 40px -12px rgba(17,19,24,.24);
--ease:cubic-bezier(.2,.8,.2,1);--ease-in-out:cubic-bezier(.45,0,.2,1);--fast:140ms;--base:220ms;--slow:400ms;
--side:244px;--side-bg:rgba(246,247,249,.78);--bar-bg:rgba(246,247,249,.84);
--c-accent:#2E6BEA;--c-second:#F0742E;--c-good:#22A757;--c-warn:#E8930C;--c-bad:#E5484D;--c-quiet:#CBD0D8;--c-ink:#6B7280;
--c-grid:#E9EBEF;--c-axis:#D3D7DE;--c-track:#EEF0F3;--c-empty:#E2E5EA;
--text:var(--label);--secondary:var(--label-2);--muted:var(--label-3);--border:var(--hairline-strong);--field:var(--hairline-strong);
--blue:var(--accent);--amber-bg:var(--amber-soft)}
@media (prefers-color-scheme:dark){:root{
--label:#EDEEF2;--label-2:#A0A6B1;--label-3:#737A88;--label-4:#4B515C;
--canvas:#0E1014;--surface:#15181E;--surface-2:#1A1E25;--surface-3:#20242C;
--fill:rgba(255,255,255,.05);--fill-2:rgba(255,255,255,.09);--fill-3:rgba(255,255,255,.17);
--hairline:rgba(255,255,255,.08);--hairline-strong:rgba(255,255,255,.15);--line:rgba(255,255,255,.065);
--accent:#5B8DEF;--accent-hover:#6D9BF2;--accent-deep:#9DBCF8;--accent-text:#86ADF7;--accent-rgb:91,141,239;
--accent-soft:rgba(91,141,239,.16);--accent-soft-2:rgba(91,141,239,.24);
--accent-top:#6A99F2;--accent-bottom:#3F72E0;
--green:#3DBB6D;--green-text:#72D698;--green-soft:rgba(61,187,109,.16);--green-rgb:61,187,109;
--amber:#F0A830;--amber-text:#F5C26B;--amber-soft:rgba(240,168,48,.16);--amber-rgb:240,168,48;
--red:#F06A6E;--red-text:#F59396;--red-soft:rgba(240,106,110,.16);--red-rgb:240,106,110;
--on-accent:#FFFFFF;--halo:#15181E;
--ring:0 0 0 3px rgba(91,141,239,.40);
--shadow-1:0 1px 2px rgba(0,0,0,.35);
--shadow-2:0 1px 2px rgba(0,0,0,.4),0 8px 24px -8px rgba(0,0,0,.6);
--shadow-float:0 2px 6px rgba(0,0,0,.45),0 16px 40px -12px rgba(0,0,0,.7);
--side-bg:rgba(14,16,20,.78);--bar-bg:rgba(14,16,20,.84);
--c-accent:#5B8DEF;--c-second:#FF8A5B;--c-good:#3DBB6D;--c-warn:#F0A830;--c-bad:#F06A6E;--c-quiet:#3A404B;--c-ink:#A0A6B1;
--c-grid:rgba(255,255,255,.07);--c-axis:rgba(255,255,255,.15);--c-track:rgba(255,255,255,.08);--c-empty:rgba(255,255,255,.12)}}
"""

# ---- the base: reset, type, the shell, the page head, cards and rows, controls, buttons, chips, callouts, tables, sign-in

BASE = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%;scroll-behavior:smooth;scrollbar-width:thin;scrollbar-color:var(--fill-3) transparent}
body{margin:0;background:var(--canvas);color:var(--label);font:13.5px/1.5 var(--font);-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
font-feature-settings:"kern","liga","calt";font-optical-sizing:auto}
::selection{background:var(--accent-soft-2)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:var(--fill-3);border-radius:10px;border:3px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-track{background:transparent}
a{color:var(--accent-text);text-decoration:none;transition:color var(--fast) var(--ease)}a:hover{text-decoration:underline;text-underline-offset:3px;text-decoration-thickness:1px}
code,pre,kbd,.mono{font-family:var(--mono);font-size:.93em}
h1,h2,h3,h4{font-weight:600;margin:0;line-height:1.25;color:var(--label);text-wrap:balance}
h1{font-size:28px;font-weight:700;letter-spacing:-.028em;line-height:1.15}h2{font-size:16px;letter-spacing:-.012em}h3{font-size:14px;letter-spacing:-.008em}
p{margin:0}
strong{font-weight:600}
:focus-visible{outline:none;box-shadow:var(--ring);border-radius:var(--r-sm)}
.icon{width:16px;height:16px;flex:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.muted{color:var(--label-3)}.num{font-variant-numeric:tabular-nums}.nowrap{white-space:nowrap}

/* the shell: a translucent sidebar and the page */
.shell{display:flex;min-height:100vh}
.side{position:sticky;top:0;height:100vh;width:var(--side);flex:0 0 var(--side);background:var(--side-bg);-webkit-backdrop-filter:saturate(160%) blur(18px);
backdrop-filter:saturate(160%) blur(18px);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:18px 12px 16px;z-index:10}
.mark{display:flex;align-items:center;gap:10px;padding:4px 10px 20px;font-weight:600;font-size:14px;letter-spacing:-.012em;color:var(--label)}
.mark:hover{text-decoration:none}.logo{width:24px;height:24px;flex:none;border-radius:7px;box-shadow:0 1px 2px rgba(0,0,0,.18),inset 0 1px 0 rgba(255,255,255,.2)}
.nav{display:flex;flex-direction:column;gap:18px}
.nav-links{display:flex;flex-direction:column;gap:1px}
.nav-group{margin:0 0 6px;padding:0 10px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--label-3)}
.nav a{display:flex;align-items:center;gap:10px;height:32px;padding:0 10px;border-radius:8px;color:var(--label-2);font-weight:500;white-space:nowrap;
transition:background var(--fast) var(--ease),color var(--fast) var(--ease),transform var(--fast) var(--ease)}
.nav a .icon{color:var(--label-3);transition:color var(--fast) var(--ease)}
.nav a:hover{background:var(--fill);color:var(--label);text-decoration:none}.nav a:hover .icon{color:var(--label-2)}
.nav a:active{transform:scale(.985)}
.nav a[aria-current=page]{background:var(--fill-2);color:var(--label);font-weight:600;box-shadow:inset 0 1px 0 rgba(255,255,255,.35)}
.nav a[aria-current=page] .icon{color:var(--accent)}
.nav .count{margin-left:auto;min-width:20px;height:18px;padding:0 6px;border-radius:var(--r-pill);background:var(--fill-2);color:var(--label-2);
font-size:11.5px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;font-variant-numeric:tabular-nums}
.who{margin-top:auto;border-top:1px solid var(--line);padding:14px 4px 0;display:grid;grid-template-columns:32px minmax(0,1fr);gap:1px 10px;align-items:center;font-size:13px}
.who .avatar{grid-row:1/3;width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--accent-soft-2);
color:var(--accent-text);font-weight:600;font-size:13px;box-shadow:inset 0 0 0 1px rgba(var(--accent-rgb),.18)}
.who .as{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.who .name{font-weight:600;overflow-wrap:anywhere;line-height:1.2}.who .version{color:var(--label-3);font-size:11.5px;line-height:1.2;font-variant-numeric:tabular-nums}
.who form{grid-column:1/-1;margin-top:12px}
.who .btn{width:100%}
.main{flex:1;min-width:0}
.content{max-width:1080px;padding:36px 48px 80px;margin:0 auto}

/* the page head: a large title, a sentence, then the state as chips */
.crumb{display:inline-flex;align-items:center;gap:4px;color:var(--label-2);font-size:12.5px;font-weight:500;margin-bottom:10px;border-radius:var(--r-pill);
padding:4px 10px 4px 6px;margin-left:-6px;transition:background var(--fast) var(--ease),color var(--fast) var(--ease)}
.crumb:hover{background:var(--fill);text-decoration:none;color:var(--label)}
.page-head{margin-bottom:26px}
.page-head .lede{color:var(--label-2);margin-top:8px;max-width:640px;font-size:14px;line-height:1.55}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.chip{display:inline-flex;align-items:center;gap:7px;height:24px;padding:0 10px;border-radius:var(--r-pill);background:var(--fill);color:var(--label-2);
font-size:12px;font-weight:500;white-space:nowrap;box-shadow:inset 0 0 0 1px var(--line)}
.chip::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--label-4);flex:none}
.chip.ok{background:var(--green-soft);color:var(--green-text);box-shadow:inset 0 0 0 1px rgba(var(--green-rgb),.18)}.chip.ok::before{background:var(--green)}
.chip.warn{background:var(--amber-soft);color:var(--amber-text);box-shadow:inset 0 0 0 1px rgba(var(--amber-rgb),.22)}.chip.warn::before{background:var(--amber)}
.chip.bad{background:var(--red-soft);color:var(--red-text);box-shadow:inset 0 0 0 1px rgba(var(--red-rgb),.2)}.chip.bad::before{background:var(--red)}
.chip.plain::before{display:none}
.chip.idle::before{background:var(--label-4)}

/* cards: surfaces on the canvas, a faint border; a shadow only where a card lifts */
.card{background:var(--surface);border:1px solid var(--hairline);border-radius:var(--r-lg);margin-bottom:14px;
transition:box-shadow var(--base) var(--ease),transform var(--base) var(--ease),border-color var(--base) var(--ease)}
.card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 22px 4px}
.card-head .sub{color:var(--label-2);font-size:13px;margin-top:3px;line-height:1.5}
.card-body{padding:2px 22px 6px}
.card.flush .card-body{padding:0}
.card.flush .card-head{padding-bottom:14px;border-bottom:1px solid var(--line)}
.card.flush .table-scroll{border-radius:0 0 var(--r-lg) var(--r-lg)}
details.card>summary{list-style:none;cursor:pointer;padding-bottom:18px;transition:background var(--fast) var(--ease);border-radius:var(--r-lg)}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary:hover{background:var(--surface-2)}
details.card[open]>summary{padding-bottom:4px;border-radius:var(--r-lg) var(--r-lg) 0 0}
details.card[open]>summary:hover{background:transparent}
.chev{transition:transform var(--base) var(--ease);color:var(--label-3);margin-top:3px}
details[open]>summary .chev{transform:rotate(90deg)}
details[open]>:not(summary){animation:reveal var(--base) var(--ease) both}
.section-head{margin:30px 0 12px}.section-head .sub{color:var(--label-2);font-size:13px;margin-top:3px}

/* a settings row: words on the left, the control on the right, a hairline between rows */
.row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,340px);gap:8px 32px;padding:15px 0;border-top:1px solid var(--line);align-items:start}
.card-body>.row:first-child,.card-body>form:first-child>.row,.card-body>form>.row:first-child{border-top:0}
.label-line{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 8px}
.label{font-weight:500;color:var(--label);font-size:14px}
.key{font-size:11px;color:var(--label-3);background:var(--fill);border-radius:5px;padding:1px 6px;line-height:16px;white-space:nowrap;font-family:var(--mono)}
.help{color:var(--label-2);font-size:12.5px;margin-top:4px;max-width:520px;line-height:1.5}
.control{display:flex;flex-direction:column;gap:6px;min-width:0}
.control.switch-cell{align-items:flex-end}
.card:has(input[name="platform_updates.on"]:not(:checked)) .dep{opacity:.45;transition:opacity var(--base) var(--ease)}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 28px;padding-bottom:12px}
.grid2 .field{padding:12px 0;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:6px}
.facts{margin:0}.facts div{display:grid;grid-template-columns:180px minmax(0,1fr);gap:16px;padding:11px 0;border-top:1px solid var(--line)}
.facts div:first-child{border-top:0}.facts dt{color:var(--label-2)}.facts dd{margin:0;overflow-wrap:anywhere}
.facts.tight div{grid-template-columns:200px minmax(0,1fr)}

/* controls: fields, selects, the switch */
input[type=text],input[type=password],input[type=number],input[type=url],input[type=email],select,textarea{width:100%;height:36px;padding:0 11px;
border:1px solid var(--hairline-strong);border-radius:var(--r-sm);background:var(--surface);color:var(--label);font:inherit;font-size:13.5px;
box-shadow:var(--shadow-1);transition:border-color var(--fast) var(--ease),box-shadow var(--fast) var(--ease),background var(--fast) var(--ease)}
textarea{height:auto;min-height:84px;padding:8px 11px;line-height:1.5;resize:vertical}
input:hover,select:hover,textarea:hover{border-color:var(--fill-3)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:var(--ring)}
input::placeholder,textarea::placeholder{color:var(--label-4)}
input:disabled,select:disabled{background:var(--surface-3);color:var(--label-3);box-shadow:none;cursor:not-allowed}
.affix{display:flex;height:36px;border:1px solid var(--hairline-strong);border-radius:var(--r-sm);background:var(--surface);overflow:hidden;box-shadow:var(--shadow-1);
transition:border-color var(--fast) var(--ease),box-shadow var(--fast) var(--ease)}
.affix input{border:0;height:34px;border-radius:0;min-width:0;flex:1;box-shadow:none}.affix input:focus{box-shadow:none}
.affix:focus-within{border-color:var(--accent);box-shadow:var(--ring)}
.unit{display:flex;align-items:center;padding:0 11px;border-left:1px solid var(--line);background:var(--surface-2);color:var(--label-2);font-size:12px;white-space:nowrap}
.select{position:relative;display:block}
.select select{appearance:none;-webkit-appearance:none;padding-right:34px;cursor:pointer}
.select::after{content:"";position:absolute;right:14px;top:50%;width:6px;height:6px;border-right:1.6px solid var(--label-2);
border-bottom:1.6px solid var(--label-2);transform:translateY(-70%) rotate(45deg);pointer-events:none}
.switch{appearance:none;-webkit-appearance:none;margin:0;width:38px;height:22px;border-radius:var(--r-pill);cursor:pointer;flex:none;position:relative;
background:var(--fill-3);transition:background var(--base) var(--ease),box-shadow var(--fast) var(--ease);border:0;box-shadow:inset 0 1px 2px rgba(0,0,0,.08)}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;
box-shadow:0 1px 3px rgba(0,0,0,.25),0 0 0 .5px rgba(0,0,0,.05);transition:transform var(--base) var(--ease),width var(--fast) var(--ease)}
.switch:checked{background:var(--accent);box-shadow:none}
.switch:checked::after{transform:translateX(16px)}
.switch:active::after{width:22px}
.switch:checked:active::after{transform:translateX(12px)}
.switch:focus-visible{box-shadow:var(--ring)}
.invalid input,.invalid select,.invalid textarea,.invalid .affix{border-color:var(--red)}
.invalid .affix input{border:0}
.field-error{color:var(--red-text);font-size:12.5px}

/* buttons: one primary action a view; the rest quiet */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:36px;padding:0 14px;border-radius:var(--r-sm);border:1px solid transparent;
font:inherit;font-size:13.5px;font-weight:500;letter-spacing:-.006em;cursor:pointer;white-space:nowrap;text-decoration:none;color:var(--accent-text);
background:var(--accent-soft);transition:background var(--fast) var(--ease),border-color var(--fast) var(--ease),color var(--fast) var(--ease),
transform var(--fast) var(--ease),box-shadow var(--fast) var(--ease)}
.btn:hover{text-decoration:none;background:var(--accent-soft-2)}
.btn:active{transform:translateY(1px)}
.btn:disabled,.btn[aria-disabled=true]{opacity:.5;cursor:not-allowed;transform:none}
.btn .icon{width:15px;height:15px}
.btn.primary{background:linear-gradient(180deg,var(--accent-top),var(--accent-bottom));color:var(--on-accent);border-color:rgba(var(--accent-rgb),.6);
box-shadow:inset 0 1px 0 rgba(255,255,255,.18),0 1px 2px rgba(var(--accent-rgb),.35)}
.btn.primary:hover{background:linear-gradient(180deg,var(--accent-hover),var(--accent-bottom));box-shadow:inset 0 1px 0 rgba(255,255,255,.18),0 2px 6px rgba(var(--accent-rgb),.4)}
.btn.primary:active{box-shadow:inset 0 1px 2px rgba(0,0,0,.2)}
.btn.secondary{background:var(--surface);color:var(--label);border-color:var(--hairline-strong);box-shadow:var(--shadow-1)}
.btn.secondary:hover{background:var(--surface-2);border-color:var(--fill-3)}
.btn.danger{background:var(--surface);color:var(--red-text);border-color:rgba(var(--red-rgb),.4);box-shadow:var(--shadow-1)}.btn.danger:hover{background:var(--red-soft)}
.btn.ghost{background:transparent;color:var(--label-2);padding:0 9px;height:30px;margin-left:-9px}.btn.ghost:hover{background:var(--fill);color:var(--label)}
.btn.block{width:100%}
.actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin:18px 0 30px}
.actions .note{margin-right:auto;color:var(--label-3);font-size:12.5px}
.danger-card{border-color:rgba(var(--red-rgb),.3)}.danger-card .card-head h2{color:var(--red-text)}
.danger-card .row .control{gap:8px}.danger-card .confirm-line{display:flex;gap:8px}.danger-card .confirm-line input{flex:1;min-width:0}

/* a pager under a long list: where you are, and the pages */
.pager{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 22px;border-top:1px solid var(--line);font-size:12.5px;color:var(--label-2)}
.pager .pages{display:inline-flex;align-items:center;gap:4px}
.pager .pages a,.pager .pages span{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:30px;padding:0 8px;border-radius:7px;
color:var(--label-2);font-weight:500;font-variant-numeric:tabular-nums;transition:background var(--fast) var(--ease),color var(--fast) var(--ease)}
.pager .pages a:hover{background:var(--fill);color:var(--label);text-decoration:none}
.pager .pages [aria-current=page]{background:var(--fill-2);color:var(--label);font-weight:600}
.pager .pages .gap{min-width:20px;color:var(--label-4)}
.pager .pages .btn{height:30px;padding:0 10px;font-size:12.5px}
.pager .pages .btn.off{opacity:.45;pointer-events:none}

/* callouts: one soft colour, one meaning */
.callout{display:flex;gap:12px;align-items:flex-start;padding:13px 16px;border:1px solid transparent;border-radius:var(--r-md);margin-bottom:18px;font-size:13.5px;line-height:1.5}
.callout>.icon{width:18px;height:18px;margin-top:1px}
.callout .body{display:flex;flex-direction:column;gap:4px;min-width:0}
.callout ul{margin:4px 0 0;padding-left:18px}.callout li+li{margin-top:2px}
.callout.amber{background:var(--amber-soft);border-color:rgba(var(--amber-rgb),.25);color:var(--amber-text)}.callout.amber>.icon{color:var(--amber)}
.callout.green{background:var(--green-soft);border-color:rgba(var(--green-rgb),.25);color:var(--green-text)}.callout.green>.icon{color:var(--green)}
.callout.red{background:var(--red-soft);border-color:rgba(var(--red-rgb),.25);color:var(--red-text)}.callout.red>.icon{color:var(--red)}
.callout.gray{background:var(--surface);border-color:var(--hairline);color:var(--label-2)}.callout.gray>.icon{color:var(--label-3)}
.callout code{font-size:12px}.callout.red a{color:inherit;text-decoration:underline;text-underline-offset:2px}

/* tables */
.table-scroll{overflow-x:auto}
table.list{width:100%;border-collapse:collapse;font-size:13.5px}
.list th{text-align:left;font-weight:500;color:var(--label-2);font-size:12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--surface-2);white-space:nowrap}
.list td{padding:13px 16px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
.list tbody tr:last-child td{border-bottom:0}
.list tbody tr{position:relative;transition:background var(--fast) var(--ease)}.list tbody tr:hover{background:var(--surface-2)}
.list a.stretch{color:var(--label);font-weight:600}.list a.stretch::after{content:"";position:absolute;inset:0}
.list .sub{display:block;color:var(--label-3);font-size:12px;font-weight:400;margin-top:1px}
.list .go{width:32px;color:var(--label-4);text-align:right}
.list .go .icon{transition:transform var(--fast) var(--ease),color var(--fast) var(--ease)}
.list tbody tr:hover .go .icon{transform:translateX(3px);color:var(--accent)}
.list .repo{font-family:var(--mono);font-size:12.5px;color:var(--label-2)}
.list .r,.r{text-align:right}
table.compact th,table.compact td{padding:8px 12px;font-size:12px}
.list tr.config td{background:var(--amber-soft)}
.list a .icon,.links .icon{width:14px;height:14px;margin-left:4px;vertical-align:-2px}.list td.outcome{min-width:170px}
.list td.outcome .chip{margin-top:4px}
.list td.wrap{white-space:normal;min-width:200px;color:var(--label-2);font-size:12.5px;overflow-wrap:anywhere}

/* people, changes and the file diff */
.people>details{border-top:1px solid var(--line)}.people>details:first-child{border-top:0}
.people summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:minmax(120px,200px) minmax(0,1fr) auto;gap:12px;align-items:center;padding:15px 22px;
transition:background var(--fast) var(--ease)}
.people summary::-webkit-details-marker{display:none}.people summary:hover{background:var(--surface-2)}
.people summary .who-cell{font-weight:600;overflow-wrap:anywhere}.people summary .chips{margin:0;gap:6px}
.people .edit{padding:0 22px 8px;background:var(--surface-2);border-top:1px solid var(--line)}
.people>details:last-child .edit{border-radius:0 0 var(--r-lg) var(--r-lg)}
.changes{list-style:none;margin:0;padding:0}
.changes li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 24px;padding:12px 0;border-top:1px solid var(--line);align-items:baseline}
.changes li:first-child{border-top:0}
.changes .what{display:flex;flex-wrap:wrap;gap:4px 8px;align-items:baseline}
.changes .values{text-align:right;overflow-wrap:anywhere}
.old{color:var(--label-3);text-decoration:line-through;text-decoration-color:var(--label-4)}.new{font-weight:600}
details.diff>summary{list-style:none;cursor:pointer;display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;padding:15px 22px;font-weight:500}
details.diff>summary .muted{font-weight:400;font-size:12.5px}
details.diff>summary::-webkit-details-marker{display:none}
pre.diff{margin:0;padding:10px 0;border-top:1px solid var(--line);overflow-x:auto;font-size:12px;line-height:1.65;background:var(--surface);border-radius:0 0 var(--r-lg) var(--r-lg)}
pre.diff span{display:block;padding:0 22px;white-space:pre;min-width:max-content}
pre.diff .add{background:var(--green-soft);color:var(--green-text)}pre.diff .del{background:var(--red-soft);color:var(--red-text)}pre.diff .meta{color:var(--label-3)}
.editor{background:var(--surface);border:1px solid var(--hairline-strong);border-radius:var(--r-lg);padding:6px;box-shadow:var(--shadow-1);
transition:border-color var(--fast) var(--ease),box-shadow var(--fast) var(--ease)}
.editor textarea{display:block;border:0;background:transparent;font-family:var(--mono);font-size:12.5px;line-height:1.65;min-height:520px;padding:12px 14px;
white-space:pre;overflow:auto;tab-size:2;box-shadow:none}
.editor textarea:focus{box-shadow:none}.editor:focus-within{border-color:var(--accent);box-shadow:var(--ring)}
.invalid .editor{border-color:var(--red)}
textarea.mono{font-family:var(--mono);font-size:12.5px}
.token-box{border:1px solid rgba(var(--amber-rgb),.35);background:var(--amber-soft);border-radius:var(--r-lg);padding:18px 22px;margin-bottom:18px;color:var(--amber-text)}
.token-box h2{font-size:15px;color:var(--label);margin-bottom:4px}
.token{display:block;margin-top:12px;padding:12px 14px;background:var(--surface);border:1px dashed var(--amber);border-radius:var(--r-sm);color:var(--label);
font-family:var(--mono);font-size:14px;user-select:all;-webkit-user-select:all;overflow-wrap:anywhere}
.empty{color:var(--label-3);font-size:13px;padding:22px;margin:4px 0 16px;text-align:center;border:1px dashed var(--hairline-strong);border-radius:var(--r-md)}

/* sign-in and other pages outside the frame */
.center{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px 16px;
background:radial-gradient(900px 480px at 50% -10%,var(--accent-soft),transparent 70%),var(--canvas)}
.center .card{width:100%;max-width:420px;padding:34px 32px 28px;margin:0;box-shadow:var(--shadow-float)}
.center .card.wide{max-width:600px}
.center .logo{width:40px;height:40px;margin-bottom:22px;border-radius:11px}
.center h1{font-size:22px;margin-bottom:6px;letter-spacing:-.024em}.center .lede{color:var(--label-2);margin-bottom:22px;line-height:1.5}
.center label{display:block;font-weight:500;margin-bottom:6px}.center form .btn{margin-top:14px;height:40px;font-size:14px}
.center input{height:40px;font-size:14px}
.foot{color:var(--label-3);font-size:12px;margin-top:20px;line-height:1.5}
"""

# ---- the Monitor views: toolbars, tiles, application cards, figures and the charts of labdeploy/charts.py

MONITOR_STYLE = """
.toolbar{position:sticky;top:0;z-index:5;display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;margin:-10px -12px 20px;padding:9px 12px;
border-radius:12px;background:var(--bar-bg);-webkit-backdrop-filter:saturate(160%) blur(18px);backdrop-filter:saturate(160%) blur(18px)}
.seg{display:inline-flex;padding:2px;border-radius:9px;background:var(--fill);flex:none;gap:2px;box-shadow:inset 0 0 0 1px var(--line)}
.seg a{display:inline-flex;align-items:center;height:26px;padding:0 11px;color:var(--label-2);font-size:12.5px;font-weight:500;border-radius:7px;white-space:nowrap;
transition:background var(--fast) var(--ease),color var(--fast) var(--ease),box-shadow var(--fast) var(--ease)}
.seg a:hover{color:var(--label);text-decoration:none}
.seg a[aria-current=page]{background:var(--surface);color:var(--label);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.12),0 0 0 1px var(--line)}
.fresh{display:inline-flex;align-items:center;gap:8px;color:var(--label-2);font-size:12px}
.fresh .dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex:none;position:relative}
.fresh .dot::after{content:"";position:absolute;inset:-4px;border-radius:50%;border:1.5px solid var(--green);opacity:0;animation:ripple 2.2s var(--ease) infinite}
.fresh .dot.off{background:var(--label-4)}.fresh .dot.off::after{display:none}
.toolbar .refresh{margin-left:auto}
.tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:16px 18px 14px;min-width:0;
display:flex;flex-direction:column;gap:2px;transition:box-shadow var(--base) var(--ease),transform var(--base) var(--ease),border-color var(--base) var(--ease)}
.tile:hover{box-shadow:var(--shadow-2);transform:translateY(-1px);border-color:var(--hairline-strong)}
.tile .label{color:var(--label-2);font-size:12.5px;font-weight:500}
.tile .value{font-size:30px;font-weight:700;letter-spacing:-.03em;line-height:1.1;margin-top:6px;font-variant-numeric:tabular-nums}
.tile .value small{font-size:13px;font-weight:500;color:var(--label-3);letter-spacing:0}
.tile .note{color:var(--label-3);font-size:12px;margin-top:4px}
.tile svg.chart{margin-top:10px}
.tile .bullet-name{font-size:12px;color:var(--label-2);margin-top:10px;display:flex;justify-content:space-between;gap:8px}
.attention{list-style:none;margin:0;padding:0}
.attention li{display:grid;grid-template-columns:20px minmax(0,1fr) auto;gap:4px 12px;align-items:start;padding:12px 0;border-top:1px solid var(--line)}
.attention li:first-child{border-top:0}
.attention .icon{width:18px;height:18px;margin-top:1px}.attention .bad .icon,.attention li.bad .icon{color:var(--red)}
.attention .warn .icon,.attention li.warn .icon{color:var(--amber)}.attention li.ok .icon{color:var(--green)}
.attention a{white-space:nowrap;font-size:12.5px;font-weight:500}
.apps{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.app{position:relative;background:var(--surface);border:1px solid var(--hairline);border-radius:var(--r-lg);padding:16px 18px 14px;min-width:0;
transition:box-shadow var(--base) var(--ease),transform var(--base) var(--ease),border-color var(--base) var(--ease)}
.app:hover{box-shadow:var(--shadow-2);transform:translateY(-1px);border-color:var(--hairline-strong)}
.app-head{display:flex;align-items:center;justify-content:space-between;gap:8px}
.app-head a{font-weight:600;color:var(--label);overflow-wrap:anywhere;font-size:14.5px;letter-spacing:-.01em}
.app-head a::after{content:"";position:absolute;inset:0;border-radius:var(--r-lg)}
.app svg.chart{position:relative;z-index:1}
.svc{margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.app-head+.svc{border-top:0;padding-top:0;margin-top:12px}
.env{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--label-3);margin-bottom:6px}
.stats{display:flex;flex-wrap:wrap;gap:2px 14px;font-size:12.5px;color:var(--label-2);margin-top:8px}
.stats strong{color:var(--label);font-weight:600;font-variant-numeric:tabular-nums}
.release-line{font-size:12.5px;color:var(--label-2);margin-top:6px;overflow-wrap:anywhere}
.reasons{margin:8px 0 0;padding-left:16px;font-size:12.5px;color:var(--amber-text)}
.viz{margin:0;padding:8px 0 16px;min-width:0}
.viz+.viz{border-top:1px solid var(--line);padding-top:16px}
.viz-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:2px 12px}
.viz-head h3{font-size:14px}.viz-head .sub{color:var(--label-3);font-size:12px;font-variant-numeric:tabular-nums}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;list-style:none;margin:8px 0 12px;padding:0;font-size:12px;color:var(--label-2)}
.legend li{display:inline-flex;align-items:center;gap:6px}.legend svg{flex:none;overflow:visible}
svg.chart{display:block;width:100%;overflow:visible;animation:chart-in var(--slow) var(--ease) both}
svg.chart .t{font-size:11px;fill:var(--label-3);font-variant-numeric:tabular-nums;paint-order:stroke;stroke:var(--halo);stroke-width:3px;stroke-linejoin:round}
svg.chart .tl{font-size:12px;fill:var(--label);font-weight:500}svg.chart .tv{font-size:12px;fill:var(--label)}svg.chart .th{fill:var(--label-2)}
svg.chart .hit{fill:transparent;transition:fill var(--fast) var(--ease)}svg.chart .hit:hover{fill:rgba(var(--accent-rgb),.08)}
svg.chart .mk:hover,svg.chart .cell:hover{opacity:.75}svg.chart .row:hover rect{opacity:.85}
.viz-note{color:var(--label-3);font-size:12px;margin-top:8px;line-height:1.5}
details.numbers{margin-top:10px}
details.numbers>summary{display:inline-flex;align-items:center;gap:8px;cursor:pointer;color:var(--label-2);font-size:12px;font-weight:500;list-style:none;
padding:4px 10px 4px 8px;margin-left:-8px;border-radius:var(--r-pill);transition:background var(--fast) var(--ease)}
details.numbers>summary:hover{background:var(--fill);color:var(--label)}
details.numbers>summary::-webkit-details-marker{display:none}
details.numbers>summary::before{content:"";width:5px;height:5px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;
transform:rotate(-45deg);transition:transform var(--base) var(--ease)}
details.numbers[open]>summary::before{transform:rotate(45deg)}
details.numbers .table-scroll{margin-top:10px;border:1px solid var(--hairline);border-radius:var(--r-md)}
.pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 28px}
.pair>.viz+.viz{border-top:0;padding-top:8px}
.filters{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 12px;padding:16px 22px}
.filters label{display:flex;flex-direction:column;gap:6px;font-size:12px;font-weight:500;color:var(--label-2);min-width:150px;flex:1}
.loglines{list-style:none;margin:0;padding:0}
.loglines li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 12px;padding:9px 0;border-top:1px solid var(--line);font-size:12px}
.loglines li:first-child{border-top:0}.loglines .msg{font-family:var(--mono);overflow-wrap:anywhere}
.links{display:flex;flex-wrap:wrap;gap:8px;padding:6px 0 16px}.links .btn{white-space:normal;height:auto;min-height:36px;padding:8px 14px}
.strip{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1.2fr) minmax(0,1.3fr) minmax(0,1.3fr);gap:10px 24px;margin:0;padding:16px 0 14px}
.strip dt{color:var(--label-3);font-size:11.5px;font-weight:500}.strip dd{margin:4px 0 0;overflow-wrap:anywhere;font-size:13px}
.strip .chip,.release-line .chip,.list td .chip{white-space:normal;height:auto;min-height:24px;padding:2px 10px;line-height:1.4}
"""

# ---- the setup screen (labdeploy/connections_page.py): three numbered steps, one open at a time

CONNECTIONS_STYLE = """
.connections{line-height:1.6}
.connections .page-head h1{font-size:28px}
.connections .help{color:var(--label-2);line-height:1.6;max-width:none}
.connections .card-body{padding:8px 22px 22px;overflow-wrap:anywhere}.connections .card-body>p{margin-bottom:12px}
.connections .btn{white-space:normal;height:auto;min-height:38px;padding:8px 14px;text-align:center}
.connections .card-body>form+.btn{margin-top:8px}
.connections .setup-step>summary{display:flex;align-items:center;gap:16px;padding:18px 22px;list-style:none;cursor:pointer}
.connections .setup-step>summary>span:nth-child(2){flex:1;min-width:0}
.connections .step-number{display:flex;align-items:center;justify-content:center;width:34px;height:34px;border-radius:50%;background:var(--fill);color:var(--label-2);
font-weight:600;font-variant-numeric:tabular-nums;flex:none;box-shadow:inset 0 0 0 1px var(--line);transition:background var(--base) var(--ease),color var(--base) var(--ease)}
.connections .setup-step[open]{border-color:rgba(var(--accent-rgb),.35);box-shadow:0 0 0 1px rgba(var(--accent-rgb),.12),var(--shadow-2)}
.connections .setup-step[open] .step-number{background:linear-gradient(180deg,var(--accent-top),var(--accent-bottom));color:var(--on-accent);box-shadow:none}
.connections .step-caption{display:block;font-size:12.5px;color:var(--label-2);font-weight:400;overflow-wrap:anywhere;margin-top:1px}
.connections .step-toggle{color:var(--label-3);transition:transform var(--base) var(--ease)}.connections .setup-step[open] .step-toggle{transform:rotate(180deg)}
.connections .extra-options{padding:14px 0;border-top:1px solid var(--line);margin-top:14px}
.connections .extra-options>summary{cursor:pointer;color:var(--accent-text);font-weight:500;padding:6px 0;list-style:none}
.connections .extra-options>summary::-webkit-details-marker{display:none}
.connections .extra-options[open]>summary{margin-bottom:12px}
.connections .row{grid-template-columns:minmax(0,1fr);gap:8px}
.connections .repository-choice,.connections .switch-row{grid-template-columns:minmax(0,1fr) auto;align-items:center}
.connections .repository-choice input{width:18px;height:18px;cursor:pointer;accent-color:var(--accent)}
.connections input[type=text],.connections select{height:40px;font-size:13.5px}
.connections input[type=text],.connections .mono,.connections code{direction:ltr;text-align:left;unicode-bidi:plaintext}
.connections #repository,.connections select[name=project]{direction:ltr;text-align:left}
.connections .btn{min-height:40px}.connections .actions{justify-content:flex-start;flex-wrap:wrap}
.connections .strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px}
"""

# ---- motion: brief, purposeful, and off on request

MOTION = """
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes reveal{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
@keyframes chart-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
@keyframes ripple{0%{transform:scale(.6);opacity:.8}100%{transform:scale(1.8);opacity:0}}
.content>*{animation:rise var(--slow) var(--ease) both}
.content>*:nth-child(2){animation-delay:40ms}.content>*:nth-child(3){animation-delay:80ms}.content>*:nth-child(4){animation-delay:120ms}
.content>*:nth-child(5){animation-delay:160ms}.content>*:nth-child(6){animation-delay:200ms}.content>*:nth-child(n+7){animation-delay:240ms}
.tiles>.tile:nth-child(2),.apps>.app:nth-child(2){animation-delay:60ms}.tiles>.tile:nth-child(3),.apps>.app:nth-child(3){animation-delay:120ms}
.tiles>.tile:nth-child(4),.apps>.app:nth-child(n+4){animation-delay:180ms}
.tiles>.tile,.apps>.app{animation:rise var(--slow) var(--ease) both}
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}
}
"""

# ---- smaller screens: the sidebar becomes a translucent bar, rows stack

RESPONSIVE = """
@media (max-width:1100px){.hide-md{display:none}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}.content{padding:32px 32px 72px}}
@media (max-width:767px){
.shell{flex-direction:column}
.side{position:sticky;top:0;height:auto;width:auto;flex:none;display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"mark who" "nav nav";
align-items:center;padding:10px 16px 0;border-right:0;border-bottom:1px solid var(--line);background:var(--bar-bg)}
.mark{grid-area:mark;padding:0}
.who{grid-area:who;margin:0;padding:0;border:0;display:flex;align-items:center;gap:10px}
.who .avatar{width:30px;height:30px;font-size:12.5px}.who .as,.who .version,.who .name{display:none}.who form{margin:0}.who .btn{height:34px;padding:0 12px;width:auto}
.nav{grid-area:nav;gap:2px;margin:10px -16px 0;padding:0 0 8px}
.nav-section{display:flex;align-items:center;min-width:0}
.nav-group{flex:none;width:84px;margin:0;padding:0 0 0 16px;font-size:10.5px}
.nav-links{flex-direction:row;overflow-x:auto;gap:4px;padding:2px 16px 2px 0;scrollbar-width:none;min-width:0}
.nav-links::-webkit-scrollbar{display:none}.nav a{flex:none;height:34px}
.content{padding:24px 16px 56px}
h1,.page-head h1{font-size:24px}
.row{grid-template-columns:minmax(0,1fr);gap:10px;padding:14px 0}
.grid2{grid-template-columns:minmax(0,1fr)}
.card-head{padding:16px 16px 4px}.card-body{padding:4px 16px 6px}
.hide-sm{display:none}
.list th,.list td{padding:12px}
.people summary{grid-template-columns:minmax(0,1fr) auto;padding:14px 16px}.people summary .chips{grid-column:1/-1;grid-row:2}
.people .edit{padding:0 16px 8px}
.changes li{grid-template-columns:minmax(0,1fr)}.changes .values{text-align:left}
details.diff>summary{padding:14px 16px}details.diff>summary .muted{flex-basis:100%;padding-left:24px}
pre.diff span{padding:0 16px}
.facts div{grid-template-columns:minmax(0,1fr);gap:2px}
.actions{justify-content:stretch}.actions .btn{flex:1}.actions .note{flex-basis:100%}
.danger-card .confirm-line{flex-direction:column}.danger-card .confirm-line input{flex:none}
.row.switch-row{grid-template-columns:minmax(0,1fr) auto;gap:16px}.row.switch-row .control.switch-cell{align-items:flex-end;padding-top:2px}
.editor textarea{min-height:420px}
.btn,input[type=text],input[type=password],input[type=number],select{min-height:44px}
.pager{padding:12px 16px}.pager .pages .btn{min-height:30px}
.toolbar{top:0;margin:-8px -8px 20px;padding:8px}.toolbar .refresh{margin-left:-8px}
.tiles{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.tile{padding:14px 16px 12px}.tile .value{font-size:24px}.tile.wide{grid-column:1/-1}
.apps{grid-template-columns:minmax(0,1fr)}
.attention li{grid-template-columns:20px minmax(0,1fr)}.attention li>a{grid-column:2}
.pair{grid-template-columns:minmax(0,1fr)}.pair>.viz+.viz{border-top:1px solid var(--line);padding-top:16px}
.filters{padding:14px 16px}.filters label{min-width:0;flex-basis:100%}
.facts.tight div{grid-template-columns:minmax(0,1fr)}
.strip{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.stack thead{display:none}
.stack tbody tr{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px 14px;padding:12px 16px;border-bottom:1px solid var(--line)}
.stack tbody tr:last-child{border-bottom:0}
.list.stack td{display:block;padding:0;border:0;white-space:normal;text-align:left;min-width:0}
.list.stack td::before{content:attr(data-label);display:block;color:var(--label-3);font-size:11.5px;font-weight:500;margin-bottom:1px}
.list.stack td.wide{grid-column:1/-1}.list.stack td.none{display:none}
.connections .setup-step>summary{padding:16px}.connections .card-body{padding:8px 16px 16px}
}
@media (max-width:480px){.mark span{white-space:nowrap}.center .card{padding:28px 22px 24px}}
@media print{.side,.toolbar{display:none!important}.shell,.main,.content{display:block!important;margin:0!important;padding:0!important}
body{background:#fff}.card,.tile,.app{box-shadow:none;break-inside:avoid}.content>*{animation:none}}
"""

STYLE = TOKENS + BASE + CONNECTIONS_STYLE + MOTION + RESPONSIVE

# ---- the mark: a rounded square in the accent, the bolt in white

MARK = ('<svg class="logo" viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6.5" fill="#2457D4"/>'
        '<rect width="24" height="12" rx="6.5" fill="#4A84F2" opacity=".6"/>'
        '<path d="M13.2 5 7.5 13.2h4.1L10.8 19l5.7-8.2h-4.1L13.2 5z" fill="#fff"/></svg>')
